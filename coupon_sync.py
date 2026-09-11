#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The coupon tab of the price sheet <-> the site's coupon store.

The owner runs discounts from a sheet, the till checks them in KV, and this is
the wire between the two: every row of 'קופונים' is pushed to
/api/orders {action:'coupon_sync'}, then the server's own view is read back and
written into the three grey columns (Q uses / R source / S synced) so the sheet
shows what the till actually believes. Codes the server knows and the sheet does
not - the personal codes minted by the survey reward - are appended as rows, so
the sheet stays the whole picture without ever being the source of truth for
them.

What must never happen here: a personal code, a bound email or a social handle
in the output. This repo is PUBLIC and every run of coupons-sync.yml publishes
its log. Rows carrying them are written to the SHEET (the owner's own, private)
and counted - never named - on stdout.

Exit codes:
    0  synced (or a clean dry run)
    1  the site refused or could not be reached
    2  configuration: no ORDERS_TOKEN, no credentials, unusable tab

Run:
    python coupon_sync.py --dry-run     # read + parse + report, no writes
    python coupon_sync.py               # the real thing (hourly in Actions)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

SHEET_ID = "108D3BUV-MNcIuRZuKUgb-E-b1Ra8moxWZZyI5JxnyRo"
TAB = "קופונים"
# Two service accounts can see this spreadsheet and only one of them can WRITE
# to it: the scraper's, the same identity every other bot here uses and the same
# one GOOGLE_CREDENTIALS_JSON carries in Actions. esim-bot's file is kept as a
# fallback for a desktop that has it and not the other, but it is read-only on
# this sheet - creating the tab with it returns 403.
CRED_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json"),
    "/Users/bhmis/Documents/esim-bot/service_account.json",
]
ORDERS_URL = os.environ.get("ORDERS_URL", "https://www.waverole.com/api/orders")
TZ = ZoneInfo("Asia/Jerusalem")

# Column A..S, in this order. The sync finds them by TEXT, never by position -
# the owner reorders columns and a positional reader would then push the note
# column as the discount.
HEADERS = [
    "קוד - Code",
    "הנחה % - Percent",
    "הנחה $ - Fixed",
    "תקרה $ - Max off",
    "מ-GB (כולל) - Min GB",
    "עד GB (כולל) - Max GB",
    "לא תקף ל - Exclude",
    "תקף רק ל - Only",
    "מקס׳ שימושים - Max uses",
    "לכל לקוח - Per customer",
    "מתאריך - Starts",
    "עד תאריך - Expires",
    "פעיל - Active",
    "רק לאימייל - Email",
    "תווית - Label",
    "הערה - Note",
    "שימושים - Uses",
    "מקור - Source",
    "סונכרן - Synced",
]

# header text -> the field name the server's upsert() expects
FIELD = {
    HEADERS[0]: "code",
    HEADERS[1]: "pct",
    HEADERS[2]: "fixed_usd",
    HEADERS[3]: "max_off_usd",
    HEADERS[4]: "min_gb",
    HEADERS[5]: "max_gb",
    HEADERS[6]: "exclude",
    HEADERS[7]: "only",
    HEADERS[8]: "max_uses",
    HEADERS[9]: "per_customer",
    HEADERS[10]: "starts_at",
    HEADERS[11]: "expires_at",
    HEADERS[12]: "active",
    HEADERS[13]: "bound_email",
    HEADERS[14]: "label",
    HEADERS[15]: "note",
}
COL_USES, COL_SOURCE, COL_SYNCED = HEADERS[16], HEADERS[17], HEADERS[18]

SOURCE_HE = {"builtin": "מובנה", "sheet": "גיליון", "auto": "אוטומטי", "api": "API"}

