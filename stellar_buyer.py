#!/usr/bin/env python3
"""
Stellar buyer -- bot #8. Buys a paid waverole.com order from Stellar Wholesale
and hands the site the eSIM.

Runs in GitHub Actions as a step of fulfillment.yml, right BEFORE the
fulfillment bot, so an order this step delivers is mailed to its buyer by the
next step of the same run. Stateless on purpose, like everything else in this
repo: the run may die on any line, and the next run must neither lose an order
nor buy it twice. Four facts, each kept by the party that cannot be wrong
about it:

  * WHAT is owed     -- the site's queue:
                        GET /api/orders?status=pending&supplier=stellar
  * WHO is on it     -- the site's shared claim: POST {order_id, claim}
                        (the PC bot takes the same lock; memory:
                        supplier-switch-wiring)
  * WHAT WAS DONE    -- the receipts sheet: one row per attempt, keyed by the
                        order number, Route 'Stellar <code>', the supplier
                        link naming the Stellar order. A row with an order id
                        means "bought -- settle it"; a row in manual review
                        means "hands off"; a failed row means "try again,
                        under a NEW idempotency key".
  * NOT TWICE        -- Stellar's mandatory Idempotency-Key on the one write
                        that costs money: 'waverole-<order>-a<attempt>'. A run
                        that placed the order and died before writing its row
                        replays the same key next run and is handed the same
                        order back, not a second one.

Money rules are the PC bot's, carried over (memory: esim-bot-project):
  GB exact; days >= what the customer was promised -- the sheet's floor AND
  the days written into the order's own token; wholesale <= the sheet's price
  (a rise since the nightly refresh is refused, never absorbed); a hard cap per
  order and per run. Whatever is refused is reported 'failed' to the site,
  which parks the PAID order on its retry list and names this buyer in the
  stale-order alert -- never silently dropped (memory: paid-order-safety-net).

The site's word 'failed' has one meaning there: NO PACKAGE WAS BOUGHT. It acts
on it by putting the paid order back on a retry list to be bought again
(api/orders.js). So this bot says it only when Stellar's answer proves the
request was refused before anything was created -- a 4xx. When the answer does
not say whether the money moved (a 5xx, no answer at all, an accepted order
Stellar will not name), NOTHING is reported: the order simply keeps its place
in the queue, the same idempotency key is asked again, and if that still
settles nothing a review row parks it for a person. An unknown outcome
reported as 'failed' is how one paid order becomes two packages.

Secrecy: this repo is PUBLIC and so are its Actions logs. Nothing here logs an
activation code, a delivery link, a customer address or a key. Order ids only.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Optional

import requests

import fulfillment_bot as fb
import stellar_prices as sp
from choose_supplier import usd_outside_parens

log = logging.getLogger("stellar-buyer")

# ── constants ────────────────────────────────────────────────────────────────
BASE = "https://wholesale.stellarsecurity.com/api/v1"
PORTAL_ORDER = "https://wholesale.stellarsecurity.com/orders/{id}"
SUPPLIER = "stellar"                 # the sheet's 'מקור' value, compared lower-case
WORKER = "gha:stellar"               # what the shared claim records
ROUTE_PREFIX = "Stellar "            # receipts Route: 'Stellar JC059'

MAX_EUR = float(os.getenv("STELLAR_MAX_EUR", "25"))       # one order
MAX_ORDERS_PER_RUN = int(os.getenv("STELLAR_MAX_PER_RUN", "5"))
PRICE_TOL = 0.005        # a rounding cent over the sheet is not a price rise
# How far the listing we are about to buy may sit ABOVE the euros the sheet
# priced the row at before this bot refuses to buy. It is a MONEY-safety bar,
# not the day rule: the sheet is refreshed on its own schedule, so a cell can
# legitimately be a run behind the catalogue (a longer validity held for a few
# cents, a supplier's own small move), and 5% is what the owner will absorb
# without being asked. Past it the price ROSE, and the margin the customer was
# quoted at is gone -- the order waits for a human instead. This used to be
# spelled sp.LONGER_TOL, which read as though it were the validity tolerance;
# the day rules now live in day_policy and have nothing to do with this number.
PRICE_RISE_TOL = 0.05
SETTLE_WAIT_S = int(os.getenv("STELLAR_SETTLE_WAIT_S", "90"))   # one run's patience
POLL_EVERY_S = 10
REPLAY_WAIT_S = float(os.getenv("STELLAR_REPLAY_WAIT_S", "3"))  # before re-asking the same key

# How long this run may spend before it hands the rest back to the next one.
# The worst case is not the common one -- a create_order that times out is 60s,
# a settle is 90 -- so five orders can outlast the step's own timeout, and being
# KILLED by that timeout is a stop with nothing written and nothing said. The
# budget makes the run stop itself instead, and the step's timeout is only ever
# the backstop (memory: scrape-workflow-budget).
RUN_BUDGET_S = int(os.getenv("STELLAR_RUN_BUDGET_S", "300"))

# Answers that mean "not now", which is not "no": Stellar says it did not
# process the request. Nothing is written, nothing is reported, and the next
# run asks again under the same key -- an outage that heals itself.
BUSY_CODES = {408, 425, 429, 503}

# Answers about US, not about the order: a rotated or revoked key, an edge
# serving a challenge page. Reporting these as the ORDER's failure would spend
# one of the site's MAX_BUY_ATTEMPTS on every queued order at once, over a
# fault that has nothing to do with any of them (memory: orders-token-locations
# -- the last rotation broke its consumers silently too).
AUTH_CODES = {401, 403, 407}

# What a dropped connection says about whether the request was SENT. requests
# raises the same ConnectionError for "could not connect" and "connected, then
# the server hung up"; only the second can have spent money.
NEVER_SENT = ("NewConnectionError", "Failed to establish a new connection",
              "Name or service not known", "nodename nor servname",
              "Temporary failure in name resolution", "Connection refused")


def reached_stellar(ex: Exception) -> bool:
    return not any(m in repr(ex) for m in NEVER_SENT)


class Blocked(Exception):
    """The world is wrong, not the order. Nothing is bought this run and every
    order keeps its place in the queue."""
FX_FALLBACK = 1.16       # only when the sheet's own '(€y) $x' cell cannot say

# Receipts 'סטטוס - Status' values this bot writes. The sheet is the ledger,
# so these words are state, not decoration: run() reads them back.
ST_PROCESSING = "מעבד"
ST_ACTIVE = "פעיל"            # same word the fulfillment bot writes: usage bot starts here
ST_FAILED = "נכשל"
ST_REVIEW = "בבדיקה ידנית"    # Stellar's manual_review, or an order we cannot identify

# Receipts headers, by NAME (the owner reorders columns; memory: receipts-sheet).
H_MAIL, H_DATE, H_SKU, H_GB = "מייל - Mail", "תאריך - Date", 'מק"ט - SUK', "איחסון - GB"
H_USAGE, H_ORDER, H_QR = "GB (0/X) - ניצול", "מס׳ הזמנה", "QR"
H_ACT, H_SMDP, H_APN = "Activation Code", "SM-DP+ Address", "גישה - APN"
H_LINK_SUP, H_LINK_WR = "Link - esim.dog", "Link - waverole"
H_REGION, H_PLAN, H_ROUTE = "אזור - Region", "חבילה - Plan", "Route"
H_STATUS, H_SOURCE = "סטטוס - Status", "מקור - source"
H_PURCHASE = "רכישה - Purchase"
H_BUY, H_SALE, H_SELL = "קנייה - Buy", "הנחה - Sale", "מכירה - Sell"

# Two columns, two questions. They were one column until 2026-09-10, and the
# one column answered the wrong one:
#   'מקור - source'   WHO the package came from -- the SUPPLIER. This bot buys
#                     from Stellar and only from Stellar, so it is a constant
#                     here; the PC bot writes 'esim.dog' in the same cell.
#   'רכישה - Purchase' HOW the customer paid US -- the rail, source_text().
# The owner reconciles PayPal's deposits against 'רכישה', and asks 'מקור' which
# supplier to chase when an eSIM misbehaves. Written into one cell, neither
# question could be answered: every Stellar row said "paypal" about its
# supplier and no row said anything at all about the rail.
SUPPLIER_NAME = "Stellar"

# Paid on a rail whose money is already in an account we can see. Those rows
# get the Mail cell painted green, which is the mark the owner has always put
# there by hand. 'manual' and 'bot - manually' are NOT here on purpose: they
# are the rows whose money still has to be chased, and green is what says a
# row needs no chasing.
PAID_RAILS = {"paypal", "payme"}
PAID_BG = {"red": 217 / 255, "green": 234 / 255, "blue": 211 / 255}   # #d9ead3

TERMINAL_BAD = {"failed", "refunded"}
NON_TERMINAL = {"processing", "vpn_processing"}


def _data(body: dict) -> dict:
    """The 'data' object, or nothing. Stellar answers JSON, but an edge, a
    proxy or an error page can answer anything at all."""
    d = (body or {}).get("data")
    return d if isinstance(d, dict) else {}


def unknown_outcome(code: int, body: dict) -> bool:
    """True when Stellar's answer does not say whether the order was created.

    A 5xx, a 2xx carrying no order id, a body that was not JSON, no answer at
    all (code 0): the request reached Stellar and what came back is silence
    about the money. The one word that must not follow is 'failed' -- the site
    reads that as "no package was bought" and puts the PAID order back in the
    queue for it (api/orders.js). Reporting an unknown outcome as failed is
    exactly how one order becomes two packages.
    """
    if code in (200, 201, 202):
        return not str(_data(body).get("id") or "").strip()
    if code in BUSY_CODES:
        return False        # Stellar SAID it did not process the request
    return code >= 500 or code < 400


class Refused(Exception):
    """A rule said no. The order goes back to the site as 'failed' (parked,
    retryable) with this text as the reason, and the owner is told."""


# ── the site ─────────────────────────────────────────────────────────────────

def _site_headers() -> dict:
    return {"Authorization": f"Bearer {fb.env('ORDERS_TOKEN')}"}


def pending_orders() -> list[dict]:
    """A BUYER poll: names its supplier, so the site hands over Stellar orders
    only and stamps orders:bot_last_poll:stellar for the no-bot alert."""
    r = requests.get(fb.ORDERS_URL, params={"status": "pending", "supplier": SUPPLIER},
                     headers=_site_headers(), timeout=20)
    r.raise_for_status()
    return list(r.json().get("orders") or [])


def claim(order_id: str) -> bool:
    """True = ours (or already ours). False = another buyer holds it.
    Raises when the site cannot be asked -- and then nothing is bought."""
    r = requests.post(fb.ORDERS_URL, json={"order_id": order_id, "claim": WORKER},
                      headers=_site_headers(), timeout=20)
    if r.status_code == 409:
        log.warning(f"{order_id}: held by {r.json().get('by', '?')} -- not ours")
        return False
    r.raise_for_status()
    return bool(r.json().get("claimed"))


def report_fulfilled(order_id: str, esim: dict):
    r = requests.post(fb.ORDERS_URL, json={"order_id": order_id, "status": "fulfilled",
                                           "esim": esim},
                      headers=_site_headers(), timeout=20)
    r.raise_for_status()


def report_failed(order_id: str, reason: str):
    r = requests.post(fb.ORDERS_URL, json={"order_id": order_id, "status": "failed",
                                           "reason": f"stellar: {reason}"[:500]},
                      headers=_site_headers(), timeout=20)
    r.raise_for_status()


# ── Stellar ──────────────────────────────────────────────────────────────────

class Stellar:
    def __init__(self, key: str, session: Optional[requests.Session] = None):
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {key}",
                               "Accept": "application/json"})

    def wallet_cents(self) -> int:
        r = self.s.get(f"{BASE}/wallet", timeout=30)
        r.raise_for_status()
        return int((r.json().get("data") or {}).get("available_balance_cents") or 0)

    def plans(self) -> list:
        return sp.fetch_plans(self.s.headers["Authorization"].split(" ", 1)[1])

    def create_order(self, plan_id: str, idem_key: str) -> tuple[int, dict]:
        """The one call in this repo that spends money.

        A read timeout is not an answer: the request was sent and the order may
        well exist. It comes back as code 0, which the caller treats as an
        unknown outcome and replays under the same key -- never as "nothing was
        bought". A CONNECT timeout is left to raise: that request never left,
        so the order simply keeps its place in the queue.
        """
        try:
            r = self.s.post(f"{BASE}/orders", json={"plans": [{"plan_id": plan_id, "quantity": 1}]},
                            headers={"Idempotency-Key": idem_key}, timeout=60)
        except requests.exceptions.ConnectTimeout:
            raise                       # never left this machine: nothing to know
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ChunkedEncodingError) as ex:
            log.warning(f"create_order: {type(ex).__name__} -- outcome unknown")
            return 0, {"error": {"code": "timeout", "message": "no answer from Stellar"}}
        except requests.exceptions.ConnectionError as ex:
            # ConnectTimeout is caught above; what is left is either "could not
            # connect" (nothing was sent, let it raise -- the order simply
            # stays queued) or "connected and then lost it", which is the
            # request going out and the answer never coming back.
            if not reached_stellar(ex):
                raise
            log.warning("create_order: connection lost after the request -- outcome unknown")
            return 0, {"error": {"code": "disconnected", "message": "no answer from Stellar"}}
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, (body if isinstance(body, dict) else {})

    def order(self, order_id: str) -> dict:
        r = self.s.get(f"{BASE}/orders/{order_id}", timeout=30)
        r.raise_for_status()
        return r.json().get("data") or {}

    def esim(self, sim_id: str) -> dict:
        r = self.s.get(f"{BASE}/esims/{sim_id}", timeout=30)
        r.raise_for_status()
        return r.json().get("data") or {}

    def plan(self, plan_id: str) -> dict:
        """One listing, for the region and networks of an order placed by an
        earlier run -- one call, where the whole catalogue is thirty-five."""
        r = self.s.get(f"{BASE}/plans/{plan_id}", timeout=30)
        r.raise_for_status()
        return r.json().get("data") or {}


# ── the price sheet ──────────────────────────────────────────────────────────

def sheet_spelling(sku: str) -> list[str]:
    """The site says 1.A.x / 1.B.x where the sheet says 1.0A.x / 1.0B.x."""
    out = [sku]
    for site, sheet in (("1.A.", "1.0A."), ("1.B.", "1.0B.")):
        if sku.startswith(site):
            out.append(sheet + sku[len(site):])
    return out


def stellar_rows() -> dict[str, sp.StellarRow]:
    svc = sp.sheets_service(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "credentials.json"))
    rows, _ = sp.read_rows(sp.read_sheet(svc))
    return {r.sku: r for r in rows}


def fx_of(row: sp.StellarRow) -> float:
    """USD per EUR, read off the row's own '(€2.12) $2.47' so the Buy column
    agrees with the price sheet to the cent. The fallback only rounds a
    receipt, never a decision -- decisions are made in EUR.

    The dollars are taken from OUTSIDE the parentheses, so both spellings of
    the pair read the same while the sheet migrates to euro-first: a reader
    that took the first number would have divided euros by euros and written
    a Buy price of 1.00 into the ledger."""
    usd = usd_outside_parens(row.price_cell or "")
    if usd and row.eur:
        return usd / row.eur
    return FX_FALLBACK


# ── the receipts sheet (the ledger) ──────────────────────────────────────────

def _hdr(ws) -> list[str]:
    return [h.strip() for h in ws.row_values(1)]


def order_rows(ws, order_id: str) -> list[dict]:
    """Every attempt this bot recorded for the order, oldest first, as
    {n, status, stellar_id, ...} -- keyed by order number AND a Stellar
    route, so an esim.dog row for the same order number is never mistaken
    for ours."""
    hdr = _hdr(ws)
    want = {H_ORDER, H_ROUTE, H_STATUS, H_LINK_SUP}
    if not want <= set(hdr):
        raise RuntimeError(f"receipts sheet header missing: {sorted(want - set(hdr))}")
    idx = {h: hdr.index(h) for h in want}
    out = []
    for n, r in enumerate(ws.get_all_values()[1:], start=2):
        r = list(r) + [""] * len(hdr)
        if r[idx[H_ORDER]].strip() != order_id or not r[idx[H_ROUTE]].strip().startswith(ROUTE_PREFIX):
            continue
        link = r[idx[H_LINK_SUP]].strip()
            # Not anchored: the owner is TOLD to paste a portal link into a row,
        # and a copied url carries a trailing slash, a ?tab= or a #fragment.
        # Anchoring it made every one of those parse as "no order", which is
        # the reading that costs money.
        m = re.search(r"/orders/([A-Za-z0-9_-]+)", link)
        out.append({"n": n, "status": r[idx[H_STATUS]].strip(),
                    "stellar_id": m.group(1) if m else ""})
    return out


def append_row(ws, values: dict) -> int:
    """Explicit A{n} write, never append_row: gspread's table detection once
    shifted a receipts row 18 columns right (memory: esim-bot-project).

    The row number is READ and then written, and the PC buyer and the
    fulfillment bot append to this same sheet -- so between the two, someone
    else's row can land on n and this one overwrites it. This row is the proof
    that money was spent; losing it buys the package again. So the write is
    read back once, and repeated at the new end if it did not survive.
    """
    hdr = _hdr(ws)
    row = [""] * len(hdr)
    unplaced = []
    for k, v in values.items():
        if v in (None, ""):
            continue
        if k in hdr:
            row[hdr.index(k)] = str(v)
        else:
            unplaced.append(k)
    if unplaced:
        # A renamed or not-yet-added column must not cost the purchase row --
        # the row is the proof the money moved. It loses a cell and says so.
        log.warning(f"receipts sheet has no column for {unplaced} -- values skipped")
    stamp, col = str(values.get(H_ORDER) or ""), hdr.index(H_ORDER)
    for last in (False, True):
        n = len(ws.get_all_values()) + 1
        ws.update(f"A{n}", [row], value_input_option="USER_ENTERED")
        if not stamp or last:
            return n
        back = ws.get_all_values()
        got = back[n - 1] if len(back) >= n else []
        if len(got) > col and got[col].strip() == stamp:
            return n
        log.warning(f"receipts row {n} was taken by another writer -- appending again")
    return n


def mail_cell(ws, n: int) -> str:
    """The A1 address of row n's Mail cell, found by HEADER NAME.

    Every other read and write in this file finds its column by header text
    because the owner reorders the receipts columns (memory: receipts-sheet) --
    this one used to be the exception, hard-coded A{n}. The day 'מייל - Mail'
    stops being first, that paints the green "no money to chase" mark onto
    whatever column moved into A instead. Column A is the fallback only when
    the header is genuinely absent, and reading the header is best-effort like
    the paint itself: a failure here must not cost a purchase.
    """
    try:
        hdr = _hdr(ws)
        if H_MAIL in hdr:
            return f"{sp.col_letter(hdr.index(H_MAIL))}{n}"
    except Exception as e:                                  # noqa: BLE001
        log.warning(f"could not read the receipts header to place the paint: {e}")
    return f"A{n}"


def paint_paid(ws, n: int, rail: str) -> bool:
    """Green the Mail cell of a row the customer paid on a real rail.

    Best-effort by contract: the row is the receipt, the colour is a
    convenience, and a Sheets formatting call that fails (quota, a gspread
    without .format, a permissions edge) must never turn a bought package
    into a failed buy. Returns whether it painted, for the tests.
    """
    if (rail or "").strip().lower() not in PAID_RAILS or n < 2:
        return False
    try:
        ws.format(mail_cell(ws, n), {"backgroundColor": PAID_BG})
        return True
    except Exception as e:                                  # noqa: BLE001
        log.warning(f"could not paint receipts row {n} green: {e}")
        return False


def update_row(ws, n: int, values: dict):
    import gspread
    hdr = _hdr(ws)
    cells = [gspread.Cell(n, hdr.index(k) + 1, str(v))
             for k, v in values.items() if k in hdr and v not in (None, "")]
    if cells:
        ws.update_cells(cells, value_input_option="USER_ENTERED")


def discount_text(list_usd, sold) -> str:
    """Same wording as the PC bot and the nightly sweep -- the owner reads one
    column, not two. Percent OF THE LIST PRICE (memory: discount-drift-vs-real)."""
    try:
        listed, sold = float(list_usd or 0), float(sold if sold is not None else "nan")
    except (TypeError, ValueError):
        return ""
    if not listed or sold != sold:
        return ""
    if sold <= 0:
        return f"100% (${listed:.2f} הנחה)"
    if sold < listed:
        return f"{round((listed - sold) / listed * 100)}% (${listed - sold:.2f} הנחה)"
    return "-"


SOURCE_UNKNOWN = "bot - manually"
SOURCE_NAMES = {"paypal": "paypal", "payme": "payme", "manual": "manual"}


def source_text(rail: str) -> str:
    """The rail the customer paid on, spelled exactly as the PC bot spells it
    (bot/sheets.py): the owner reconciles this column against PayPal's
    deposits, so the buyer's name does not belong here -- Route says 'Stellar'."""
    key = (rail or "").strip().lower()
    return SOURCE_NAMES.get(key, key) if key else SOURCE_UNKNOWN


