#!/usr/bin/env python3
"""Refresh the Stellar rows of the price sheet from Stellar's catalogue.

esim.dog has no API, so its prices are read by driving its website with a
browser — 54 minutes a day, at the edge of the run's budget. Stellar is the
other way round: the whole catalogue is one JSON file, read in four seconds,
no browser, no login. This script is that read, plus the one piece of
judgment the read cannot make for us: WHICH of Stellar's listings is the
product we sell under a given SKU.

Where the price comes from
--------------------------
With STELLAR_READ_KEY in the environment (a plans:read-only key — memory:
stellar-key-placement) the catalogue is read from the wholesale API,
GET /api/v1/plans, paged at 100 a call: the price on each listing is the
one we actually pay, in EUR, and every listing carries the plan UUID an
order is placed against. Without the key the script falls back to the
public retail feed and ESTIMATES wholesale as retail / RETAIL_OVER_WHOLESALE
(1.2192, measured 2026-09-09 over 58 rows, sd 0.0041). The fallback says so
in the run log; it exists so a lost key degrades the prices, not the run.

Two things the API settles that the feed could not: `data.megabytes` is the
true size (3 GB is 3072), and `coverage.codes` is the plan's own coverage,
not its product page's. The regional rule is kept as it was — a code sold
under ANY multi-country listing is regional — the conservative reading of
both sources. A read that makes most coded rows vanish at once is treated
as a broken read and nothing is written (see sanity()).

Why the listing has to be CHOSEN
--------------------------------
A Stellar package code is a family, not a product: CKH995 is sold as
6/7/8/9/10 GB x 20/30 days across ten listings. The 20-day and the 30-day
listing usually cost the same, and the 20-day one is strictly worse. The
sheet's Stellar rows were first written from a cheapest-first read, and 19 of
them landed on the 20-day twin: they say Stellar gives fewer days than it
does, for no saving at all. This script picks by the owner's rule instead:

    GB must equal the SKU's GB exactly.
    Days must be AT LEAST what the customer is promised — the days on the row
    we actually sell (the esim.dog row, or the Stellar row itself when it is
    the only one). More days is fine; fewer is not a substitute.
    Among what qualifies: the cheapest; on a tie, the longer product.

Two traps in the feed, both handled here and pinned by test_stellar_prices.py:
  * `variant.data_gb` is ROUNDED UP — a 750MB plan says 1. `meta.data_gb`
    holds the true size and is the only size field read.
  * `meta.coverage_codes` echoes the product page, not the plan. The feed says
    Taiwan's plans cover TW, but their code P32JCTKR0 is also listed under the
    12-area Asia product — it IS that regional. A code is regional if it is
    sold under any regional product, and a regional code under a country SKU
    is refused, never priced as the country.

What this script owns on a Stellar row
--------------------------------------
Only the cells the catalogue answers: days, buy price, the bookkeeping beside
it (previous price / updated / changed / last change), and the stock column —
that one only while it holds one of this script's own markers. A value the
owner typed there is never cleared. SKU, country, GB, code, networks and
breakout are read, not written. A row with no code ('—') is left alone.

Run:
    python stellar_prices.py              dry run — prints the plan, writes nothing
    python stellar_prices.py --apply      writes to the sheet
    python stellar_prices.py --feed F     read a saved retail feed instead of fetching
    python stellar_prices.py --plans F    read a saved wholesale-API dump instead of fetching
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from esim_price_scraper import HEADER_KEYS, SHEET_ID, col_letter

FEED_URL = "https://stellarsecurity.com/assets/esim/products.index.json"   # retail; fallback only
API_URL = "https://wholesale.stellarsecurity.com/api/v1/plans"
API_PER_PAGE = 100            # the API's maximum
API_MAX_PAGES = 60            # 6,000 listings; the catalogue is ~3,400. Also the 60/min budget.
LONGER_TOL = 0.05             # a longer validity may cost up to 5% over the cheapest listing
FX_URL = "https://api.frankfurter.app/latest"
# Measured 2026-09-09: 58 rows, median 1.2192, stdev 0.0041. Re-measure the
# day the wholesale API is wired in — a drift here mis-prices every row.
RETAIL_OVER_WHOLESALE = 1.2192
FX_FALLBACK = 1.1614          # frankfurter, 2026-09-08 — last resort only
SOURCE = "stellar"            # column D, compared lower-case
FEED_STALE_HOURS = 12         # the feed is a snapshot; say so when it is old

# Column Q means "out of stock when non-empty". These are the values THIS
# script writes there. It clears the cell only when it finds one of them, so
# a word the owner typed is never overwritten.
MARK_GONE = "לא זמין"                 # the code disappeared from the catalogue
MARK_SHORT = "פחות ימים מהמובטח"      # nothing at this GB with enough days
MARK_REGIONAL = "אזורי"               # a regional code under a country SKU
OUR_MARKS = frozenset({MARK_GONE, MARK_SHORT, MARK_REGIONAL})

# MB-named listings (750MB, 500MB) are kept so the family is whole; their
# size still comes from meta.data_gb, never from the name or the ceil'd field.
_SKU_RE = re.compile(r"^ESIM-(.+?)-([\d.]+)(?:GB|MB)-(\d+)D-([A-Z0-9]+)$")


# ── catalogue ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Variant:
    code: str          # package code — the family, from the SKU's tail
    gb: float          # meta.data_gb: the TRUE size (top-level data_gb is ceil'd)
    days: int
    wholesale_eur: float   # what WE pay: the API's price, or retail / 1.2192 from the feed
    slug: str          # the product the listing sat under
    name: str
    plan_id: str = ""  # the API's UUID — what an order is placed against; '' from the feed


class Catalogue:
    def __init__(self, variants, regional_codes, generated_at: str = ""):
        self.variants = list(variants)
        self.regional_codes = set(regional_codes)
        self.generated_at = generated_at
        self.by_code: dict[str, list[Variant]] = {}
        for v in self.variants:
            self.by_code.setdefault(v.code, []).append(v)

    @classmethod
    def from_feed(cls, feed: dict) -> "Catalogue":
        variants, regional = [], set()
        for product in feed.get("data", []):
            slug = product.get("slug", "")
            pmeta = product.get("meta") or {}
            # A product covering more than one area is a regional. Its codes
            # are what make a country-named listing regional too.
            is_regional = len(pmeta.get("coverage_codes") or []) > 1
            for v in product.get("variants", []):
                meta = v.get("meta") or {}
                if meta.get("data_type") != "Data in Total":
                    continue          # per-day unlimited plans are another product
                if not v.get("active", True):
                    continue
                m = _SKU_RE.match(str(v.get("sku", "")).upper())
                if not m:
                    continue
                gb, days, cents = meta.get("data_gb"), v.get("duration_days"), v.get("unit_price_cents")
                if gb is None or days is None or cents is None:
                    continue
                code = m.group(4)
                variants.append(Variant(code, float(gb), int(days),
                                        round(cents / 100.0 / RETAIL_OVER_WHOLESALE, 2),
                                        slug, str(v.get("name", ""))))
                if is_regional:
                    regional.add(code)
        return cls(variants, regional, str(feed.get("snapshot_generated_at", "")))

    @classmethod
    def from_api(cls, plans: list) -> "Catalogue":
        """The wholesale API's listings (GET /plans, every page). The price is
        what we pay. Daily-unlimited plans are billed per day and their SKU
        reads 3GBD-1D, so both the billing unit and the SKU shape drop them."""
        variants, regional, synced = [], set(), ""
        for p in plans:
            price = p.get("price") or {}
            if price.get("billing_unit", "plan") != "plan":
                continue          # per-day unlimited plans are another product
            if (p.get("duration") or {}).get("configurable"):
                continue
            if not p.get("available", True):
                continue
            m = _SKU_RE.match(str(p.get("sku") or "").upper())
            if not m:
                continue
            mb, days, cents = (p.get("data") or {}).get("megabytes"), p.get("validity_days"), price.get("amount_cents")
            if mb is None or days is None or cents is None:
                continue
            code = m.group(4)
            variants.append(Variant(code, _gb_from_mb(int(mb)), int(days), cents / 100.0,
                                    str(p.get("product_slug") or ""), str(p.get("name", "")),
                                    str(p.get("id", ""))))
            if len((p.get("coverage") or {}).get("codes") or []) > 1:
                regional.add(code)
            synced = max(synced, str(p.get("catalogue_synced_at") or ""))
        return cls(variants, regional, synced)

    def age_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        if not self.generated_at:
            return None
        try:
            gen = datetime.fromisoformat(self.generated_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - gen).total_seconds() / 3600


def _gb_from_mb(mb: int) -> float:
    """Stellar counts 1 GB as 1024 MB (3 GB is 3072). Sub-GB plans are named in
    round decimal sizes and the sheet says 0.75 for 750MB, so a size that is
    not a clean binary multiple is read as decimal — 750 and 768 both land on
    0.75, 500 and 512 on 0.5."""
    return round(mb / 1024, 3) if mb % 256 == 0 else round(mb / 1000, 3)


def fetch_feed() -> dict:
    r = requests.get(FEED_URL, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_plans(key: str) -> list:
    """Every listing of the wholesale catalogue. ~35 calls at 100 a page against
    a 60/minute limit; a second between pages keeps well clear of it."""
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {key}", "Accept": "application/json"})
    out, page = [], 1
    while page <= API_MAX_PAGES:
        r = s.get(API_URL, params={"per_page": API_PER_PAGE, "page": page}, timeout=60)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("data") or [])
        if page >= int((body.get("meta") or {}).get("last_page") or page):
            break
        page += 1
        time.sleep(1)
    return out


# ── the sheet ───────────────────────────────────────────────────────────────

@dataclass
class StellarRow:
    row: int
    sku: str
    country: str
    code: str                   # column 'Route' holds Stellar's package code
    gb: Optional[float]
    days: Optional[int]
    eur: Optional[float]        # parsed out of '$2.57 (€2.21)'
    price_cell: str
    changed: str
    stock: str
    floor_days: Optional[int] = None   # what the customer is promised


def _num(s) -> Optional[float]:
    m = re.search(r"[\d.]+", str(s or "").replace(",", ""))
    return float(m.group()) if m else None


def _eur(cell) -> Optional[float]:
    m = re.search(r"€\s*([\d.]+)", str(cell or ""))
    return float(m.group(1)) if m else None


def is_regional_sku(sku: str) -> bool:
    """The sheet's own legend: a second segment starting with 0 is regional
    (2.0B.10 is Stellar's 35-area Europe; 2.49.10 is Germany)."""
    parts = sku.split(".")
    return len(parts) >= 2 and parts[1].startswith("0")


REQUIRED = ("code", "countries", "gb", "source", "validity", "price", "prev",
            "updated", "changed", "last_change", "route", "stock")


def read_rows(values: list[list[str]]) -> tuple[list[StellarRow], dict[str, int]]:
    """Every Stellar row, with the days the customer is promised on its SKU.

    Columns are found by header text through the scraper's own HEADER_KEYS,
    so the two scripts can never disagree about which column is which.
    """
    if not values:
        return [], {}
    header = [str(h).strip() for h in values[0]]
    col = {k: header.index(h) for k, h in HEADER_KEYS.items() if h in header}
    missing = [HEADER_KEYS[k] for k in REQUIRED if k not in col]
    if missing:
        raise SystemExit(f"price sheet header missing: {missing}")
    width = max(col.values()) + 1

    stellar: list[StellarRow] = []
    sold_days: dict[str, int] = {}
    for idx, raw in enumerate(values[1:], start=2):
        r = [str(x) for x in raw] + [""] * (width - len(raw))
        sku = r[col["code"]].strip()
        if not sku or "." not in sku:
            continue
        src = r[col["source"]].strip().lower()
        days = _num(r[col["validity"]])
        if src == "esim.dog":
            # The row the site sells today; its days are the promise.
            if days:
                sold_days[sku] = int(days)
            continue
        if src != SOURCE:
            continue
        price = r[col["price"]].strip()
        stellar.append(StellarRow(
            row=idx, sku=sku, country=r[col["countries"]].strip(),
            code=r[col["route"]].strip(), gb=_num(r[col["gb"]]),
            days=int(days) if days else None, eur=_eur(price), price_cell=price,
            changed=r[col["changed"]].strip(), stock=r[col["stock"]].strip()))
    for s in stellar:
        s.floor_days = sold_days.get(s.sku, s.days)
    return stellar, col


# ── the decision ────────────────────────────────────────────────────────────

@dataclass
class Decision:
    row: StellarRow
    pick: Optional[Variant]
    reason: str = ""            # "" | "gone" | "short" | "regional"
    have_days: tuple = ()
    paid_up: float = 0.0        # EUR paid over the cheapest, to hold the longer plan
    won_days: int = 0           # days the cheapest listing would have cost us


def decide(cat: Catalogue, row: StellarRow) -> Optional[Decision]:
    """None means the row is not ours to touch (an owner's '—' note)."""
    if not row.code or row.gb is None:
        return None
    if row.code in cat.regional_codes and not is_regional_sku(row.sku):
        return Decision(row, None, "regional")
    family = cat.by_code.get(row.code)
    if not family:
        return Decision(row, None, "gone")
    floor = row.floor_days or 0
    same_gb = [v for v in family if abs(v.gb - row.gb) < 1e-9]
    ok = [v for v in same_gb if v.days >= floor]
    if not ok:
        return Decision(row, None, "short", tuple(sorted({v.days for v in same_gb})))
    # Inside one package code the listings are the SAME product cut at different
    # (GB, days), so a cent between the 20-day and the 30-day twin is a pricing
    # artifact, not a difference in what the buyer receives. Cheapest-wins
    # therefore sold a third of the validity for an agora. So: the LONGEST wins,
    # and a shorter listing only takes it back by saving real money -- more than
    # LONGER_TOL of the cheapest price. A tie goes to the plainer name, so a
    # '(nonhkip)' twin never wins on the order the API happened to list it in.
    thrift = min(ok, key=lambda v: (v.wholesale_eur, -v.days))     # the old rule's pick
    near = [v for v in ok if v.wholesale_eur <= thrift.wholesale_eur * (1 + LONGER_TOL) + 1e-9]
    best = max(near, key=lambda v: (v.days, -v.wholesale_eur, -len(v.name)))
    return Decision(row, best, paid_up=round(best.wholesale_eur - thrift.wholesale_eur, 4),
                    won_days=best.days - thrift.days)


def sanity(decisions) -> str:
    """A read that makes MOST coded rows vanish at once is a broken read (an
    empty page, a renamed SKU format, a revoked key answering 200 with nothing)
    — not a supplier that dropped half its catalogue overnight. The reason to
    stop, or '' to go on."""
    coded = [d for d in decisions if d.reason != "regional"]
    gone = sum(1 for d in coded if d.reason == "gone")
    if len(coded) >= 4 and gone * 2 > len(coded):
        return (f"{gone} of {len(coded)} package codes vanished at once — that is a broken "
                f"catalogue read, not a catalogue; nothing written")
    return ""


# ── money ───────────────────────────────────────────────────────────────────

def price_cell(eur: float, fx: float) -> str:
    """'$2.57 (€2.21)'. Column G carries a LEFT_TO_RIGHT text direction so this
    reads as written inside the RTL sheet — keep the format and the cell format
    in step (see memory: rtl-sheet-price-direction)."""
    return f"${eur * fx:.2f} (€{eur:.2f})"


def _frankfurter() -> Optional[float]:
    r = requests.get(FX_URL, params={"from": "EUR", "to": "USD"}, timeout=15)
    if r.status_code == 200:
        return float(r.json()["rates"]["USD"])
    return None


def fetch_fx(existing_cells, fetch: Callable[[], Optional[float]] = _frankfurter) -> tuple[float, str]:
    """EUR->USD, and where it came from.

    Live rate first. If that fails, the sheet's own price cells hold the rate
    that was used last time (dollars / euros), and staying consistent with
    yesterday beats a constant that goes stale for months. The constant is the
    last resort.
    """
    try:
        rate = fetch()
        if rate:
            return rate, "frankfurter"
    except Exception:            # noqa: BLE001 — any failure means fall back
        pass
    ratios = []
    for cell in existing_cells:
        usd = re.search(r"\$\s*([\d.]+)", str(cell or ""))
        eur = re.search(r"€\s*([\d.]+)", str(cell or ""))
        if usd and eur and float(eur.group(1)) > 0:
            ratios.append(float(usd.group(1)) / float(eur.group(1)))
    if ratios:
        return round(statistics.median(ratios), 4), "derived from the sheet's own cells"
    return FX_FALLBACK, "hard-coded fallback"


# ── what to write ───────────────────────────────────────────────────────────

def _real_note(note: str) -> bool:
    # Mirrors the scraper: a price-change note stays on the row until the next
    # change; anything else ('↔', an error text) is cleared once resolved.
    return note.startswith(("↑", "↓")) or note == "First check"


def plan_updates(decisions, fx: float, ts: str, today: str) -> list[tuple[int, str, str]]:
    """(row, HEADER_KEYS key, value) — the whole write, before any of it happens."""
    out: list[tuple[int, str, str]] = []

    def put(row, key, val):
        out.append((row, key, val))

    for d in decisions:
        r = d.row
        put(r.row, "updated", ts)

        if d.pick is None:
            if d.reason == "gone":
                note, mark = "הקוד נעלם מהקטלוג של Stellar", MARK_GONE
            elif d.reason == "short":
                have = ", ".join(f"{x}d" for x in d.have_days) or "כלום"
                note = f"אין {r.gb:g}GB ל-{r.floor_days}+ ימים בקוד {r.code} (יש: {have})"
                mark = MARK_SHORT
            else:
                note, mark = "קוד אזורי תחת שם מדינה — לא הושווה", MARK_REGIONAL
            put(r.row, "changed", note)
            # Only over an empty cell or our own marker — never over the owner's word.
            if (r.stock == "" or r.stock in OUR_MARKS) and r.stock != mark:
                put(r.row, "stock", mark)
            continue

        v = d.pick
        new_eur = v.wholesale_eur
        put(r.row, "price", price_cell(new_eur, fx))

        notes = []
        if r.eur is None:
            notes.append("First check")
        elif abs(new_eur - r.eur) > 0.001:
            diff = new_eur - r.eur
            pct = diff / r.eur * 100
            arrow, sign = ("↑", "+") if diff > 0 else ("↓", "-")
            notes.append(f"{arrow} {sign}€{abs(diff):.2f} ({sign}{abs(pct):.1f}%)")
            put(r.row, "prev", r.price_cell)
        if r.days != v.days:
            put(r.row, "validity", f"{v.days}d")
            notes.append(f"↔ {r.days}d → {v.days}d" if r.days else f"↔ {v.days}d")

        if notes:
            put(r.row, "changed", " | ".join(notes))
            if any(n[0] in "↑↓↔" for n in notes):
                put(r.row, "last_change", today)
        elif r.changed and not _real_note(r.changed):
            put(r.row, "changed", "")
        if r.stock in OUR_MARKS:
            put(r.row, "stock", "")
    return out


# ── Sheets I/O ──────────────────────────────────────────────────────────────

def sheets_service(cred_path: str):
    """Credentials from the environment in the cloud, from a file on a desktop.

    Same order as the scraper's setup_google_sheets: GitHub Actions has no
    credentials.json to read, only the GOOGLE_CREDENTIALS_JSON secret.
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    env = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if env:
        creds = Credentials.from_service_account_info(json.loads(env), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(cred_path, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet(svc) -> list[list[str]]:
    return svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="A1:Z").execute().get("values", [])


def stamp_price_direction(svc, col: dict[str, int]) -> int:
    """Make the money columns render left-to-right; report how many needed it.

    The sheet is right-to-left, so '$2.57 (€2.21)' renders euro-first unless the
    cell says otherwise — the owner reads €2.21 as the price. The string is not
    the price; the string PLUS this format is.

    Both money columns get it, not just the buy price: on a price move the OLD
    two-currency string is copied verbatim into 'מחיר קודם', so a column that
    was never stamped shows yesterday's price backwards while today's reads
    correctly. It runs over the whole of each column so rows added later are
    covered, and it runs BEFORE the values are written, because a value that
    lands in an unstamped cell is a wrong number until the second call returns.
    """
    targets = [col[k] for k in ("price", "prev") if k in col]
    sheet_id, rows = _grid(svc)
    before = sum(_unstamped(svc, c, rows) for c in targets)
    svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [{
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": rows,
                      "startColumnIndex": c, "endColumnIndex": c + 1},
            "cell": {"userEnteredFormat": {"textDirection": "LEFT_TO_RIGHT",
                                           "horizontalAlignment": "RIGHT"}},
            "fields": ("userEnteredFormat.textDirection,"
                       "userEnteredFormat.horizontalAlignment"),
        }} for c in targets]}).execute(num_retries=3)
    return before


def _grid(svc) -> tuple[int, int]:
    """(sheetId, rowCount) of the first tab — the one read_sheet's bare A1:Z hits."""
    sheet = svc.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(sheetId,gridProperties(rowCount)))",
    ).execute()["sheets"][0]["properties"]
    return sheet["sheetId"], sheet["gridProperties"]["rowCount"]