# The 5 codes index.html used to carry in client JS. They are seeded as rows so
# the owner can see - and switch off - what is already circulating.
BUILTIN_ROWS = [
    ("WAVE10", 10, None, "WAVE10 10% off"),
    ("WELCOME", 10, None, ""),
    ("WAVE20", 20, None, ""),
    ("TRAVEL5", None, 5, ""),
    ("CAPYBARA", 15, None, ""),
]

A1_NOTE = (
    "עמודות לא תקף ל / תקף רק ל: רשימת מק״טים מופרדת בפסיקים.\n"
    "אפשר מק״ט מלא (1.81.10), תחילית (1.81 = כל הגדלים של אותה מדינה),\n"
    "או רק את קוד המדינה/אזור (81 = כל החבילות שלה בכל הרמות).\n\n"
    'מ-GB / עד GB הם כולל: "מעל 1GB" נכתב כ-2 בעמודת מ-GB.\n\n'
    "עמודות שימושים / מקור / סונכרן (האפורות) נכתבות על ידי הסנכרון - "
    "מה שנכתב בהן ביד יימחק."
)

TRUE_WORDS = {"כן", "yes", "y", "true", "1", "v", "✓", "on", "כן ", "פעיל"}
FALSE_WORDS = {"לא", "no", "n", "false", "0", "off", "מושבת", ""}

SHEETS_EPOCH = dt.date(1899, 12, 30)
CODE_RE = re.compile(r"^[A-Z0-9]{2,24}$")


# ── cell parsing ────────────────────────────────────────────────────────────
# Cells arrive UNFORMATTED (see read_tab): a number is a number, a date is a
# serial. That is deliberate - a currency format on one cell once cancelled
# three paid orders because the reader matched the RENDERING (memory:
# variant-cell-rendering).

def text(v) -> str:
    return "" if v is None else str(v).strip()


def norm_code(raw) -> str:
    """Trim + uppercase. '' for anything the site would refuse anyway."""
    c = text(raw).upper()
    return c if CODE_RE.match(c) else ""


def parse_num(v, field="", lo=None, hi=None):
    """A number or None. Raises ValueError naming the column, not 'row 14'."""
    if isinstance(v, bool):
        raise ValueError(f"{field}: expected a number, got a checkbox")
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        s = text(v).replace("$", "").replace("%", "").replace(",", "").replace("₪", "")
        if not s:
            return None
        try:
            n = float(s)
        except ValueError:
            raise ValueError(f"{field}: '{text(v)}' is not a number")
    if lo is not None and n < lo or hi is not None and n > hi:
        raise ValueError(f"{field}: {n:g} is outside {lo}..{hi}")
    return n


def parse_pct(v, field=""):
    """Percent, with the percent-FORMAT trap defused.

    A cell formatted as a percent reads back as 0.1, not 10. Nobody ships a
    0.1% coupon, so a value under 1 is the format talking and is scaled up;
    anything else is taken literally.
    """
    n = parse_num(v, field, 0, 100)
    if n is not None and 0 < n < 1:
        n *= 100
    return n


def parse_int(v, field=""):
    n = parse_num(v, field, 0, None)
    if n is None:
        return None
    if abs(n - round(n)) > 1e-9:
        raise ValueError(f"{field}: {n:g} must be a whole number")
    return int(round(n))


def parse_bool(v) -> bool:
    """Blank counts as NOT active - a half-typed row must not go live."""
    if isinstance(v, bool):
        return v
    s = text(v).lower()
    if s in TRUE_WORDS or text(v) in TRUE_WORDS:
        return True
    if s in FALSE_WORDS or text(v) in FALSE_WORDS:
        return False
    return False


def parse_list(v) -> list[str]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # a bare country code typed into 'only' arrives as the number 81
        return [("%g" % v)]
    parts = re.split(r"[,\n;]+", text(v))
    return [p.strip() for p in parts if p.strip()]


