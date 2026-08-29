#!/usr/bin/env python3
"""
Usage bot (bot #5) — runs every 4 hours in GitHub Actions.

Keeps every live package's consumption up to date in two places:
the receipts sheet (for you) and waverole.com (for the customer, who sees a
"1.2 of 5 GB used" bar on their order page without asking anyone).

How it stays cheap with hundreds of packages live at once:

  * Only rows whose Status column says the package is still worth checking
    are looked at. A finished package is never queried again.
  * The supplier's usage endpoint takes a LIST of eSIMs, so a hundred live
    packages cost ONE request, not a hundred.
  * The sheet is read once and written once, and the site is updated in
    batches — no per-customer round trips anywhere in the run.

When to stop checking a package (the Status column, R):
  · used up   — consumption reached the package size
  · finished  — its validity ran out. Counted with ONE SPARE DAY, so a
                30-day package keeps being checked for 31: time zones and
                the supplier's own clock disagree by hours, and a package
                cut off a few hours early would look "finished" to the
                customer while it still works.
  · active    — anything else; checked again on the next run.
A blank Status means "never checked yet", so existing rows join in on their
own with no migration.

Required environment (GitHub Secrets):
  GOOGLE_CREDENTIALS_JSON  service account with Editor on the receipts sheet
  ORDERS_TOKEN             bearer token of waverole.com/api/orders
  GMAIL_APP_PASSWORD       only used to email you if the run itself breaks
"""

import logging
import re
import sys
from datetime import datetime, timedelta, timezone

import gspread
import requests