def _unstamped(svc, g: int, rows: int) -> int:
    """How many two-currency cells in this column render backwards right now."""
    data = svc.spreadsheets().get(
        spreadsheetId=SHEET_ID, includeGridData=True,
        ranges=[f"{col_letter(g)}2:{col_letter(g)}{rows}"],
        fields="sheets(data(rowData(values(userEnteredValue,"
               "userEnteredFormat/textDirection))))",
    ).execute()["sheets"][0]["data"][0].get("rowData", [])
    n = 0
    for rd in data:
        v = (rd.get("values") or [{}])[0]
        text = (v.get("userEnteredValue") or {}).get("stringValue", "")
        if text.strip() and (v.get("userEnteredFormat") or {}).get(
                "textDirection") != "LEFT_TO_RIGHT":
            n += 1
    return n


def write_updates(svc, col: dict[str, int], updates) -> int:
    data = [{"range": f"{col_letter(col[k])}{row}", "values": [[v]]}
            for row, k, v in updates if k in col]
    # RAW: nothing written here is a formula or a number for Sheets to
    # interpret, and RAW is the one mode that cannot turn '$2.57 (€2.21)'
    # into something else.
    for i in range(0, len(data), 200):
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"data": data[i:i + 200], "valueInputOption": "RAW"},
        ).execute(num_retries=3)
    return len(data)