def parse_date(v, end_of_day=False):
    """Sheet date -> ISO with the Israel offset, or None.

    Whole-day cells are widened to the day the owner means: a start date opens
    at 00:00 Israel and an expiry runs to 23:59:59 Israel. Bare 'YYYY-MM-DD'
    would be read by the site as UTC midnight, which retires a coupon three
    hours before the date printed next to it. A cell that already carries a
    time is passed through untouched.
    """
    if isinstance(v, bool):
        raise ValueError("date: expected a date, got a checkbox")
    if isinstance(v, dt.datetime):
        return v.isoformat()
    if isinstance(v, dt.date):
        return _stamp(v, end_of_day)
    if isinstance(v, (int, float)):
        if v <= 0:
            return None
        d = SHEETS_EPOCH + dt.timedelta(days=int(v))
        frac = float(v) - int(v)
        if frac > 1e-6:                      # the cell carried a clock time too
            base = dt.datetime.combine(d, dt.time()) + dt.timedelta(seconds=round(frac * 86400))
            return base.replace(tzinfo=TZ).isoformat()
        return _stamp(d, end_of_day)
    s = text(v)
    if not s:
        return None
    if "T" in s:                             # already an instant, keep it
        return s
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", s)
    if m:
        y, mo, day = (int(x) for x in m.groups())
    else:
        m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", s)
        if not m:
            raise ValueError(f"date: '{s}' is not a date (dd/mm/yyyy or yyyy-mm-dd)")
        day, mo, y = (int(x) for x in m.groups())   # day first: the owner is Israeli
        if y < 100:
            y += 2000
    try:
        return _stamp(dt.date(y, mo, day), end_of_day)
    except ValueError:
        raise ValueError(f"date: '{s}' is not a real date")


def _stamp(d: dt.date, end_of_day: bool) -> str:
    t = dt.time(23, 59, 59) if end_of_day else dt.time(0, 0, 0)
    return dt.datetime.combine(d, t, tzinfo=TZ).isoformat()


def row_to_input(row: dict) -> dict | None:
    """One sheet row -> one upsert input. None when the row has no code.

    Raises ValueError with a column name when a cell cannot be read; the caller
    reports the row and moves on, because one broken row must not stop the
    other twenty from syncing.
    """
    code = norm_code(row.get(HEADERS[0]))
    if not code:
        if text(row.get(HEADERS[0])):
            raise ValueError("code: letters and digits only, 2-24 characters")
        return None
    email = text(row.get(HEADERS[13])).lower() or None
    # Rows the server wrote here are its own: a minted personal code pushed
    # back up would be reborn as a permanent sheet definition the day its KV
    # record expires. The source cell says so; the shape says so when the
    # cell was lost.
    if text(row.get(COL_SOURCE)) == SOURCE_HE["auto"] or (MINT_RE.match(code) and email):
        return None
    label = text(row.get(HEADERS[14]))[:40]
    return {
        "code": code,
        "pct": parse_pct(row.get(HEADERS[1]), HEADERS[1]),
        "fixed_usd": parse_num(row.get(HEADERS[2]), HEADERS[2], 0, None),
        "max_off_usd": parse_num(row.get(HEADERS[3]), HEADERS[3], 0, None),
        "min_gb": parse_num(row.get(HEADERS[4]), HEADERS[4], 0, None),
        "max_gb": parse_num(row.get(HEADERS[5]), HEADERS[5], 0, None),
        "exclude": parse_list(row.get(HEADERS[6])),
        "only": parse_list(row.get(HEADERS[7])),
        "max_uses": parse_int(row.get(HEADERS[8]), HEADERS[8]),
        "per_customer": parse_int(row.get(HEADERS[9]), HEADERS[9]),
        "starts_at": parse_date(row.get(HEADERS[10])),
        "expires_at": parse_date(row.get(HEADERS[11]), end_of_day=True),
        "active": parse_bool(row.get(HEADERS[12])),
        "bound_email": email,
        "label": label or None,
        "note": text(row.get(HEADERS[15])) or None,
    }