from fulfillment_bot import (
    ORDERS_URL, RECEIPTS_SHEET_ID, TZ, alert, env, fetch_esim_details,
    fetch_usage, sheet_client, _redact, _row_time,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("usage")

# Status column values. Hebrew, because a person reads this sheet.
ACTIVE = "פעיל"
USED_UP = "נוצל"
EXPIRED = "הסתיים"
FAULT = "תקלה"                      # only ever written BY HAND, by the owner
# A blank cell means "not checked yet". FAULT is deliberately absent: when the
# owner marks a package faulty there is a reason behind it, and a sweep that
# later found the package merely spent would erase that reason. Leaving FAULT
# out of this set is what makes the status stick.
STILL_CHECK = {"", ACTIVE}

# Backgrounds for the status column, installed as CONDITIONAL FORMATTING
# rather than painted row by row: the colour then follows the text the instant
# the owner types it, with no bot run in between — which is exactly what was
# asked for ("if I wrote a fault and it did not go yellow, make it yellow").
STATUS_COLOURS = (
    ((ACTIVE,),           (0.78, 0.91, 0.79)),   # green  — still running
    ((USED_UP, EXPIRED),  (0.79, 0.87, 0.96)),   # blue   — spent or over
    ((FAULT,),            (1.00, 0.93, 0.66)),   # yellow — needs a person
)

GRACE_DAYS = 1          # the spare day described above
UNKNOWN_MAX_DAYS = 90   # cap for rows with no plan length (we sell max 30 days)
USAGE_CHUNK = 50        # eSIMs per supplier request
SITE_CHUNK = 100        # orders per site request
MAX_ICCID_LOOKUPS = 25  # repairs of rows missing an ICCID, per run

COL_ICCID = "מס סידורי -ICCID"
COL_STATUS = "סטטוס - Status"
COL_USAGE = "GB (0/X) - ניצול"
COL_ORDER = "מס׳ הזמנה"
COL_LINK = "Link - esim.dog"
COL_QR = "QR"
COL_DATE = "תאריך - Date"
COL_PLAN = "חבילה - Plan"
COL_WAVEROLE = "Link - waverole"
COL_ACTIVATED = "הופעל - Activated"
COL_SALE = "הנחה - Sale"
COL_SOURCE = "מקור - source"
COL_SELL = "מכירה - Sell"
COL_SKU = 'מק"ט - SUK'
ACTIVATED_YES = "Activated"
ACTIVATED_NO = "no"
# The owner's mark for "this customer paid full price". A blank cell is not
# the same statement: blank means nobody has looked yet, "-" means we looked
# and there was no discount. Only the first of those is safe to leave.
NO_DISCOUNT = "-"

# The price sheet's list price, which is what a discount is measured against.
PRICE_SHEET_ID = "108D3BUV-MNcIuRZuKUgb-E-b1Ra8moxWZZyI5JxnyRo"
COL_PRICE_SKU = "חבילה (קוד)"
COL_LIST_PRICE = "מחיר סופי"


def fetch_order_link(order_id: str) -> str:
    """The customer's own page link, from the site.

    It is a signed token, so it cannot be rebuilt from an order number — if a
    row lost it, this is the only way to get back in and see what the customer
    sees. Rows written by hand, or before the queue exposed the link, are
    missing it.
    """
    try:
        r = requests.get(ORDERS_URL, params={"order_id": order_id},
                         headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"},
                         timeout=20)
        if r.status_code == 404:
            return ""                    # older than the site's 90-day record
        r.raise_for_status()
        return str(r.json().get("order_url") or "")
    except Exception as e:
        log.warning(f"could not look up {order_id}: {_redact(str(e))}")
        return ""


def _parse_expiry(s: str):
    """The supplier's expiry date, ALWAYS as an aware datetime.

    Its providers do not agree on a format: some send an offset
    ('2026-07-23T22:03:18+0000'), others send none at all
    ('2026-08-31T09:43:27'). A date without an offset parses to a naive
    datetime, and comparing that to `now` raises

        TypeError: can't compare offset-naive and offset-aware datetimes

    which killed the whole daily run — every customer's meter froze because
    of one date string. It stayed hidden until a customer first ACTIVATED an
    eSIM on such a provider, since before activation the expiry comes back
    null and this branch is never reached.

    A missing offset is read as UTC: the same supplier stamps its other dates
    '+0000' and 'Z', so UTC is its house clock. Even if a provider meant local
    time somewhere, GRACE_DAYS covers the difference — which is what that
    spare day is for.
    """
    s = str(s or "").strip()
    if not s:
        return None
    parsed = None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_usage_cell(text: str) -> dict | None:
    """Read the sheet's own "used / total" figure, e.g. "0.076 / 1".

    Used as the fallback when the supplier declines to report on a package,
    so a finished package still shows the customer its final reading instead
    of an empty meter.
    """
    m = re.match(r"\s*([\d.]+)\s*/\s*([\d.]+)\s*$", str(text or ""))
    if not m:
        return None
    try:
        used, total = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    if total <= 0 or used < 0:
        return None
    return {"used_gb": used, "total_gb": total, "expires": None}


def _plan_days(row_plan: str) -> int | None:
    """Days out of a plan label like '1GB - 30 days — Cellcom'."""
    m = re.search(r"[-–]\s*(\d+)\s*(?:days?|d\b|ימים)", str(row_plan or ""), re.I)
    return int(m.group(1)) if m else None


def decide_status(usage: dict | None, bought_at, plan_days: int | None, now=None) -> str:
    """Whether this package is still worth checking tomorrow.

    `usage` is the supplier's reading, or None when it does not know this
    eSIM. Falling back to the sheet's own dates matters: an eSIM the supplier
    has forgotten must not stay in the daily sweep forever.
    """
    now = now or datetime.now(timezone.utc)

    def too_old(limit_days: int) -> bool:
        if not bought_at:
            return False
        return now - bought_at.astimezone(timezone.utc) > timedelta(days=limit_days)

    if usage:
        if usage["total_gb"] > 0 and usage["used_gb"] >= usage["total_gb"]:
            return USED_UP
        exp = _parse_expiry(usage.get("expires", ""))
        if exp:
            return EXPIRED if now > exp + timedelta(days=GRACE_DAYS) else ACTIVE
        if usage.get("status") in ("used_expired", "expired", "used_up"):
            return EXPIRED
        # Answered, but with no expiry date at all — which is the NORM for
        # some of the supplier's providers, not a passing state. Read alone
        # that means "still active", so these packages were asked about every
        # day forever: the sweep would have grown without limit as customers
        # accumulated, and every one of them stayed on the customer's page as
        # a live package long after it died.
        #
        # No date is still a real possibility that the customer never
        # installed it, so its clock never started — retiring at the plan's
        # own length would cut those off early. The 90-day cap is the
        # compromise: far past any package we sell, but finite.
        return EXPIRED if too_old(UNKNOWN_MAX_DAYS) else ACTIVE

    # The supplier does not know it. Retire it once its own validity is past,
    # otherwise keep it (a fresh order may not have registered yet).
    # No plan length on the row (older rows stored only a network name)? The
    # longest package we sell is 30 days, so UNKNOWN_MAX_DAYS is far past any
    # real expiry — without a cap those rows would be queried every day forever.
    return (EXPIRED if too_old((plan_days + GRACE_DAYS) if plan_days else UNKNOWN_MAX_DAYS)
            else ACTIVE)


def push_to_site(items: list[dict]) -> int:
    """Send consumption to waverole.com so the order pages show it."""
    done = 0
    token = env("ORDERS_TOKEN")
    for i in range(0, len(items), SITE_CHUNK):
        chunk = items[i:i + SITE_CHUNK]
        try:
            r = requests.post(ORDERS_URL, json={"action": "usage_batch", "items": chunk},
                              headers={"Authorization": f"Bearer {token}"}, timeout=60)
            r.raise_for_status()
            body = r.json()
            done += len(body.get("updated") or [])
            missing = body.get("not_found") or []
            if missing:
                # Normal for orders older than the site's 90-day record, and
                # for anything bought before the site existed.
                log.info(f"{len(missing)} order(s) are no longer on the site — sheet only")
        except Exception as e:
            log.warning(f"site usage update failed for {len(chunk)} order(s): {_redact(str(e))}")
    return done


# ── discount, activation, colour ─────────────────────────────────────────────

def _money(text) -> float | None:
    """'6.33$' / '$6.33' / '0' -> a number. None when the cell holds no figure.

    Zero is a real, meaningful price here (a package given away), so the caller
    must test for None and never for falsiness.
    """
    m = re.search(r"\d+(?:\.\d+)?", str(text or "").replace(",", ""))
    return float(m.group()) if m else None


def list_prices() -> dict:
    """SKU -> list price, from the price sheet's 'מחיר סופי'.

    A discount is what the customer did NOT pay against the advertised price,
    so it can only be measured against that sheet — the receipt itself records
    what was charged and has no memory of the sticker price.
    """
    try:
        ws = sheet_client().open_by_key(PRICE_SHEET_ID).sheet1
        rows = ws.get_all_values()
    except Exception as e:
        log.warning(f"could not read the price sheet for list prices: {_redact(str(e))}")
        return {}
    if not rows:
        return {}
    hdr = [h.strip() for h in rows[0]]
    if COL_PRICE_SKU not in hdr or COL_LIST_PRICE not in hdr:
        log.warning(f"price sheet has no {COL_PRICE_SKU!r}/{COL_LIST_PRICE!r} column")
        return {}
    si, pi = hdr.index(COL_PRICE_SKU), hdr.index(COL_LIST_PRICE)
    out = {}
    for r in rows[1:]:
        sku = (r[si] if len(r) > si else "").strip()
        price = _money(r[pi] if len(r) > pi else "")
        if sku and price:
            out[sku] = price
    return out


def discount_text(sku: str, sell, prices: dict) -> str:
    """What belongs in 'הנחה - Sale' for this row, or '' to leave it alone.

    Owner's format (2026-08-29): "100% ($6.33 הנחה)" — the percentage, and the
    money the customer actually saved. A price of 0 is always 100% off, which
    is the one case that needs no list price to state.
    """
    paid = _money(sell)
    if paid is None:
        return ""
    listed = prices.get((sku or "").strip())
    if paid == 0:
        # Written by hand for a giveaway. Say 100% even when the price sheet
        # has since dropped the SKU, but only name a figure we actually know.
        return f"100% (${listed:.2f} הנחה)" if listed else "100%"
    if listed is None:
        return ""        # no list price to measure against — say nothing
    if paid >= listed:
        # Paid the sticker price (or more, after the scraper marked it down
        # since): no discount was given, and that is a fact, not a guess.
        # This is the case the column was mostly missing — every ordinary
        # full-price sale sat blank, so "-" only ever appeared by hand.
        return NO_DISCOUNT
    # UNDER the list price is the one ambiguous case, and it is left alone
    # deliberately. 'מחיר סופי'
    # is rewritten by the scraper every day, so measuring a sale from three
    # weeks ago against today's list price invents discounts that were never
    # given: a dry run over the real sheet turned eleven rows sold at $6.00
    # against a $6.49 list into "8% off", which is price drift, not a
    # discount. A partial discount is only truthfully known at the moment of
    # sale — it belongs to whatever records the sale, or to the owner's own
    # hand — never to a sweep guessing backwards.
    return ""


def activation_text(usage: dict | None, current: str,
                    sheet_usage: dict | None = None) -> str:
    """'Activated' once the package has moved any data at all, else 'no'.

    Deliberately one-way. An eSIM that has been installed cannot become
    uninstalled, and the supplier goes quiet on spent packages (see the usage
    fallback below) — so a reading that has stopped arriving must never be
    allowed to rewrite a package's history back to 'no'.
    """
    if (current or "").strip().lower() == ACTIVATED_YES.lower():
        return ACTIVATED_YES
    # The supplier's answer first, then the figure already in the sheet. The
    # sheet matters because the supplier goes quiet on spent packages: a row
    # reading "18 / 20" with no live answer was being stamped 'no', which
    # says a package nobody could have used moved 18GB.
    for u in (usage, sheet_usage):
        if u and u.get("used_gb", 0) > 0:
            return ACTIVATED_YES
    return ACTIVATED_NO


def _a1_col(idx0: int) -> str:
    """0-based column index -> 'A', 'Z', 'AA'. The owner keeps adding columns,
    and a formula built with chr(65+i) turns to punctuation past column Z."""
    out, n = "", idx0 + 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(ord('A') + rem) + out
    return out


def ensure_status_colours(ws, col_index: int) -> None:
    """Install the status column's background rules, replacing our own.

    Conditional formatting rather than painted cells: the owner types 'תקלה'
    and the row is yellow immediately, without waiting for this bot to run —
    and a colour that is a rule cannot drift out of step with the text the way
    a painted one does.
    """
    sheet_id = ws.id
    rng = {"sheetId": sheet_id, "startRowIndex": 1,
           "startColumnIndex": col_index, "endColumnIndex": col_index + 1}
    try:
        existing = ws.spreadsheet.fetch_sheet_metadata().get("sheets", [])
        current = next((sh.get("conditionalFormats", []) for sh in existing
                        if sh["properties"]["sheetId"] == sheet_id), [])
    except Exception as e:
        log.warning(f"could not read existing colour rules: {_redact(str(e))}")
        return

    # Drop only the rules that sit on this column, so anything the owner added
    # elsewhere in the sheet survives untouched. Deleting back-to-front keeps
    # the indexes of the not-yet-deleted rules valid.
    requests = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}}
                for i, rule in reversed(list(enumerate(current)))
                if any(r.get("startColumnIndex") == col_index for r in rule.get("ranges", []))]

    for i, (labels, (r, g, b)) in enumerate(STATUS_COLOURS):
        requests.append({"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [rng],
            "booleanRule": {
                "condition": {"type": "TEXT_EQ" if len(labels) == 1 else "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": labels[0]}] if len(labels) == 1
                              else [{"userEnteredValue":
                                     "=OR(" + ",".join('$%s2="%s"' % (_a1_col(col_index), l)
                                                       for l in labels) + ")"}]},
                "format": {"backgroundColor": {"red": r, "green": g, "blue": b}},
            }}}})
    try:
        ws.spreadsheet.batch_update({"requests": requests})
        log.info(f"status colours: {len(STATUS_COLOURS)} rule(s) in place")
    except Exception as e:
        log.warning(f"could not install status colours: {_redact(str(e))}")