# ── CLI ─────────────────────────────────────────────────────────────────────

def _describe(d: Decision, fx: float) -> str:
    r = d.row
    head = f"row {r.row:<4}{r.sku:<9}{r.country:<12}{r.code:<11}"
    if d.pick is None:
        why = {"gone": "GONE from catalogue", "regional": "REGIONAL code under a country SKU",
               "short": f"no {r.gb:g}GB with ≥{r.floor_days}d (has {', '.join(str(x) for x in d.have_days) or 'none'})"}[d.reason]
        return f"{head} ✗ {why}"
    v = d.pick
    old = f"€{r.eur:.2f}" if r.eur is not None else "—"
    days = f"{r.days}d" if r.days == v.days else f"{r.days}d→{v.days}d"
    mark = " " if r.eur is not None and abs(v.wholesale_eur - r.eur) < 0.001 else "$"
    held = f"   +{d.won_days}d for +{d.paid_up * 100:.0f}c" if d.won_days > 0 else ""
    return (f"{head} {mark} {old} → €{v.wholesale_eur:.2f}  {days:<9} "
            f"{price_cell(v.wholesale_eur, fx)}{held}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:            # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write to the sheet (default: dry run)")
    ap.add_argument("--feed", help="read a saved products.index.json (retail feed) instead of fetching")
    ap.add_argument("--plans", help="read a saved wholesale-API dump (JSON list of plans) instead of fetching")
    ap.add_argument("--credentials",
                    default=os.environ.get("SHEETS_CREDENTIALS",
                                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")))
    a = ap.parse_args(argv)

    key = os.environ.get("STELLAR_READ_KEY", "").strip()
    if a.plans:
        with open(a.plans, encoding="utf-8") as f:
            dump = json.load(f)
        cat = Catalogue.from_api(dump if isinstance(dump, list) else dump.get("data") or [])
        source = f"saved API dump {a.plans}"
    elif a.feed:
        with open(a.feed, encoding="utf-8") as f:
            cat = Catalogue.from_feed(json.load(f))
        source = f"saved retail feed {a.feed}"
    elif key:
        cat, source = Catalogue.from_api(fetch_plans(key)), "wholesale API — real cost"
    else:
        cat = Catalogue.from_feed(fetch_feed())
        source = f"public RETAIL feed / {RETAIL_OVER_WHOLESALE} — an ESTIMATE, no STELLAR_READ_KEY"
    age = cat.age_hours()
    print(f"📦 Stellar catalogue via {source}: {len(cat.variants)} fixed-data listings, "
          f"{len(cat.by_code)} package codes, {len(cat.regional_codes)} regional codes; "
          f"snapshot {cat.generated_at or '?'}"
          + (f" ({age:.1f}h old)" if age is not None else ""))
    if age is not None and age > FEED_STALE_HOURS:
        print(f"⚠️  feed snapshot is {age:.0f} hours old — prices below may already be stale")

    svc = sheets_service(a.credentials)
    rows, col = read_rows(read_sheet(svc))
    fx, fx_src = fetch_fx([r.price_cell for r in rows])
    print(f"💱 EUR→USD {fx:.4f} ({fx_src})")

    decisions = [d for d in (decide(cat, r) for r in rows) if d is not None]
    now = datetime.now()
    updates = plan_updates(decisions, fx, now.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d"))

    print(f"\n📋 {len(rows)} Stellar rows, {len(decisions)} with a code, "
          f"{len(rows) - len(decisions)} left alone\n")
    for d in decisions:
        print("  " + _describe(d, fx))
    stop = sanity(decisions)
    if stop:
        print(f"\n🛑 {stop}")
        return 2

    picked = [d for d in decisions if d.pick]
    repriced = sum(1 for d in picked if d.row.eur is not None and abs(d.pick.wholesale_eur - d.row.eur) > 0.001)
    redayed = sum(1 for d in picked if d.row.days != d.pick.days)
    refused = {k: sum(1 for d in decisions if d.reason == k) for k in ("gone", "short", "regional")}
    print(f"\n📊 priced {len(picked)} | price moved on {repriced} | days corrected on {redayed} | "
          f"gone {refused['gone']} | too short {refused['short']} | regional {refused['regional']} | "
          f"{len(updates)} cells")
    longer = [d for d in picked if d.won_days > 0]
    if longer:
        print(f"⏳ held the longer plan on {len(longer)} rows: "
              f"+{sum(d.won_days for d in longer)} days for +{sum(d.paid_up for d in longer) * 100:.0f} cents total")
        for d in longer:
            print(f"     row {d.row.row:<4}{d.row.sku:<9}{d.row.country:<12}{d.row.code:<11}"
                  f"{d.pick.days}d instead of {d.pick.days - d.won_days}d, +{d.paid_up * 100:.0f}c")

    if not a.apply:
        print("\n(dry run — nothing written; add --apply to write)")
        return 0
    n = write_updates(svc, col, updates)
    fixed = stamp_price_direction(svc, col)
    print(f"\n✅ wrote {n} cells"
          + (f"; corrected the text direction of {fixed} money cells" if fixed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