# ── alerts, throttled ────────────────────────────────────────────────────────

def _quiet_hour(now: Optional[datetime] = None) -> bool:
    """This step runs every few minutes. A condition that persists (an empty
    wallet, Stellar down) must not mail every run; the first minutes of each
    hour carry it, and the site's own stale-order ladder covers the rest.

    The window has to be WIDER than the gap between runs, or it is not a
    throttle but a lottery. Actions delays a */5 schedule by an unpredictable
    few minutes, so a three-minute window at the top of the hour is hit only
    if the delay happens to land there: runs at :04, :09, :14 never alert at
    all and a persistent fault goes unreported for days. Ten minutes always
    contains a slot of a five-minute cadence, and costs at most two mails an
    hour, which is the bound that matters (memory: stuck-claim-watchdog).
    """
    return (now or datetime.now(fb.TZ)).minute >= 10


def alert_hourly(subject: str, body: str, now: Optional[datetime] = None):
    if _quiet_hour(now):
        log.warning(f"(alert held until the top of the hour) {subject}")
        return
    fb.alert(f"[stellar] {subject}", body)


def alert_now(subject: str, body: str):
    fb.alert(f"[stellar] {subject}", body)


# ── choosing what to buy ─────────────────────────────────────────────────────

def promised_days(order: dict) -> int:
    """The days the customer SAW: written into the order's own token (d) by the
    till. The sheet's floor is the promise of the esim.dog twin; the token is
    the promise of the row actually sold. The customer is owed the larger."""
    d = fb.order_payload(order.get("order_url") or "").get("d")
    try:
        return int(d or 0)
    except (TypeError, ValueError):
        return 0