def parse_rows(values: list[list]) -> tuple[list[dict], list[str], dict[str, int]]:
    """(inputs, problems, header index). Row 1 is the header."""
    if not values:
        raise ValueError("the coupon tab is empty - no header row")
    head = [text(h) for h in values[0]]
    idx = {h: head.index(h) for h in HEADERS if h in head}
    missing = [h for h in HEADERS if h not in idx]
    if missing:
        raise ValueError("missing columns: " + ", ".join(missing))
    width = max(idx.values()) + 1
    inputs, problems, seen = [], [], {}
    for n, raw in enumerate(values[1:], start=2):
        cells = list(raw) + [""] * (width - len(raw))
        row = {h: cells[i] for h, i in idx.items()}
        try:
            inp = row_to_input(row)
        except ValueError as e:
            problems.append(f"row {n}: {e}")
            continue
        if inp is None:
            continue
        if inp["code"] in seen:
            name = public(inp["code"], bound_email=inp["bound_email"])
            problems.append(f"row {n}: {name} already on row {seen[inp['code']]}")
            continue
        seen[inp["code"]] = n
        inputs.append(inp)
    return inputs, problems, idx


# ── what is safe to say out loud ────────────────────────────────────────────

# The shape mintPersonal() produces. A personal code that has been written back
# into the sheet arrives on the NEXT run as an ordinary row with no source, so
# the shape and the binding are the only two signs left that it is a secret.
MINT_RE = re.compile(r"^WR[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$")


def public(code: str, source: str = "", bound_email=None) -> str:
    """A personal code is a bearer secret; this log is world-readable."""
    personal = source == "auto" or bool(bound_email) or bool(MINT_RE.match(code or ""))
    return "(אישי)" if personal else code


# ── sheet I/O ───────────────────────────────────────────────────────────────

