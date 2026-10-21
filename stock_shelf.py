#!/usr/bin/env python3
"""
Stock shelf -- second packages onto the receipts spreadsheet's stock tab.

Born 2026-10-21, with the owner's new rule: speed for the customer beats
avoiding a double purchase. An uncertain payment is waited on for ten minutes
and then bought AGAIN, automatically, once -- so sometimes an order ends up
with two packages: the re-buy serves the customer, and the first payment's
bank approval lands after all (or the other way round). That is acceptable,
on three conditions, and this module is the third:

  * the customer has ONE eSIM on their page and gets ONE email
    (api/orders.js: sessions, extras, the mail lease; fulfillment_bot.send_once);
  * the extra package is never served, never mailed (the site parks it);
  * it goes onto the stock tab as a free spare -- where the PC bot serves the
    next order for the same SKU from it instead of buying (bot/spares.take) --
    and the owner is told, ONCE, with what to do if he would rather have the
    money back.

Why here, and why only here. The PC bot used to be the only writer of the
stock tab, by hand ("python -m bot.spares add <row>"), and the site mailed the
owner a "Spare eSIM?" alert for every parked package. But the PC may be off,
and an alert that asks a person to copy six cells by hand is how a package
ends up nowhere (memory: duplicate-guard-and-stock -- the 09-08 packages sat
in a mailbox for days). The cloud runs every five minutes whatever the PC is
doing, already holds the receipts sheet, and is where the delivery mail of a
second package lands. So: one writer, one alert, from here. The PC's hand
path (bot/spares.add / add_from_receipt) stays for packages nobody parked.

Stateless, like everything in this repo. What has been shelved lives in two
places that cannot be wrong about it: the tab itself (an ICCID -- or, for a
Stellar package, which has none, its Session -- already on it is never added
again), and the site's extra record (extra_shelved). The row is written
FIRST and the site told after, so a run killed in between re-tells the site
next time and appends nothing.

Secrecy: this repo is PUBLIC and so are its Actions logs. Nothing here logs a
full ICCID or a session id; the owner's alert carries tails only. The stock
row itself carries the full ICCID and session -- it has to, it is how the
package is served -- and lives in the owner's private spreadsheet.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import requests

import fulfillment_bot as fb

log = logging.getLogger("stock-shelf")

# The tab and its columns, spelled exactly as the PC bot spells them
# (bot/spares.py TAB / HEADER, FREE) -- the PC reads what this writes, by
# header NAME, so a column moved by hand keeps working on both sides. In \u
# escapes on purpose (memory: hebrew-in-code-encoding).
STOCK_TAB = "\u05de\u05dc\u05d0\u05d9 - Stock"
HEADER = [
    "ICCID",
    '\u05de\u05e7"\u05d8 - SKU',
    "\u05d7\u05d1\u05d9\u05dc\u05d4 - Plan",
    "Session",
    "\u05e0\u05d5\u05e1\u05e3 - Added",
    "\u05e2\u05dc\u05d5\u05ea - Cost",
    "\u05de\u05d0\u05d9\u05e4\u05d4 - Origin",
    "\u05e1\u05d8\u05d8\u05d5\u05e1 - Status",
    "\u05e0\u05de\u05e1\u05e8 \u05dc\u05d4\u05d6\u05de\u05e0\u05d4 - Served order",
    "\u05e0\u05de\u05e1\u05e8 \u05dc\u05dc\u05e7\u05d5\u05d7 - Served to",
    "\u05e0\u05de\u05e1\u05e8 \u05d1\u05ea\u05d0\u05e8\u05d9\u05da - Served at",
    "\u05d4\u05e2\u05e8\u05d4 - Note",
]
(H_ICCID, H_SKU, H_PLAN, H_SESSION, H_ADDED, H_COST, H_ORIGIN, H_STATUS,
 _H_SERVED_ORDER, _H_SERVED_TO, _H_SERVED_AT, H_NOTE) = HEADER
FREE = "\u05e4\u05e0\u05d5\u05d9"                      # 'free' -- the next order of its SKU gets it

# A Stellar package has no ICCID (Stellar never returns one) and no esim.dog
# session the PC could hand over, so its row names the Stellar order instead.
# bot/spares.available() serves only esim.dog sessions, so this row is never
# handed to a customer by the PC -- the owner serves it by hand, if at all.
STELLAR_SESSION = "stellar:"
STELLAR_NOTE = "Stellar package - serve by hand"

# Receipts columns read here (the matcher's spellings, from fulfillment_bot).
R_SKU, R_GB, R_PLAN, R_REGION = ('\u05de\u05e7"\u05d8 - SUK', "\u05d0\u05d9\u05d7\u05e1\u05d5\u05df - GB",
                                 "\u05d7\u05d1\u05d9\u05dc\u05d4 - Plan", "\u05d0\u05d6\u05d5\u05e8 - Region")


class ShelfError(Exception):
    """The stock tab cannot be written safely (a renamed column). Nothing is
    appended and the owner is told; the extras stay listed for the next run."""


# ── the site ─────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {"Authorization": f"Bearer {fb.env('ORDERS_TOKEN')}"}


def extras_list() -> list[dict]:
    """The site's unshelved second packages (api/orders.js ?status=extras), or
    [] when there are none -- or when the site cannot say: a site from before
    2026-10-21 answers 400, and a run that cannot read the list shelves
    nothing and loses nothing (the next run reads it again)."""
    try:
        r = requests.get(fb.ORDERS_URL, params={"status": "extras"}, headers=_headers(), timeout=20)
        if r.status_code == 400:
            log.info("the site predates the extras list - nothing to shelve")
            return []
        r.raise_for_status()
        out = r.json()
    except Exception as e:
        log.warning(f"could not read the extras list: {fb._redact(str(e))}")
        return []
    orders = (out or {}).get("orders") if isinstance(out, dict) else None
    return [o for o in (orders or []) if isinstance(o, dict) and o.get("extras")]


def mark_shelved(order_id: str, extra: dict, shelf_row: int = 0, refused: str = "") -> bool:
    """Tell the site the extra is on the shelf (or, `refused`, that it must
    never be): it leaves the extras list. Idempotent on the site. False when
    the site could not be told -- the next run finds the row on the tab and
    tells it again."""
    body = {"action": "extra_shelved", "order_id": order_id}
    for k in ("sid", "iccid", "supplier_ref"):
        if extra.get(k):
            body[k] = extra[k]
    if shelf_row:
        body["shelf_row"] = int(shelf_row)
    if refused:
        body["refused"] = refused[:80]
    try:
        r = requests.post(fb.ORDERS_URL, json=body, headers=_headers(), timeout=20)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning(f"order {order_id}: could not tell the site the extra was shelved: {fb._redact(str(e))}")
        return False


# ── the tab ──────────────────────────────────────────────────────────────────

def stock_ws(book):
    """The stock tab, created with the PC bot's header if it is not there yet.
    A tab whose header lost a column is refused: writing by position into a
    renamed column puts an ICCID where the PC reads a status."""
    try:
        ws = book.worksheet(STOCK_TAB)
    except Exception:
        ws = book.add_worksheet(title=STOCK_TAB, rows=200, cols=len(HEADER))
        ws.update(range_name="A1", values=[HEADER], value_input_option="RAW")
        log.info("created the stock tab in the receipts spreadsheet")
        return ws
    header = [h.strip() for h in ws.row_values(1)]
    missing = [h for h in HEADER if h not in header]
    if missing:
        raise ShelfError(f"the stock tab is missing column(s) {missing}")
    return ws


def _tab_rows(ws) -> tuple[list[str], list[dict]]:
    vals = ws.get_all_values()
    if not vals:
        return list(HEADER), []
    header = [h.strip() for h in vals[0]]
    rows = []
    for n, r in enumerate(vals[1:], start=2):
        r = list(r) + [""] * (len(header) - len(r))
        d = {h: r[i].strip() for i, h in enumerate(header) if h}
        d["_row"] = n
        rows.append(d)
    return header, rows


def _digits(s) -> str:
    return re.sub(r"[^0-9]", "", str(s or ""))


def session_cell(order: dict, extra: dict) -> str:
    """What the row's Session cell says: the bare esim.dog session (the PC
    turns it into a success link, bot/spares.success_url), or 'stellar:<ref>'."""
    sup = str(extra.get("supplier") or order.get("supplier") or "esim.dog").lower()
    if sup == "stellar" and extra.get("supplier_ref"):
        return STELLAR_SESSION + str(extra["supplier_ref"])
    return str(extra.get("sid") or "")


def already_on_tab(rows: list[dict], iccid: str, session: str) -> dict | None:
    """The tab row already holding this package -- by ICCID first (the PC's
    own dedupe key), else by Session for a package that has no ICCID."""
    d = _digits(iccid)
    for r in rows:
        if d and _digits(r.get(H_ICCID)) == d:
            return r
    if session:
        for r in rows:
            if str(r.get(H_SESSION) or "").strip() == session:
                return r
    return None


def receipts_row_for(values: list, extra: dict) -> dict | None:
    """The receipts row that recorded this package's PURCHASE, by its session
    (live, '#payment-uncertain' or defused 'superseded-session:'), or by the
    Stellar order in its supplier link. {n, sku, plan, buy, status} -- the
    cost and the plan come from it. None when no row names it (a purchase the
    PC never wrote down: the site's own buy price is used instead)."""
    if not values:
        return None
    hdr = [h.strip() for h in values[0]]
    idx = lambda name: hdr.index(name) if name in hdr else None
    cols = [i for i in (idx(fb.H_LINK_SUP), idx(fb.H_QR)) if i is not None]
    sid, ref = str(extra.get("sid") or ""), str(extra.get("supplier_ref") or "")
    if not cols or not (sid or ref):
        return None
    for n, r in enumerate(values[1:], start=2):
        get = lambda i: r[i].strip() if i is not None and len(r) > i else ""
        hit = False
        for c in cols:
            cell = get(c)
            if sid and (sid in fb._session_ids(cell) or
                        sid in {m.group(1) for m in fb.SUPERSEDED_RE.finditer(cell)}):
                hit = True
            if ref and re.search(r"/orders/" + re.escape(ref) + r"(?:[/?#]|$)", cell):
                hit = True
        if not hit:
            continue
        gb, plan_txt, region = get(idx(R_GB)), get(idx(R_PLAN)), get(idx(R_REGION))
        first = f"{gb}{(' / ' + plan_txt) if plan_txt else ''}" if (gb or plan_txt) else ""
        return {"n": n, "order_id": get(idx(fb.H_ORDER)), "sku": get(idx(R_SKU)),
                "plan": " \u00b7 ".join(p for p in (first, region) if p),
                "buy": get(idx(fb.H_BUY)), "status": get(idx(fb.H_STATUS))}
    return None


def _money(text) -> float | None:
    """'3.86$' / '$3.86' / '3.86' -> 3.86. The receipts cells are dollars by
    construction; a bare number from the supplier's plan is too."""
    m = re.search(r"\d+(?:\.\d+)?", str(text or "").replace(",", ""))
    try:
        return float(m.group(0)) if m else None
    except ValueError:
        return None


def _local(ts: str, fmt: str = "%d/%m %H:%M") -> str:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(fb.TZ).strftime(fmt)
    except (TypeError, ValueError):
        return ""


def _append(ws, header: list, values: dict) -> int:
    """One row at the end, by an explicit A{n} write and read back once --
    never ws.append_row, whose table detection once shifted a receipts row 18
    columns right (memory: esim-bot-project). The PC can add a row to this tab
    by hand at the same moment, so a row taken from under us is written again
    at the new end (stellar_buyer.append_row, the same reasoning)."""
    row = [str(values.get(h, "")) for h in header]
    # The package's own key is the stamp: two extras of one order share an
    # Origin, never an ICCID (or, for a Stellar package, a Session).
    key_col = header.index(H_ICCID if values.get(H_ICCID) else H_SESSION)
    stamp = row[key_col]
    n = 0
    for last in (False, True):
        n = len(ws.get_all_values()) + 1
        ws.update(range_name=f"A{n}", values=[row], value_input_option="RAW")
        if last:
            break
        back = ws.get_all_values()
        got = back[n - 1] if len(back) >= n else []
        if len(got) > key_col and got[key_col].strip() == stamp:
            break
        log.warning(f"stock row {n} was taken by another writer - appending again")
    return n


# ── the one alert ────────────────────────────────────────────────────────────

def _how(order: dict, extra: dict) -> str:
    """One line from the site's sessions of the order: which payment was which,
    in the owner's words. Tails and times only."""
    words = {"first": "first payment", "auto_rebuy": "automatic re-buy", "owner": "bought by hand",
             "spare": "served from stock"}
    mine = str(extra.get("sid") or "")
    parts = []
    for s in order.get("sessions") or []:
        tail, st = str(s.get("sid_tail") or ""), str(s.get("state") or "")
        if st == "serving":
            state = "the customer's"
        elif st == "extra":
            state = "this package" if tail and mine.endswith(tail) else "a second package"
        else:
            state = {"gave_up": "never delivered", "waiting": "not delivered yet"}.get(st, "")
        parts.append(f"{words.get(str(s.get('attempt') or ''), 'payment')} ...{tail or '?'} at "
                     f"{_local(s.get('ts')) or '?'}" + (f" ({state})" if state else ""))
    line = "; ".join(parts)
    got = f"this package reported {_local(extra.get('ts')) or '?'} via {extra.get('via') or '?'}"
    return f"{line}; {got}" if line else got


def shelf_alert(order: dict, extra: dict, shelf_row: int, rrow: dict | None, cost: float | None):
    oid, sku = str(order.get("order_id") or "?"), str(order.get("sku") or "?")
    stellar = str(extra.get("supplier") or order.get("supplier") or "").lower() == "stellar"
    iccid_tail = _digits(extra.get("iccid"))[-4:]
    ref_tail = str(extra.get("supplier_ref") or "")[-8:]
    sid_tail = str(extra.get("sid") or "")[-10:]
    pkg = (f"Stellar order ...{ref_tail}" if stellar else f"ICCID ...{iccid_tail or '?'}"
           + (f" (session ...{sid_tail})" if sid_tail else ""))
    if order.get("replaced_by"):
        first = (f"Order {oid} ({sku}) was replaced by {order['replaced_by']}, and a package for it was "
                 f"delivered afterwards. The customer has their eSIM on {order['replaced_by']}'s page; "
                 f"this one was NOT shown or mailed to them.")
    elif order.get("serving_iccid_tail") or order.get("status") == "fulfilled":
        first = (f"Order {oid} ({sku}) was bought twice. The customer has ONE eSIM on their page "
                 f"and got ONE email.\n\nCustomer's package: ICCID ...{str(order.get('serving_iccid_tail') or '?')[-4:]}"
                 + (f" (session ...{order['serving_sid_tail']})" if order.get("serving_sid_tail") else ""))
    else:
        first = (f"Order {oid} ({sku}) has no eSIM on its page, and this package of it was parked as "
                 f"nobody's. If the customer is still owed one, the next purchase of {sku} - a re-buy "
                 f"of this very order included - is served from this spare.")
    cost_line = (f"Its cost: ${cost:.2f}" if cost is not None else "Its cost: unknown") + (
        f" (receipts row {rrow['n']}, '{rrow.get('status') or '-'}')" if rrow else " (no receipts row names it)")
    if stellar:
        todo = ("This is a Stellar package: the PC does not serve it by itself. Serve it by hand from the "
                f"Stellar portal, or ask Stellar to refund order ...{ref_tail} and set the stock row's "
                f"Status to the refunded word.")
    else:
        todo = (f"Nothing to do: the next order for {sku} is served from this spare.\n"
                f"For the money back instead: ask esim.dog to refund ICCID ...{iccid_tail or '?'}, then on the PC:\n"
                f"  python -m bot.spares remove {iccid_tail or '<ICCID digits>'} refund requested")
    fb.alert(f"{oid}: a second package was bought - it is on the stock shelf",
             f"{first}\n\nSecond package: {pkg} -> '{STOCK_TAB}' row {shelf_row}, free\n"
             f"How: {_how(order, extra)}\n{cost_line}\n\n{todo}")


def refusal_alert(order: dict, extra: dict, why: str):
    oid = str(order.get("order_id") or "?")
    fb.alert(f"{oid}: a parked package was NOT put on the stock shelf",
             f"Order {oid} ({order.get('sku') or '?'}) has a parked package that this bot refused to "
             f"shelve: {why}.\n\nShelving the eSIM the customer is using would sell the next customer "
             f"a package somebody already has. Compare the order's page with its receipts rows by hand "
             f"(package ICCID ...{_digits(extra.get('iccid'))[-4:] or '?'}). It will not be offered again.")


# ── the run ──────────────────────────────────────────────────────────────────

def shelve_extras(book, receipts_ws=None, now: datetime | None = None) -> list[dict]:
    """Put every unshelved second package the site lists on the stock tab,
    exactly once, and tell the owner once per package. Returns what was newly
    shelved, [{order_id, row, iccid_tail}], for the tests and the log.

    Never raises for one package's sake: each is tried on its own, and a
    failure leaves it listed for the next run."""
    orders = extras_list()
    if not orders:
        return []
    if book is None:
        log.info("no spreadsheet handle here - the fulfillment bot's run shelves the extras")
        return []
    try:
        ws = stock_ws(book)
    except ShelfError as e:
        log.error(f"stock tab not writable: {e}")
        fb.alert("The stock tab cannot be written - second packages are waiting",
                 f"{e}.\n\nRestore the header row of '{STOCK_TAB}' (the PC bot's columns, in any order); "
                 f"until then no package is shelved, and the site keeps listing them.")
        return []
    receipts = receipts_ws if receipts_ws is not None else book.sheet1
    try:
        rvalues = receipts.get_all_values()
    except Exception as e:
        log.warning(f"could not read the receipts sheet for the extras' costs: {fb._redact(str(e))}")
        rvalues = []
    header, rows = _tab_rows(ws)
    added_at = (now or datetime.now(timezone.utc)).astimezone(fb.TZ).strftime("%d/%m/%Y %H:%M:%S")
    done = []
    for order in orders:
        oid = str(order.get("order_id") or "")
        serving_tail = _digits(order.get("serving_iccid_tail"))
        for extra in order.get("extras") or []:
            try:
                iccid = _digits(extra.get("iccid"))
                # The customer's own package is never stock: shelving it would
                # sell the next customer an eSIM somebody is using. The site
                # parks only what it proved different; this is the second lock.
                if extra.get("distinct") is False:
                    refusal_alert(order, extra, "the site could not prove it differs from the customer's eSIM")
                    mark_shelved(oid, extra, refused="not_distinct")
                    continue
                if iccid and serving_tail and iccid.endswith(serving_tail):
                    refusal_alert(order, extra, "its ICCID ends like the customer's own package")
                    mark_shelved(oid, extra, refused="same_as_serving")
                    continue
                session = session_cell(order, extra)
                if not iccid and not session.startswith(STELLAR_SESSION):
                    continue                    # nothing to key a row on (the site lists none such)
                have = already_on_tab(rows, iccid, session)
                if have:
                    # Shelved by an earlier run (whose word to the site was
                    # lost), or by the owner by hand: tell the site, add nothing.
                    mark_shelved(oid, extra, shelf_row=have["_row"])
                    log.info(f"order {oid}: extra ...{iccid[-4:] or session[-6:]} is already on the "
                             f"stock tab (row {have['_row']}) - site told")
                    continue
                rrow = receipts_row_for(rvalues, extra)
                cost = _money(rrow.get("buy")) if rrow and rrow.get("buy") else None
                if cost is None:
                    cost = _money(extra.get("buy_price"))
                stellar = session.startswith(STELLAR_SESSION)
                values = {
                    H_ICCID: iccid,
                    H_SKU: (rrow or {}).get("sku") or str(order.get("sku") or ""),
                    H_PLAN: (rrow or {}).get("plan") or str(extra.get("plan") or ""),
                    H_SESSION: session,
                    H_ADDED: added_at,
                    H_COST: f"{cost:.2f}$" if cost is not None else "",
                    H_ORIGIN: f"auto: second package of {oid}"
                              + (f" (receipts row {rrow['n']})" if rrow else " (no receipts row)"),
                    H_STATUS: FREE,
                    H_NOTE: STELLAR_NOTE if stellar else
                            f"via {extra.get('via') or '?'} {_local(extra.get('ts'), '%d/%m/%Y %H:%M')}".strip(),
                }
                n = _append(ws, header, values)
                rows.append({**values, "_row": n})
                log.info(f"order {oid}: extra ...{iccid[-4:] or session[-6:]} shelved as stock row {n}")
                shelf_alert(order, extra, n, rrow, cost)
                mark_shelved(oid, extra, shelf_row=n)
                done.append({"order_id": oid, "row": n, "iccid_tail": iccid[-4:]})
            except Exception as e:
                log.error(f"order {oid}: could not shelve an extra ({type(e).__name__}: "
                          f"{fb._redact(str(e))}) - it stays listed for the next run")
    return done


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    got = shelve_extras(fb.sheet_client().open_by_key(fb.RECEIPTS_SHEET_ID))
    print(f"{len(got)} package(s) shelved")