def choose(cat: sp.Catalogue, row: sp.StellarRow, order: dict) -> sp.Variant:
    # The size's own day band is day_policy's business and decide() applies it;
    # what this bot adds is the promise in the order token, which can only make
    # the floor longer. Never fewer days than the customer was shown.
    promised = promised_days(order)
    floor = max(row.floor_days or 0, promised)
    d = sp.decide(cat, row, min_days=promised)
    if d is None:
        raise Refused(f"sheet row {row.row} ({row.sku}) has no code or size")
    if d.pick is None:
        raise Refused(f"no listing for {row.sku}: {d.reason} "
                      f"(code {row.code}, {row.gb:g}GB, need >= {floor} days)")
    v = d.pick
    if not v.plan_id:
        raise Refused(f"{row.sku}: listing has no plan id (feed instead of API?)")
    if row.eur is None:
        raise Refused(f"{row.sku}: sheet row carries no wholesale price")
    # The sheet's cell can be a run behind the catalogue -- the chooser holds a
    # longer listing for a few cents (memory: validity-beats-pennies) and the
    # row is repriced later. PRICE_RISE_TOL is how much of that gap is absorbed;
    # past it the price ROSE since the row was priced, and that is refused.
    if v.wholesale_eur > row.eur * (1 + PRICE_RISE_TOL) + PRICE_TOL:
        raise Refused(f"{row.sku}: price rose EUR {row.eur:.2f} -> {v.wholesale_eur:.2f} "
                      f"since the sheet was priced -- not absorbing it")
    if v.wholesale_eur > MAX_EUR:
        raise Refused(f"{row.sku}: EUR {v.wholesale_eur:.2f} is over the per-order cap {MAX_EUR:.0f}")
    if abs(v.gb - (row.gb or 0)) > 1e-9:
        raise Refused(f"{row.sku}: chooser returned {v.gb:g}GB for a {row.gb:g}GB row")
    return v