def open_tab(create=True):
    """Credentials from the environment in the cloud, from a file on a desktop -
    the same order as choose_supplier.sheets_service."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    env = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if env:
        creds = Credentials.from_service_account_info(json.loads(env), scopes=scopes)
    else:
        path = next((p for p in CRED_PATHS if os.path.exists(p)), None)
        if not path:
            raise FileNotFoundError(
                "no GOOGLE_CREDENTIALS_JSON and none of " + ", ".join(CRED_PATHS))
        creds = Credentials.from_service_account_file(path, scopes=scopes)
    sh = gspread.authorize(creds).open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(TAB)
    except gspread.WorksheetNotFound:
        if not create:
            raise
        ws = None
    if ws is None:
        ws = create_tab(sh)
    else:
        add_missing_headers(ws)
    return sh, ws


def create_tab(sh):
    """Build 'קופונים' from nothing: headers, the 5 circulating codes, and the
    formatting that says which three columns the owner should not type in."""
    ws = sh.add_worksheet(title=TAB, rows=200, cols=len(HEADERS))
    rows = [HEADERS]
    for code, pct, fixed, label in BUILTIN_ROWS:
        r = [""] * len(HEADERS)
        r[0] = code
        r[1] = pct if pct is not None else ""
        r[2] = fixed if fixed is not None else ""
        r[12] = "כן"
        r[14] = label
        r[17] = SOURCE_HE["builtin"]
        rows.append(r)
    ws.update(rows, f"A1:S{len(rows)}", value_input_option="RAW")
    sid = ws.id
    grey = {"red": 0.937, "green": 0.937, "blue": 0.937}
    sh.batch_update({"requests": [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"frozenRowCount": 1},
                           "rightToLeft": True},
            "fields": "gridProperties.frozenRowCount,rightToLeft"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.85, "green": 0.89, "blue": 0.95},
                "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(textFormat,backgroundColor,wrapStrategy)"}},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1,
                      "startColumnIndex": 16, "endColumnIndex": 19},
            "cell": {"userEnteredFormat": {"backgroundColor": grey}},
            "fields": "userEnteredFormat.backgroundColor"}},
        {"updateCells": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "rows": [{"values": [{"note": A1_NOTE}]}],
            "fields": "note"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": 19},
            "properties": {"pixelSize": 120}, "fields": "pixelSize"}},
    ]})
    return ws


def add_missing_headers(ws):
    """The tab is already there: only fill in headers it does not have, to the
    right of what is written. Existing columns are never moved - the owner's
    data sits under them."""
    head = [text(h) for h in (ws.get_values("1:1") or [[]])[0]]
    missing = [h for h in HEADERS if h not in head]
    if not missing:
        return
    start = len(head) + 1
    ws.update([missing], _a1(1, start, 1, start + len(missing) - 1),
              value_input_option="RAW")
    print(f"  added {len(missing)} missing header(s)")


def _a1(r1, c1, r2, c2) -> str:
    return f"{_col(c1)}{r1}:{_col(c2)}{r2}"


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def read_tab(ws) -> list[list]:
    """UNFORMATTED_VALUE: a cell arrives as what it IS, not as what it looks
    like. See the note above parse_num."""
    return ws.get_values(value_render_option="UNFORMATTED_VALUE")


# ── the site ────────────────────────────────────────────────────────────────

def push(token: str, coupons: list[dict]) -> dict:
    import requests
    r = requests.post(ORDERS_URL, timeout=30,
                      headers={"Authorization": "Bearer " + token},
                      json={"action": "coupon_sync", "coupons": coupons})
    if r.status_code != 200:
        raise RuntimeError(f"coupon_sync HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def fetch(token: str) -> dict:
    import requests
    r = requests.get(ORDERS_URL, timeout=30, params={"coupons": "1"},
                     headers={"Authorization": "Bearer " + token})
    if r.status_code != 200:
        raise RuntimeError(f"coupons read HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


# ── writing the answer back ─────────────────────────────────────────────────

def plan_writes(values: list[list], idx: dict[str, int], server: list[dict],
                now: str) -> tuple[list[dict], list[str]]:
    """The single batch: Q/R/S beside every row the sheet already has, plus a
    row for every code the server knows and the sheet does not.

    Returns (gspread batch entries, names of the appended codes).
    """
    by_code = {norm_code(c.get("code")): c for c in server if norm_code(c.get("code"))}
    ccol = idx[HEADERS[0]]
    qcol = idx[COL_USES]
    contiguous = idx[COL_SOURCE] == qcol + 1 and idx[COL_SYNCED] == qcol + 2

    trio, seen = [], set()
    for raw in values[1:]:
        cells = list(raw) + [""] * (max(ccol, idx[COL_SOURCE]) + 1 - len(raw))
        code = norm_code(cells[ccol])
        d = by_code.get(code)
        if not code or not d:
            # Not on the server (yet, or any more): no count, no stamp - but
            # the source cell stays, so a personal code that has expired out
            # of KV is still recognised as the server's and never pushed back.
            src = cells[idx[COL_SOURCE]] if len(cells) > idx[COL_SOURCE] else ""
            trio.append(["", src, ""])
            continue
        seen.add(code)
        trio.append([d.get("uses") or 0,
                     SOURCE_HE.get(d.get("source") or "", d.get("source") or ""),
                     now])

    entries = []
    if trio:
        last = len(values)
        if contiguous:
            entries.append({"range": _a1(2, qcol + 1, last, qcol + 3), "values": trio})
        else:
            # A reordered sheet is still worth syncing; it just costs 3 ranges.
            for n, col in enumerate((qcol, idx[COL_SOURCE], idx[COL_SYNCED])):
                entries.append({"range": _a1(2, col + 1, last, col + 1),
                                "values": [[t[n]] for t in trio]})

    extra, names = [], []
    for code, d in sorted(by_code.items()):
        if code in seen:
            continue
        src = d.get("source") or ""
        r = [""] * len(HEADERS)
        r[0] = code
        r[1] = d.get("pct") or ""
        r[2] = d.get("fixed_usd") or ""
        r[3] = d.get("max_off_usd") or ""
        r[4] = d.get("min_gb") or ""
        r[5] = d.get("max_gb") or ""
        r[6] = ", ".join(d.get("exclude") or [])
        r[7] = ", ".join(d.get("only") or [])
        r[8] = d.get("max_uses") or ""
        r[9] = d.get("per_customer") or ""
        r[10] = (d.get("starts_at") or "")[:10]
        r[11] = (d.get("expires_at") or "")[:10]
        r[12] = "כן" if d.get("active") else "לא"
        r[13] = d.get("bound_email") or ""
        r[14] = d.get("label") or ""
        r[15] = d.get("note") or ""
        r[16] = d.get("uses") or 0
        r[17] = SOURCE_HE.get(src, src)
        r[18] = now
        extra.append(r)
        names.append(public(code, src, d.get("bound_email")))
    if extra:
        first = len(values) + 1
        entries.append({"range": _a1(first, 1, first + len(extra) - 1, len(HEADERS)),
                        "values": extra})
    return entries, names


# ── main ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="sync the coupon tab with the site")
    ap.add_argument("--dry-run", action="store_true",
                    help="read and report; write nothing anywhere")
    args = ap.parse_args(argv)

    token = os.environ.get("ORDERS_TOKEN", "").strip()
    try:
        sh, ws = open_tab()
        values = read_tab(ws)
        inputs, problems, idx = parse_rows(values)
    except Exception as e:
        print(f"sheet: {e}")
        return 2

    print(f"tab '{TAB}': {len(values) - 1} rows, {len(inputs)} coupon(s) to push")
    for p in problems:
        print(f"  skipped {p}")
    if inputs:
        # Names, not counts, are what the owner reads a run by - but only of the
        # codes it is safe to publish. The personal ones are counted instead.
        for state, want in (("active", True), ("inactive", False)):
            names = [public(i["code"], bound_email=i["bound_email"])
                     for i in inputs if i["active"] is want]
            if names:
                print(f"  {', '.join(names)}  ({state})")

    if not token:
        # Fail closed and say why. A missing token used to look exactly like a
        # working sync with nothing to do (memory: orders-token-locations).
        print("no ORDERS_TOKEN in the environment - stopping before any HTTP call.")
        return 0 if args.dry_run else 2

    try:
        if args.dry_run:
            print(f"dry run: would POST coupon_sync with {len(inputs)} coupon(s)")
        else:
            res = push(token, inputs)
            up = res.get("upserted") or []
            errs = res.get("errors") or []
            print(f"pushed: {len(up)} accepted, {len(errs)} refused")
            for e in errs:
                print(f"  refused {public(norm_code(e.get('code')))}: {e.get('error')}")
        state = fetch(token)
    except Exception as e:
        print(f"site: {e}")
        return 1

    server = state.get("coupons") or []
    pending = state.get("rewards_pending") or []
    now = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    entries, added = plan_writes(values, idx, server, now)

    if args.dry_run:
        print(f"dry run: would write {len(entries)} range(s), "
              f"append {len(added)} row(s)")
    elif entries:
        need = len(values) + len(added)
        if need > ws.row_count:              # a values write past the grid is an error
            ws.add_rows(need - ws.row_count + 50)
        sh.values_batch_update({
            "valueInputOption": "RAW",
            "data": [{"range": f"'{TAB}'!{e['range']}", "values": e["values"]}
                     for e in entries],
        })
        if added:
            print(f"appended {len(added)} code(s) from the site: " + ", ".join(added))

    kinds: dict[str, int] = {}
    for d in server:
        kinds[d.get("source") or "?"] = kinds.get(d.get("source") or "?", 0) + 1
    print("site holds " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
          if kinds else "site holds no coupons")
    print(f"rewards awaiting approval: {len(pending)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