def main() -> int:
    ws = sheet_client().open_by_key(RECEIPTS_SHEET_ID).sheet1
    rows = ws.get_all_values()
    if not rows:
        log.info("receipts sheet is empty")
        return 0
    hdr = [h.strip() for h in rows[0]]
    missing = [c for c in (COL_ICCID, COL_STATUS, COL_USAGE, COL_ORDER) if c not in hdr]
    if missing:
        alert("Usage bot cannot read the receipts sheet",
              f"These columns are missing: {missing}\n"
              "Add them back (or tell me the new names) — until then usage is not updated.")
        return 1
    idx = {c: hdr.index(c) for c in hdr}
    cell = lambda r, c: (r[idx[c]].strip() if c in idx and len(r) > idx[c] else "")

    ensure_status_colours(ws, idx[COL_STATUS])

    # ── one-off: send the sheet's figures to the site and stop ──
    # For rows retired before the site was ever told their final reading. They
    # are skipped by the sweep below forever, so their customers were left
    # looking at an empty meter with no way for it to ever fill in.
    if "--backfill" in sys.argv:
        items = []
        for r in rows[1:]:
            oid = cell(r, COL_ORDER)
            if oid and (u := _parse_usage_cell(cell(r, COL_USAGE))):
                items.append({"order_id": oid, **u})
        log.info(f"backfill: {len(items)} row(s) with a usage figure")
        log.info(f"site: {push_to_site(items)} order page(s) updated")
        return 0

    # ── who is still worth checking ──
    todo = []
    for n, r in enumerate(rows[1:], start=2):
        if not cell(r, COL_ORDER):
            continue
        if cell(r, COL_STATUS) not in STILL_CHECK:
            continue                                  # finished — never again
        todo.append({
            "row": n,
            "order_id": cell(r, COL_ORDER),
            "iccid": re.sub(r"\D", "", cell(r, COL_ICCID)),
            "link": cell(r, COL_LINK),
            "qr": cell(r, COL_QR),
            "status": cell(r, COL_STATUS),
            "usage_cell": cell(r, COL_USAGE),
            "bought_at": _row_time(cell(r, COL_DATE)),
            "plan_days": _plan_days(cell(r, COL_PLAN)),
            "waverole": cell(r, COL_WAVEROLE),
            "activated": cell(r, COL_ACTIVATED),
        })
    log.info(f"{len(todo)} package(s) still being checked "
             f"(out of {len(rows) - 1} row(s) in the sheet)")
    if not todo:
        return 0

    # ── rows missing the facts this sweep runs on: recover them ──
    # Both from the supplier link and from the QR column, because rows written
    # before the columns were straightened out keep their order link there.
    # Anything we learn is written back, so a row is only ever repaired once.
    repairs = [t for t in todo if not (t["iccid"] and t["plan_days"])
               and (t["link"] or t["qr"])][:MAX_ICCID_LOOKUPS]
    for t in repairs:
        got = fetch_esim_details([u for u in (t["link"], t["qr"]) if u])
        if not got:
            continue
        if got.get("iccid") and not t["iccid"]:
            t["iccid"] = t["new_iccid"] = re.sub(r"\D", "", got["iccid"])
        # The plan length decides when to stop checking a package the usage
        # endpoint has never heard of; without it the row lingers for months.
        if got.get("plan_days") and not t["plan_days"]:
            t["plan_days"] = int(got["plan_days"])
            gb = got.get("plan_gb")
            t["new_plan"] = (f"{gb:g}GB - {t['plan_days']} days"
                             + (f" — {got['networks']}" if got.get("networks") else "")) if gb else ""
        log.info(f"row {t['row']}: filled in missing details from its order link")

    # ── rows with no customer link: ask the site for it ──
    for t in todo:
        if not t["waverole"] and COL_WAVEROLE in idx:
            if link := fetch_order_link(t["order_id"]):
                t["new_waverole"] = link
                log.info(f"row {t['row']}: recovered the customer's page link")

    # ── one supplier request per 50 packages ──
    usage = {}
    known = [t["iccid"] for t in todo if t["iccid"]]
    for i in range(0, len(known), USAGE_CHUNK):
        usage.update(fetch_usage(known[i:i + USAGE_CHUNK]))
    log.info(f"the supplier reported on {len(usage)} of {len(known)} package(s)")

    # ── decide, then write the sheet ONCE ──
    cells, site_items, counts = [], [], {ACTIVE: 0, USED_UP: 0, EXPIRED: 0}
    skipped = 0
    for t in todo:
        u = usage.get(t["iccid"]) if t["iccid"] else None
        # One package must never take the sweep down with it. A single
        # unreadable date from the supplier used to raise out of the whole
        # run, so NOBODY's meter was updated — the blast radius of one bad
        # string was every customer. Skip the row, keep going, say so at the
        # end.
        try:
            status = decide_status(u, t["bought_at"], t["plan_days"])
        except Exception as e:
            skipped += 1
            log.warning(f"row {t['row']} ({t['order_id']}): skipped — {type(e).__name__}: {e}")
            continue
        counts[status] += 1

        if t.get("new_iccid"):
            cells.append(gspread.Cell(t["row"], idx[COL_ICCID] + 1, f"'{t['new_iccid']}"))
        if t.get("new_plan") and COL_PLAN in idx:
            cells.append(gspread.Cell(t["row"], idx[COL_PLAN] + 1, t["new_plan"]))
        if t.get("new_waverole"):
            cells.append(gspread.Cell(t["row"], idx[COL_WAVEROLE] + 1, t["new_waverole"]))
        sheet_u = _parse_usage_cell(t["usage_cell"])
        if u:
            text = f"{u['used_gb']:g} / {u['total_gb']:g}"
            if text != t["usage_cell"]:
                cells.append(gspread.Cell(t["row"], idx[COL_USAGE] + 1, text))
            site_items.append({"order_id": t["order_id"], "used_gb": u["used_gb"],
                               "total_gb": u["total_gb"], "expires": u["expires"]})
        elif sheet_u:
            # The supplier went quiet — which it does precisely when a package
            # is spent, the moment the customer most wants to see where they
            # stand. Send what the sheet already knows rather than nothing:
            # the customer's meter showed BLANK for every finished package
            # because this push only ever ran when the supplier answered.
            site_items.append({"order_id": t["order_id"], **sheet_u})
        if COL_ACTIVATED in idx:
            was = t.get("activated", "")
            now_txt = activation_text(u, was, sheet_u)
            if now_txt != was:
                cells.append(gspread.Cell(t["row"], idx[COL_ACTIVATED] + 1, now_txt))

        if status != t["status"]:
            cells.append(gspread.Cell(t["row"], idx[COL_STATUS] + 1, status))

    # ── discounts: every row, including the retired ones ──
    # The sweep above only visits packages still worth checking, but the owner
    # settles a price by hand whenever it suits — often on a package that
    # finished weeks ago. Reading the whole sheet here is what lets "I typed 0
    # in the price column" turn into "100%" without anything else happening.
    if COL_SALE in idx and COL_SELL in idx:
        prices = list_prices()
        n_sale = 0
        for n, r in enumerate(rows[1:], start=2):
            if not cell(r, COL_ORDER):
                continue
            want = discount_text(cell(r, COL_SKU), cell(r, COL_SELL), prices)
            # BLANK cells only. The owner keeps this column by hand, and the
            # same rule that protects a hand-written 'תקלה' from the sweep
            # protects a hand-written discount: a person had a reason, and a
            # sweep that overwrites it erases the reason. Filling in a blank
            # states something nobody had stated yet; rewriting a filled cell
            # contradicts someone. To have the bot redo a row, clear it.
            if want and not cell(r, COL_SALE):
                cells.append(gspread.Cell(n, idx[COL_SALE] + 1, want))
                n_sale += 1
        if n_sale:
            log.info(f"discount column: {n_sale} row(s) corrected")

    # ── source: the one column no bot can honestly fill ──
    # It records how WE paid the supplier. The purchase bot stamps the rows it
    # creates; a package bought by hand never reaches that code, so the owner
    # fills those. Guessing here would invent a payment method, so this only
    # names the rows waiting on a person — deliberately a log line and not an
    # alert, because a blank the owner has chosen to leave must not nag.
    if COL_SOURCE in idx:
        blank = [cell(r, COL_ORDER) for r in rows[1:]
                 if cell(r, COL_ORDER) and not cell(r, COL_SOURCE)]
        if blank:
            log.info(f"source column: {len(blank)} row(s) still blank, for the "
                     f"owner to fill — {', '.join(blank[:10])}"
                     + (" ..." if len(blank) > 10 else ""))

    if cells:
        ws.update_cells(cells, value_input_option="USER_ENTERED")
    log.info(f"sheet: {len(cells)} cell(s) updated · still active {counts[ACTIVE]} · "
             f"used up {counts[USED_UP]} · finished {counts[EXPIRED]}"
             + (f" · SKIPPED {skipped}" if skipped else ""))
    if skipped:
        alert("Usage bot skipped some packages",
              f"{skipped} of {len(todo)} package(s) could not be read this run "
              "(details in the GitHub Actions log). Everyone else was updated "
              "normally — those rows keep their previous figure.")

    if site_items:
        log.info(f"site: {push_to_site(site_items)} order page(s) now show current usage")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # A silent failure here means customers' usage bars quietly freeze.
        log.exception("usage run failed")
        alert("Usage bot failed", f"{type(e).__name__}: {_redact(str(e))}\n\n"
              "Usage bars on order pages will be stale until the next run.")
        sys.exit(1)