def plan_facts(raw_plan: dict, row: sp.StellarRow) -> tuple[str, str]:
    """(region, networks) for the order page and the receipts row, from the
    listing itself -- the sheet's Hebrew country name is the fallback."""
    networks = sp.networks_label(raw_plan.get("coverage") or {})
    region = str(raw_plan.get("destination") or row.country or "")
    return region, networks


# ── credentials out of a Stellar order ───────────────────────────────────────

def credentials(st: Stellar, data: dict) -> Optional[dict]:
    """The installation block of the first eSIM that has one -- on the order
    itself when the key carries esims:read, else fetched per sim_id. None
    while Stellar is still provisioning (or the key cannot see it)."""
    for e in data.get("esims") or []:
        inst = e.get("installation") or {}
        if not inst.get("activation_code") and e.get("sim_id"):
            try:
                inst = st.esim(str(e["sim_id"])).get("installation") or {}
            except requests.HTTPError as ex:
                log.warning(f"esim lookup: HTTP {ex.response.status_code if ex.response is not None else '?'}")
                inst = {}
        if inst.get("activation_code"):
            return {"activation_code": str(inst["activation_code"]),
                    "qr_code_url": str(inst.get("qr_code_url") or ""),
                    "apn": str(inst.get("apn") or ""),
                    "sim_id": str(e.get("sim_id") or "")}
    return None


def site_payload(cred: dict, region: str, networks: str, gb: float, days: int) -> dict:
    """What /api/orders stores under esim.* (ESIM_FIELDS). No ICCID: Stellar
    never returns one, by design. The plan string parses with the fulfillment
    bot's PLAN_RE so the customer email states GB and days."""
    act = cred["activation_code"]
    parts = act.split("$")
    smdp = parts[1] if len(parts) >= 3 else ""
    # Stellar's qr_code_url is NOT an image: it answers text/html (a page on
    # p.esimlink.org, checked 2026-09-10), and handing it over as qr_code drew
    # an empty square on the order page and a broken attachment in the mail.
    # The site draws the QR from the activation code; the link stays on the
    # receipts row only.
    return {"activation_code": act,
            "qr_code": "",
            "smdp": smdp, "iccid": "", "apn": cred.get("apn") or "",
            "region": region, "plan": f"{gb:g}GB - {days} days", "networks": networks}


# ── the run ──────────────────────────────────────────────────────────────────

class Run:
    """One invocation. Loads the catalogue and the price sheet lazily -- most
    runs find an empty queue and must cost nothing."""

    def __init__(self, st: Stellar, ws, now: Optional[datetime] = None):
        self.st, self.ws = st, ws
        self.now = now or datetime.now(fb.TZ)
        self._rows: Optional[dict] = None
        self._cat: Optional[sp.Catalogue] = None
        self._raw: dict = {}
        self.bought = 0

    def rows(self) -> dict:
        if self._rows is None:
            self._rows = stellar_rows()
        return self._rows

    def catalogue(self) -> sp.Catalogue:
        if self._cat is None:
            plans = self.st.plans()
            cat = sp.Catalogue.from_api(plans)
            # A read that makes most coded rows vanish at once is a broken read
            # -- an empty page, a renamed id format, a key answering 200 with
            # nothing -- not a supplier that dropped half its catalogue. Every
            # order in the queue would be refused by choose() and reported
            # 'failed' on the strength of it, spending one of the site's
            # MAX_BUY_ATTEMPTS on each. The nightly refresh runs this same
            # guard before it writes a price; nothing may be bought without it.
            why = sp.sanity([d for d in (sp.decide(cat, r) for r in self.rows().values()) if d])
            if why:
                raise Blocked(why)
            self._raw = {str(p.get("id")): p for p in plans}
            self._cat = cat
            log.info(f"catalogue: {len(cat.variants)} fixed listings")
        return self._cat

    def row_for(self, sku: str) -> sp.StellarRow:
        for s in sheet_spelling(sku):
            if s in self.rows():
                return self.rows()[s]
        raise Refused(f"no Stellar row in the price sheet for SKU {sku}")

    # ── one order ──
    def handle(self, o: dict):
        oid = str(o.get("order_id") or "").strip()
        if not oid:
            return
        if not claim(oid):
            return
        attempts = order_rows(self.ws, oid)
        # Only a row that explicitly says FAILED authorises another purchase.
        # Anything else -- processing, active, in review, a word a person
        # typed, a blank cell -- means an attempt is outstanding and this bot
        # keeps its hands off. The old rule also demanded that the supplier
        # link PARSE into a Stellar order id, so a link the owner reformatted,
        # or a cell the sheet renders as a hyperlink label rather than a url
        # (memory: variant-cell-rendering), turned a row that had already
        # spent money into "no attempt was made" -- and bought it again.
        live = [r for r in attempts if not r["status"].startswith(ST_FAILED)]
        if live:
            r = live[-1]
            if r["status"].startswith(ST_REVIEW):
                log.info(f"{oid}: in manual review (row {r['n']}) -- hands off")
                return
            if not r["stellar_id"]:
                log.warning(f"{oid}: row {r['n']} says {r['status']!r} but names no Stellar order")
                alert_hourly(f"{oid}: receipts row {r['n']} cannot be settled",
                             f"Row {r['n']} says {r['status']!r} for {oid} but its supplier link "
                             f"does not name a Stellar order, so nothing can be settled from it "
                             f"-- and nothing is bought either, because that row may already have "
                             f"cost money. Paste the order's portal link into it, or set it to "
                             f"'{ST_FAILED}' if nothing was bought and the next run will retry.",
                             self.now)
                return
            self.settle(o, r["n"], r["stellar_id"], wait=False)
            return
        if self.bought >= MAX_ORDERS_PER_RUN:
            log.warning(f"{oid}: per-run cap of {MAX_ORDERS_PER_RUN} reached -- next run")
            return
        n, sid = self.buy(o, attempt=len(attempts))
        self.bought += 1
        if sid:
            self.settle(o, n, sid, wait=True)

    def buy(self, o: dict, attempt: int) -> tuple[int, str]:
        oid = str(o["order_id"])
        sku = str(o.get("sku") or "")
        try:
            row = self.row_for(sku)
            v = choose(self.catalogue(), row, o)
        except Refused as ex:
            log.warning(f"{oid}: refused -- {ex}")
            report_failed(oid, str(ex))
            alert_now(f"{oid} refused", f"{ex}\n\nThe order is parked on the site's retry list.")
            return 0, ""

        cents = int(round(v.wholesale_eur * 100))
        have = self.st.wallet_cents()
        if have < cents:
            alert_hourly(f"wallet too low for {oid}",
                         f"Stellar wallet EUR {have/100:.2f}, order needs EUR {cents/100:.2f} "
                         f"({sku}, {v.gb:g}GB/{v.days}d). Top up at wholesale.stellarsecurity.com; "
                         f"the order stays queued and is bought on the next run after that.",
                         self.now)
            log.warning(f"{oid}: the wallet is short of this order -- waiting for a top-up")
            return 0, ""

        idem = f"waverole-{oid}-a{attempt}"
        code, body = self.st.create_order(v.plan_id, idem)
        # Once ONE answer has failed to say whether the order was created, the
        # whole exchange is unknown and STAYS unknown: no later answer can
        # prove the first POST created nothing. A clean success resolves it --
        # Stellar hands the same order back -- and nothing else does, so this
        # flag closes the 'failed' branch below for the rest of the call.
        unsure = unknown_outcome(code, body)
        if unsure:
            # Guessing is the one move that can cost a second package. Ask the
            # same question again under the SAME key: if an order was created,
            # the replay is handed that order back and the run carries on as if
            # the first answer had arrived. (The replay re-sends the same plan;
            # a listing that moved under us answers 409, which is a review row
            # -- never a purchase.)
            log.warning(f"{oid}: HTTP {code} says nothing about the money -- replaying {idem}")
            time.sleep(REPLAY_WAIT_S)
            code, body = self.st.create_order(v.plan_id, idem)
        data = _data(body)
        err = (body.get("error") or {}) if isinstance(body.get("error"), dict) else {}
        region, networks = plan_facts(self._raw.get(v.plan_id) or {}, row)
        base = {H_MAIL: o.get("customer_email") or "", H_DATE: self.now.strftime("%d/%m/%Y %H:%M:%S"),
                H_SKU: row.sku, H_GB: f"{v.gb:g}GB", H_USAGE: f"0 / {v.gb:g}", H_ORDER: oid,
                H_LINK_WR: o.get("order_url") or "", H_ROUTE: f"{ROUTE_PREFIX}{row.code}",
                H_REGION: region, H_PLAN: f"{v.gb:g}GB - {v.days} days — {networks}".rstrip(" —"),
                H_SOURCE: SUPPLIER_NAME,
                H_PURCHASE: source_text(o.get("source") or ""),
                H_BUY: f"{v.wholesale_eur * fx_of(row):.2f}$",
                H_SELL: (f"{float(o['paid_usd']):.2f}$" if o.get("paid_usd") not in (None, "") else ""),
                H_SALE: discount_text(o.get("list_usd"), o.get("paid_usd"))}

        if code in (200, 201, 202) and data.get("id"):
            sid = str(data["id"])
            n = append_row(self.ws, {**base, H_LINK_SUP: PORTAL_ORDER.format(id=sid),
                                     H_STATUS: ST_PROCESSING})
            paint_paid(self.ws, n, (o.get("source") or ""))
            log.info(f"{oid}: Stellar order {sid} placed ({v.gb:g}GB/{v.days}d), row {n}")
            return n, sid
        if code == 402:
            alert_hourly(f"wallet refused {oid}", f"Stellar answered 402 (insufficient balance) "
                         f"for EUR {cents/100:.2f}. Top up; the order stays queued.", self.now)
            return 0, ""
        if code == 409:
            # Same key, different body: an order exists at Stellar for this key
            # and we cannot name it. A person must, before anyone buys again --
            # the review row is what stops the next run from doing so.
            n = append_row(self.ws, {**base, H_LINK_SUP: "https://wholesale.stellarsecurity.com/orders",
                                     H_STATUS: ST_REVIEW})
            paint_paid(self.ws, n, (o.get("source") or ""))
            alert_now(f"{oid} needs a look: idempotency conflict",
                      f"Stellar already holds an order under key {idem} but answered 409 to a replay. "
                      f"Find it in the portal, deliver by hand or settle, then clear row {n} of the receipts sheet.")
            return n, ""
        msg = f"HTTP {code} {err.get('code', '')} {err.get('message', '')}".strip()

        if code in AUTH_CODES:
            # Says nothing about this order, so the order is told nothing. It
            # keeps its place in the queue and the next run asks again.
            log.error(f"{oid}: Stellar refused the KEY ({msg}) -- not the order's fault")
            alert_hourly("Stellar refused the key",
                         f"Stellar answered {msg}. That is our key or an edge in front of it, not "
                         f"{oid} ({sku}) -- so nothing was reported against the order and every "
                         f"queued Stellar order is simply waiting. Check STELLAR_API_KEY in the "
                         f"repo secrets and the account at wholesale.stellarsecurity.com.", self.now)
            return 0, ""

        if code in BUSY_CODES:
            # Stellar says it did not process the request. Nothing is written
            # and the site is told nothing, so the order keeps its place in the
            # queue and the next run asks again under the same key -- which is
            # what lets an outage heal itself instead of needing anybody.
            log.warning(f"{oid}: Stellar busy ({msg}) -- same key next run")
            alert_hourly(f"Stellar busy, {oid} waiting",
                         f"Stellar answered {msg} to the order for {oid} ({sku}). Nothing was "
                         f"bought and nothing was written; the next run asks again under the same "
                         f"key {idem}, so this cannot become a second package.", self.now)
            return 0, ""

        if 400 <= code < 500 and not unsure:
            # Stellar READ the request and rejected it -- a bad plan, a bad
            # key, a rule of theirs. Nothing was created, and this is the only
            # shape of answer that earns the word the site reserves: 'failed'
            # there means "no package was bought", and the site acts on it by
            # putting a PAID order back on its retry list. The row is what
            # gives the next attempt a NEW key, since this one is now spent.
            log.warning(f"{oid}: Stellar refused the order: {msg}")
            append_row(self.ws, {**base, H_STATUS: f"{ST_FAILED} (HTTP {code})"})
            report_failed(oid, msg)
            alert_now(f"{oid} not bought",
                      f"Stellar: {msg}\n\nThe request was refused before anything was created "
                      f"-- the wallet is untouched. The order is parked on the site's retry list "
                      f"and the next attempt buys under a new key.")
            return 0, ""

        # Stellar has still not said whether the money moved -- either twice
        # over, or once and then with a refusal that only covers the REPLAY.
        # The site is NOT told 'failed': it would read that as "nothing was
        # hand this PAID order back to be bought again, and the wallet may
        # already be down a package. So the order simply keeps its place in the
        # queue, this row stops the next run from touching it, and a person
        # settles it from the portal.
        n = append_row(self.ws, {**base, H_LINK_SUP: "https://wholesale.stellarsecurity.com/orders",
                                 H_STATUS: ST_REVIEW})
        paint_paid(self.ws, n, (o.get("source") or ""))
        alert_now(f"{oid} needs a look: Stellar would not say what it did",
                  f"Stellar: {msg}\n\nThe order for {oid} ({sku}, {v.gb:g}GB/{v.days}d, "
                  f"EUR {v.wholesale_eur:.2f}) may or may not exist under key {idem} -- the first "
                  f"answer said nothing about the money and the second did not settle it. "
                  f"Nothing has been reported to the site, so the order is "
                  f"still queued, and nothing more is bought for it while row {n} of the receipts "
                  f"sheet says so.\n\nCheck the wallet and the orders list in the portal:\n"
                  f"  * the order IS there -- paste its link into row {n} and set the status to "
                  f"'{ST_PROCESSING}'; the next run settles and delivers it.\n"
                  f"  * it is NOT there -- set row {n} to '{ST_FAILED}' and the next run buys it "
                  f"under a new key.")
        return n, ""

    def settle(self, o: dict, n: int, sid: str, wait: bool):
        oid = str(o["order_id"])
        deadline = time.monotonic() + (SETTLE_WAIT_S if wait else 0)
        while True:
            data = self.st.order(sid)
            status = str(data.get("status") or data.get("order_status") or "").lower()
            cred = credentials(self.st, data)
            if cred:
                self.deliver(o, n, sid, cred, data)
                return
            if status in TERMINAL_BAD:
                log.warning(f"{oid}: Stellar order {sid} {status}")
                report_failed(oid, f"order {status} (auto-refunded by Stellar)")
                update_row(self.ws, n, {H_STATUS: f"{ST_FAILED} ({status})"})
                alert_now(f"{oid}: Stellar order {status}",
                          f"Stellar order {sid} ended {status}; the wallet is refunded by Stellar. "
                          f"The site parked the order for retry -- the next attempt buys under a new key.")
                return
            if status == "manual_review":
                rows = order_rows(self.ws, oid)
                already = any(r["stellar_id"] == sid and r["status"] == ST_REVIEW for r in rows)
                update_row(self.ws, n, {H_STATUS: ST_REVIEW})
                if not already:
                    alert_now(f"{oid}: Stellar put the order in manual review",
                              f"Stellar order {sid} is 'manual_review' (an ambiguous provider timeout). "
                              f"Nothing more is bought for {oid} until row {n} of the receipts sheet is cleared.")
                return
            if time.monotonic() >= deadline:
                log.info(f"{oid}: Stellar order {sid} still {status or 'provisioning'} -- next run")
                # A wait that is NOT this run's own purchase means an earlier
                # run already spent the money and the order has still not come
                # good; so does any status outside the two that are a normal
                # wait -- 'fulfilled' with no readable credentials lands here
                # too. Provisioning takes seconds, so either way the customer
                # has paid and has nothing. The hourly throttle IS the delay:
                # the runs just after a purchase say nothing at all.
                if not wait or status not in NON_TERMINAL:
                    alert_hourly(f"{oid}: Stellar order still {status or 'provisioning'}",
                                 f"Stellar order {sid} for {oid} (receipts row {n}) is "
                                 f"'{status or 'not provisioned'}' and has no credentials this key "
                                 f"can read. The wallet is already debited. Look at it in the "
                                 f"portal -- the customer has paid and has nothing.", self.now)
                return
            time.sleep(POLL_EVERY_S)

    def deliver(self, o: dict, n: int, sid: str, cred: dict, data: dict):
        oid = str(o["order_id"])
        row = None
        try:
            row = self.row_for(str(o.get("sku") or ""))
        except Refused:
            pass
        gb, days = ((row.gb or 0) if row else 0), ((row.days or 0) if row else 0)
        e = (data.get("esims") or [{}])[0]
        plan_id = str(e.get("plan_id") or "")
        days = int(e.get("duration_days") or days)
        region, networks = (row.country if row else ""), ""
        raw = self._raw.get(plan_id)
        if plan_id and raw is None:
            try:
                raw = self.st.plan(plan_id)
            except Exception:
                raw = None
        if raw:
            region, networks = plan_facts(raw, row) if row else (str(raw.get("destination") or ""), "")
            if not gb:
                gb = sp._gb_from_mb(int((raw.get("data") or {}).get("megabytes") or 0))
        payload = site_payload(cred, region, networks, gb, days)
        # The sheet FIRST, then the site. That reads backwards -- the customer
        # ought to come first -- until you ask what a run KILLED between the two
        # leaves behind. Row written, site not told: the order is still pending,
        # and the next run finds this very row and settles it, five minutes
        # late. Site told, row not written: the order leaves the queue for good
        # and the row is stranded at 'processing', so the usage meter never
        # starts (it starts at ST_ACTIVE) and the books never learn the cost.
        # Only one of those heals itself, and this step runs on a 4-minute
        # timeout. A sheet that REFUSES the write still does not hold up the
        # customer: it is reported anyway and the owner is told.
        try:
            update_row(self.ws, n, {H_QR: cred.get("qr_code_url") or "", H_ACT: payload["activation_code"],
                                    H_SMDP: payload["smdp"], H_APN: payload["apn"],
                                    H_REGION: region, H_STATUS: ST_ACTIVE,
                                    H_PLAN: f"{gb:g}GB - {days} days — {networks}".rstrip(" —")})
        except Exception as ex:
            log.exception("receipts row update failed")
            alert_now(f"{oid}: receipts row {n} not updated",
                      f"The eSIM is being handed to the customer now; the sheet row is missing "
                      f"its credentials and its status: {ex}")
        report_fulfilled(oid, payload)
        log.info(f"{oid}: delivered from Stellar order {sid}")


# -- readiness ---------------------------------------------------------------

MIN_FUEL_CENTS = int(os.getenv("STELLAR_MIN_FUEL_CENTS", "200"))


def preflight(st: Optional[Stellar] = None, loud: bool = False,
              now: Optional[datetime] = None) -> bool:
    """Ask Stellar the one read-only question nothing else in this file asks:
    does this key still work?

    `run()` returns on an empty queue before the key is ever touched, so this
    step going green in two seconds proves the workflow is wired and proves
    NOTHING about the secret behind it -- the same shape that let a wrong
    ORDERS_TOKEN sit unnoticed (memory: orders-token-locations). Left alone,
    the first thing to test the key would be a customer who has already paid.

    Quiet by default: it speaks only when the key is missing or refused, which
    is never a false alarm. `loud=True` is the hand-started preflight run and
    mails the answer either way, with the balance in it -- outside the portal
    there is nowhere else to read that number.
    """
    try:
        st = st or Stellar(fb.env("STELLAR_API_KEY"))
    except Exception as ex:
        alert_hourly("no Stellar key in this run",
                     f"{ex}\n\nNothing can be bought from Stellar until STELLAR_API_KEY is "
                     f"set in the repo's Actions secrets and reaches this step.", now)
        log.error("STELLAR_API_KEY is not set")
        return False

    try:
        cents = st.wallet_cents()
    except requests.HTTPError as ex:
        code = getattr(ex.response, "status_code", 0)
        if code in AUTH_CODES:
            alert_hourly("Stellar refuses our key",
                         f"GET /wallet answered {code}. Nothing can be bought from Stellar "
                         f"until this is fixed: check STELLAR_API_KEY in the repo's Actions "
                         f"secrets against the one in the portal.", now)
            log.error(f"Stellar refuses our key ({code})")
        else:
            log.warning(f"wallet read answered {code} -- not the key; asking again next run")
            if loud:
                alert_now("preflight: Stellar answered an error", f"GET /wallet -> HTTP {code}.")
        return False
    except Exception as ex:
        # Stellar being briefly unreachable says nothing about the key, and a
        # queue this run had nothing to buy from is not an outage worth mail.
        log.warning(f"wallet unreachable ({type(ex).__name__}) -- asking again next run")
        if loud:
            alert_now("preflight: Stellar unreachable", f"{type(ex).__name__}: {ex}")
        return False

    fuelled = cents >= MIN_FUEL_CENTS
    # The amount stays out of the log on purpose: this repo is public.
    log.info(f"Stellar accepted the key; wallet {'funded' if fuelled else 'EMPTY'}")
    if loud:
        alert_now("preflight: the key works",
                  f"Stellar accepted the key from GitHub Actions.\n\n"
                  f"Wallet available: EUR {cents/100:.2f}\n\n" +
                  ("There is enough in it to buy.\n\n" if fuelled else
                   "That is too low to buy anything -- top it up BEFORE a Stellar row goes "
                   "on sale, or every paid Stellar order waits for the wallet.\n\n") +
                  "This run bought nothing; it only asked.")
    return fuelled


def run(st: Optional[Stellar] = None, ws=None, now: Optional[datetime] = None) -> int:
    orders = pending_orders()
    if not orders:
        log.info("no Stellar orders pending")
        # An idle run is the only chance to catch a key that stopped working
        # before a paying customer does. Once an hour, in the same window the
        # alerts use, so an idle bot stays a free bot.
        if not _quiet_hour(now):
            try:
                preflight(st, now=now)
            except Exception:
                log.exception("readiness check failed")
        return 0
    log.info(f"{len(orders)} Stellar order(s) pending")
    st = st or Stellar(fb.env("STELLAR_API_KEY"))
    ws = ws if ws is not None else fb.sheet_client().open_by_key(fb.RECEIPTS_SHEET_ID).sheet1
    r = Run(st, ws, now)
    started = time.monotonic()
    for o in orders:
        if time.monotonic() - started > RUN_BUDGET_S:
            log.warning(f"{RUN_BUDGET_S}s spent -- the remaining order(s) stay queued for the "
                        f"next run, which is five minutes away")
            break
        oid = str(o.get("order_id") or "?")
        try:
            r.handle(o)
        except Blocked as ex:
            log.error(f"nothing is bought this run: {ex}")
            alert_hourly("the Stellar catalogue read is not trustworthy",
                         f"{ex}\n\nNothing was bought; every queued Stellar order keeps its "
                         f"place and the next run tries again.", r.now)
            break
        except Exception as ex:
            log.exception(f"{oid}: {type(ex).__name__}")
            alert_hourly(f"{oid}: {type(ex).__name__}", f"{ex}\n\nThe order stays queued; this repeats next run.", r.now)
    return r.bought


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    try:
        # `--preflight` (stellar_preflight.yml, by hand from the Actions tab)
        # asks Stellar whether the key works and mails back the balance. It
        # never buys, so it is safe to run at any moment, including while the
        # queue has orders in it.
        if "--preflight" in sys.argv[1:]:
            # The exit code has to carry the answer. A preflight that goes
            # green whether or not the key works is the very disease it was
            # written to cure -- and the run log is 403 to everyone outside
            # the Actions tab, so a red X is the only signal that travels.
            return 0 if preflight(loud=True) else 1
        run()
        return 0
    except Exception as ex:
        log.exception("stellar buyer crashed")
        try:
            alert_hourly("buyer crashed", f"{type(ex).__name__}: {ex}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
