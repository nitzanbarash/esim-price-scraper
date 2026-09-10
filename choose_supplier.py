#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which SUPPLIER's row the site sells, for every SKU that has two of them.

The sheet holds one row per (SKU x supplier) — memory: supplier-split-row-layout
— and the tick in נבחר (column W) is what the sync reads: the ticked row is the
price the site quotes and the supplier the bot buys from. Until now that tick
was moved by hand. This script moves it, by the one rule the owner gave:

    Stay with the incumbent unless the other supplier is REALLY cheaper.

"Really" is day_policy.DAY_TOL — the same 1% the validity rules use, and for the
same reason: a price that is a rounding error cheaper is not cheaper, and moving
a live SKU to another supplier for half a cent costs more in churn than it saves.
The one case that moves for free is an incumbent that cannot be sold at all: no
price, or a word in במלאי/רווחי (column Q) saying it is out of stock. Then any
eligible challenger beats it, however small the gap.

What "eligible" means (all four, or the row cannot win):
    D  מקור          is esim.dog or stellar (case-insensitive). A BLANK cell is
                     esim.dog — that is what waverole_sync.gs rowToPackage_
                     (blank source defaults to esim.dog) reads it as, and what
                     both buy bots buy on. Reading blank as "unknown"
                     let a Stellar rival win a SKU at any price at all.
    G  מחיר קנייה    parses as a leading USD amount — '—', blank or prose is not
                     a price, and a row with no price is not for sale
    Q  במלאי/רווחי   is EMPTY. Anything there — לא רווחי, לא זמין, the owner's
                     own note — means do not sell this row
    F  זמן חבילה     parses as days

Who the incumbent is
--------------------
The tick, if there is exactly one. Otherwise the esim.dog row, because that is
the supplier every SKU was born on. And when that is not a single answer — two
ticks, two esim.dog rows, or neither a tick nor a dog row — the SKU is SKIPPED
and logged. A guess here moves real money to the wrong supplier; a skip only
leaves the sheet as the owner last had it.

What a switch writes, in one batchUpdate so the row can never be half-switched:
    W   the tick on the winner, cleared on every other row of the SKU
    U,S,T,V  copied from the incumbent — those four are the CUSTOMER side of the
        SKU (מחיר סופי is what he is charged; memory: fee-ladder-vs-real-fee) and
        they belong to the SKU, not to the supplier. The gate is U, and it is
        a READ, not a blank check: U must parse as a price ('16.99' or '$16.99').
        If the incumbent is the TICKED row and its U does not parse, the SKU is
        SKIPPED — moving the tick to a row that has no customer price is how a
        live SKU goes dark on the site (rowToPackage_ drops a priceless row),
        and a supplier saving is not worth taking the package off sale.
        Every carried cell is written with the TYPE it was read as: a number
        stays a number, text stays text. The sheet's own formulas read S/T/U/V,
        and a currency FORMAT arriving as text once cancelled three paid orders
        (memory: variant-cell-rendering).
    J   a note saying which way it moved and at what two costs
    A..W background — green on the winner, grey on the losers, so the owner can
        see the state of the sheet without reading a single number. A..W is
        exactly the width enforceChoice_ paints, so a hand tick and a bot tick
        leave the same stripe. X and beyond are never touched: the free columns
        and then the reference blocks live there (memory: price-sheet-column-tail).

And on EVERY multi-row SKU, switch or no switch, P (רווח (כדאיות)) is filled in
wherever it is blank — the Stellar rows have always been blank there, so the
owner has never been able to compare the two rows on the one number that matters.
It is written in the scraper's own format, off the SKU's מחיר שלי, and only
where that price exists.

Run:
    python choose_supplier.py            dry run — prints the table, writes nothing
    python choose_supplier.py --apply    writes to the sheet
    python choose_supplier.py --apply --max-switches 5
                                         write the first 5 switches only, and say
                                         how many were left — for a phased first
                                         run. 0 (the default) is unlimited.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Optional

import day_policy
from esim_price_scraper import HEADER_KEYS, SHEET_ID

# The two suppliers, spelled the way column D spells them (compared lower-case:
# the sheet writes 'Stellar' on some rows and 'stellar' on others).
SOURCES = frozenset({"esim.dog", "stellar"})

# A blank מקור is not an unknown supplier — it is esim.dog. Every other reader
# of this sheet already says so: waverole_sync.gs rowToPackage_ (blank source
# defaults to esim.dog), and so do both buy bots. Treating blank as unknown here made the
# oldest rows in the sheet — the ones written before there WAS a second supplier
# — ineligible to defend themselves, so a Stellar row won them at any price.
DEFAULT_SOURCE = "esim.dog"

TICK = "✓"                    # the mark the sync looks for in נבחר
WINNER_BG = "#d8efd3"              # the sheet's own green
LOSER_BG = "#f2f2f2"               # the sheet's own grey
# Colour A..W and stop — 23 columns, the same width enforceChoice_ paints
# (waverole_sync.gs: max mapped column + 1, and נבחר is the last mapped one).
# X..AH are the owner's free columns and AI.. the reference blocks; a wider
# stripe would paint over them (memory: price-sheet-column-tail).
LAST_COL = 23

# Columns W/T/U/V are the owner's, not the scraper's, so they are not in
# HEADER_KEYS. Everything is still found by header TEXT — the owner reorders
# columns and letters in this docstring are only a convenience for the reader.
EXTRA_HEADERS = {
    "fee":      "סליקה",              # T
    "final":    "מחיר סופי",           # U — what the customer pays
    "discount": "מבעצעים (אחוזים)",    # V
    "chosen":   "נבחר",               # W — the tick the sync reads
}
COLUMN_KEYS = {**HEADER_KEYS, **EXTRA_HEADERS}

REQUIRED = ("code", "source", "validity", "price", "changed", "profit", "stock",
            "my_price", "fee", "final", "discount", "chosen")

# The four customer-side cells a switch carries over, in the order they are
# written. U leads because U is the one that decides whether any of it moves.
CARRY = ("final", "my_price", "fee", "discount")


# ── the sheet, as values ────────────────────────────────────────────────────

def text(cell) -> str:
    """Any cell — str, float, int, bool or missing — as text, for the parsers.

    The sheet is read UNFORMATTED, so a numeric cell arrives as a float and a
    text cell as a str, and every parser below has to survive both.
    """
    return "" if cell is None else str(cell)


@dataclass
class Row:
    """One line of the price sheet — only the cells this script reads.

    A cell holds what the API returned for it: str for a text cell, float/int
    for a numeric one. Nothing is coerced on the way in, because a carried cell
    has to be written back with the type it came with.
    """
    row: int                       # 1-based sheet row
    sku: str = ""
    source: object = ""            # D — blank means esim.dog
    price: object = ""             # G — '$0.58' or '$0.56 (€0.48)'
    validity: object = ""          # F — '7d'
    stock: object = ""             # Q — non-empty means: do not sell
    chosen: object = ""            # W
    my_price: object = ""          # S
    fee: object = ""               # T
    final: object = ""             # U
    discount: object = ""          # V
    profit: object = ""            # P

    def get(self, key: str):
        """The cell exactly as the sheet gave it — this is what a carry copies."""
        v = getattr(self, key, "")
        return "" if v is None else v

    def txt(self, key: str) -> str:
        """The cell as text, for reading it."""
        return text(self.get(key))


def source_of(r: Row) -> str:
    """Column D as it should be READ: a blank cell is esim.dog."""
    return text(r.source).strip() or DEFAULT_SOURCE


def source_key(r: Row) -> str:
    """source_of, lower-cased — 'Stellar' and 'stellar' are one supplier."""
    return source_of(r).lower()


# A price cell is '$0.56 (€0.48)' and the dollars come first — but the sheet is
# right-to-left and renders that string backwards (memory:
# rtl-sheet-price-direction), so the '$' is read, never the position on screen.
# A bare number is NOT a price: memory stripe-rtl-price-blindness is the whole
# reason that rule exists.
_USD = re.compile(r"^[\s\u200e\u200f\u202a-\u202e\u2066-\u2069]*\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


def usd(cell) -> Optional[float]:
    """The leading USD amount of a price cell; None for '—', blank or prose."""
    m = _USD.match(text(cell))
    return float(m.group(1).replace(",", "")) if m else None


# מחיר סופי is the owner's own number, typed by hand or computed by his ladder:
# '16.99', 16.99, or '$16.99'. Nothing else is a customer price — not '—', not
# a note to himself, not a blank. Anchored at both ends on purpose: half a cell
# that reads like a price is not a price the site can charge.
_FINAL = re.compile(
    r"^[\s\u200e\u200f\u202a-\u202e\u2066-\u2069]*\$?\s*"
    r"([0-9][0-9,]*(?:\.[0-9]+)?)"
    r"[\s\u200e\u200f\u202a-\u202e\u2066-\u2069]*$")


def final_usd(cell) -> Optional[float]:
    """מחיר סופי as a number — leading '$' optional. None if it is not a price."""
    m = _FINAL.match(text(cell))
    return float(m.group(1).replace(",", "")) if m else None


def days_of(cell) -> Optional[int]:
    """'7d' -> 7. None when the cell holds no number at all."""
    m = re.search(r"\d+", text(cell))
    return int(m.group()) if m else None


def price_num(cell) -> Optional[float]:
    """מחיר שלי, as the scraper reads it — a bare number or a $ one."""
    try:
        return float(text(cell).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def eligible(r: Row) -> bool:
    """Can this row be sold at all today?"""
    return (source_key(r) in SOURCES
            and usd(r.price) is not None
            and not text(r.stock).strip()
            and days_of(r.validity) is not None)


def profit_text(my_price: float, cost: float) -> str:
    """The scraper's own רווח (כדאיות) string — esim_price_scraper.py:1311-1327.

    Percent is of the BUY price, and the leading emoji is not decoration: it
    stops Sheets parsing a leading '+'/'-' as a formula.
    """
    profit_abs = my_price - cost
    profit_pct = (profit_abs / cost) * 100
    sign = "+" if profit_abs >= 0 else "-"
    emoji = "🟢" if profit_abs >= 0 else "🔴"
    return f"{emoji} {sign}${abs(profit_abs):.2f} ({sign}{abs(profit_pct):.1f}%)"


def money(v: Optional[float]) -> str:
    """A cost, for a human. A side with NO price reads '—', never '$0.00': the
    two say opposite things, and '$0.00' says the wrong one — free."""
    return f"${v:.2f}" if v is not None else "—"


def switch_note(old: Row, new: Row) -> str:
    """The J cell of a switch: where it moved from, to, and at what two costs."""
    return (f"↔ ספק: {source_of(old)} → {source_of(new)} "
            f"({money(usd(old.price))} → {money(usd(new.price))})")


# ── the decision ────────────────────────────────────────────────────────────

@dataclass
class Decision:
    sku: str
    action: str = "keep"            # 'switch' | 'keep' | 'skip' | 'deferred'
    reason: str = ""
    incumbent: Optional[Row] = None
    winner: Optional[Row] = None
    writes: list = field(default_factory=list)   # (sheet row, column key, value)
    colours: list = field(default_factory=list)  # (sheet row, hex)

    @property
    def old_cost(self) -> Optional[float]:
        return usd(self.incumbent.price) if self.incumbent else None

    @property
    def new_cost(self) -> Optional[float]:
        return usd(self.winner.price) if self.winner else None


def _cheapest(rows: list[Row]) -> Row:
    """Cheapest; on a tie the LONGER plan, exactly as day_policy breaks ties."""
    return min(rows, key=lambda r: (usd(r.price), -(days_of(r.validity) or 0), r.row))


def _incumbent(rows: list[Row]) -> tuple[Optional[Row], str]:
    """The row the SKU is sold on today, or why that is not a single answer."""
    ticked = [r for r in rows if text(r.chosen).strip()]
    if len(ticked) > 1:
        return None, f"{len(ticked)} rows ticked in נבחר"
    if ticked:
        return ticked[0], ""
    dogs = [r for r in rows if source_key(r) == DEFAULT_SOURCE]
    if len(dogs) > 1:
        return None, f"no tick and {len(dogs)} esim.dog rows"
    if not dogs:
        return None, "no tick and no esim.dog row"
    return dogs[0], ""


def decide(rows: list[Row]) -> list[Decision]:
    """Pure: rows in, one Decision per multi-row SKU out. No sheet, no network."""
    groups: dict[str, list[Row]] = {}
    for r in rows:
        sku = (r.sku or "").strip()
        if sku and "." in sku:
            groups.setdefault(sku, []).append(r)

    out: list[Decision] = []
    for sku, group in groups.items():
        if len(group) < 2:
            continue                       # one supplier: nothing to choose
        inc, why = _incumbent(group)
        if inc is None:
            out.append(Decision(sku=sku, action="skip", reason=why))
            continue                       # and NOT one cell of this SKU is touched

        others = [r for r in group if r is not inc]
        rivals = [r for r in others if eligible(r)]
        winner, reason = inc, ""
        if not eligible(inc):
            if rivals:
                winner = _cheapest(rivals)
                reason = ("no price" if usd(inc.price) is None
                          else f"incumbent {text(inc.stock).strip() or 'not sellable'}")
            else:
                reason = "incumbent not sellable, no challenger either"
        else:
            floor = usd(inc.price) * (1 - day_policy.DAY_TOL)
            cheaper = [r for r in rivals if usd(r.price) < floor]
            if cheaper:
                winner = _cheapest(cheaper)
                gap = (usd(inc.price) - usd(winner.price)) / usd(inc.price) * 100
                reason = f"{gap:.1f}% cheaper"

        d = Decision(sku=sku, incumbent=inc, winner=winner, reason=reason)
        if winner is not inc:
            # The carry gate, read BEFORE anything is written. U is the price
            # the customer is charged; if it does not parse, there is nothing
            # to carry — and if the incumbent is the row the site is selling
            # TODAY (it carries the tick), moving that tick to a row with no
            # customer price takes the SKU off the site. Saving 12 cents of
            # cost is not worth an unsellable package, so the SKU is skipped
            # whole and the owner is told which cell to look at.
            if final_usd(inc.final) is None and text(inc.chosen).strip():
                seen = inc.txt("final").strip() or "blank"
                out.append(Decision(
                    sku=sku, action="skip", incumbent=inc,
                    reason=f"U unreadable ({seen}) — the ticked row has no "
                           f"customer price to carry"))
                continue
            d.action = "switch"
            if text(winner.chosen).strip() != TICK:
                d.writes.append((winner.row, "chosen", TICK))
            for r in group:
                if r is not winner and text(r.chosen).strip():
                    d.writes.append((r.row, "chosen", ""))
            # The customer side of the SKU rides along — but only if there is
            # one. An unreadable U on an UNTICKED incumbent carries nothing:
            # no tick means the site was not selling that row's price anyway.
            if final_usd(inc.final) is not None:
                for key in CARRY:
                    d.writes.append((winner.row, key, inc.get(key)))
            d.writes.append((winner.row, "changed", switch_note(inc, winner)))
            d.colours.append((winner.row, WINNER_BG))
            d.colours.extend((r.row, LOSER_BG) for r in group if r is not winner)

        # P, on every multi-row SKU: the Stellar rows have never had one.
        mine = price_num(inc.my_price)
        if mine is not None:
            for r in group:
                cost = usd(r.price)
                if not text(r.profit).strip() and cost:
                    d.writes.append((r.row, "profit", profit_text(mine, cost)))
        out.append(d)
    return out


def cap_switches(decisions: list[Decision], limit: int) -> int:
    """Keep the first `limit` switches and defer the rest; return how many were
    left. limit <= 0 is unlimited and changes nothing.

    A deferred SKU is not a half-switch: every write and every colour it had is
    dropped, so the sheet keeps the state the owner last had for it. The order
    is the sheet's own, top to bottom, so a phased run walks down the sheet.
    """
    if limit <= 0:
        return 0
    done = left = 0
    for d in decisions:
        if d.action != "switch":
            continue
        if done < limit:
            done += 1
            continue
        d.action = "deferred"
        d.writes = []            # including this SKU's רווח fills: all or nothing
        d.colours = []
        left += 1
    return left


# ── sheet I/O ───────────────────────────────────────────────────────────────

def sheets_service(cred_path: str):
    """Credentials from the environment in the cloud, from a file on a desktop —
    the same order as stellar_prices.sheets_service and the scraper's setup."""
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    env = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if env:
        creds = Credentials.from_service_account_info(json.loads(env), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(cred_path, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet(svc) -> list[list]:
    """A1:X and not one column further — the reference blocks live past it.

    UNFORMATTED_VALUE, so a cell arrives as what it IS: 16.99 for a number,
    '$16.99' for text. The default (FORMATTED_VALUE) renders every cell through
    its display format and hands back a string, which is how a currency format
    on one cell once turned a live price into an unparseable one (memory:
    variant-cell-rendering). Reading raw is also what makes a type-faithful
    carry possible: the write can only preserve a type the read kept.
    """
    return svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="A1:X",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute().get("values", [])


def sheet_id(svc) -> int:
    """The first tab — the one a bare A1:X hits."""
    return svc.spreadsheets().get(
        spreadsheetId=SHEET_ID, fields="sheets(properties(sheetId))",
    ).execute()["sheets"][0]["properties"]["sheetId"]


def read_rows(values: list[list]) -> tuple[list[Row], dict[str, int]]:
    """Rows and the header map. Columns are found by their header TEXT."""
    if not values:
        raise ValueError("price sheet came back empty")
    header = [text(h).strip() for h in values[0]]
    col = {k: header.index(h) for k, h in COLUMN_KEYS.items() if h in header}
    missing = [COLUMN_KEYS[k] for k in REQUIRED if k not in col]
    if missing:
        raise ValueError(f"price sheet header missing: {missing}")
    width = max(col.values()) + 1

    rows = []
    for idx, raw in enumerate(values[1:], start=2):
        # NOT str()-ed: a numeric cell stays a float all the way to the write.
        cells = list(raw) + [""] * (width - len(raw))
        rows.append(Row(row=idx,
                        sku=text(cells[col["code"]]).strip(),
                        source=cells[col["source"]],
                        price=cells[col["price"]],
                        validity=cells[col["validity"]],
                        stock=cells[col["stock"]],
                        chosen=cells[col["chosen"]],
                        my_price=cells[col["my_price"]],
                        fee=cells[col["fee"]],
                        final=cells[col["final"]],
                        discount=cells[col["discount"]],
                        profit=cells[col["profit"]]))
    return rows, col


def _rgb(hex_colour: str) -> dict:
    h = hex_colour.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255,
            "blue": int(h[4:6], 16) / 255}


def _entered_value(value) -> dict:
    """One cell, written back as the TYPE it was read as.

    S/T/U/V are the owner's arithmetic ladder and the sheet's own formulas read
    them, so a carried value has to land as what it was: a number as a number,
    text as text. Nothing is parsed and nothing is converted here — the read
    was UNFORMATTED, so the Python type IS the sheet's type, and '16.99' typed
    as text stays text even though it looks like a number (memory:
    variant-cell-rendering).
    """
    # A cleared cell is an ABSENT userEnteredValue, not an empty string: an
    # empty stringValue is not a value Sheets accepts.
    if value is None or value == "":
        return {}
    if isinstance(value, bool):        # before int — bool IS an int in Python
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value)}}


def _value_request(sid: int, row: int, cidx: int, value) -> dict:
    cell = _entered_value(value)
    return {"updateCells": {
        "range": {"sheetId": sid, "startRowIndex": row - 1, "endRowIndex": row,
                  "startColumnIndex": cidx, "endColumnIndex": cidx + 1},
        "rows": [{"values": [cell]}],
        "fields": "userEnteredValue"}}


def _colour_request(sid: int, row: int, hex_colour: str) -> dict:
    return {"repeatCell": {
        "range": {"sheetId": sid, "startRowIndex": row - 1, "endRowIndex": row,
                  "startColumnIndex": 0, "endColumnIndex": LAST_COL},
        "cell": {"userEnteredFormat": {"backgroundColor": _rgb(hex_colour)}},
        "fields": "userEnteredFormat.backgroundColor"}}


def requests_for(d: Decision, sid: int, col: dict[str, int]) -> list[dict]:
    """Every write one SKU needs, as one list — so it goes in one batchUpdate
    and the sheet can never hold a half-switched SKU."""
    reqs = [_value_request(sid, row, col[key], value)
            for row, key, value in d.writes if key in col]
    reqs += [_colour_request(sid, row, hexc) for row, hexc in d.colours]
    return reqs


def apply(svc, decisions: list[Decision], col: dict[str, int], chunk: int = 200) -> int:
    """Write. A SKU's requests are never split across two calls."""
    sid = sheet_id(svc)
    batches: list[list[dict]] = []
    for d in decisions:
        reqs = requests_for(d, sid, col)
        if not reqs:
            continue
        if batches and len(batches[-1]) + len(reqs) <= chunk:
            batches[-1].extend(reqs)
        else:
            batches.append(list(reqs))
    for body in batches:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID, body={"requests": body}).execute(num_retries=3)
    return sum(len(b) for b in batches)


# ── CLI ─────────────────────────────────────────────────────────────────

_MARK = {"switch": "\u2194", "keep": " ", "deferred": "\u23f8"}


def _describe(d: Decision) -> str:
    if d.action == "skip":
        return f"  {d.sku:<10} \u23ed  SKIPPED \u2014 {d.reason}"
    inc, win = d.incumbent, d.winner
    head = (f"  {d.sku:<10} {_MARK[d.action]} {source_of(inc):<9} {money(d.old_cost):>8}"
            f"  \u2192  {source_of(win):<9} {money(d.new_cost):>8}")
    profits = sum(1 for _, key, _ in d.writes if key == "profit")
    tail = f"   {d.reason}" if d.reason else ""
    if d.action == "deferred":
        tail += "   [held by --max-switches]"
    if profits:
        tail += f"   [+{profits} P]"
    return head + tail


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:            # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write to the sheet (default: dry run)")
    # For a phased first run: the owner may want to watch five switches land
    # before letting the rest go. Nothing in the workflow passes this \u2014 the
    # daily run is unlimited, as it has to be to keep the sheet current.
    ap.add_argument("--max-switches", type=int, default=0, metavar="N",
                    help="stop after N switches (0 = unlimited); the rest are "
                         "held, untouched, and counted in the summary")
    ap.add_argument("--credentials",
                    default=os.environ.get(
                        "SHEETS_CREDENTIALS",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "credentials.json")))
    a = ap.parse_args(argv)

    try:
        svc = sheets_service(a.credentials)
        rows, col = read_rows(read_sheet(svc))
    except Exception as exc:                 # noqa: BLE001
        print(f"\U0001f6d1 could not read the price sheet: {exc}")
        return 2

    decisions = decide(rows)
    left = cap_switches(decisions, a.max_switches)
    print(f"\U0001f4cb {len(rows)} rows, {len(decisions)} SKUs with two supplier rows "
          f"(tolerance {day_policy.DAY_TOL * 100:.0f}%)\n")
    for d in decisions:
        print(_describe(d))

    switched = [d for d in decisions if d.action == "switch"]
    kept = [d for d in decisions if d.action == "keep"]
    skipped = [d for d in decisions if d.action == "skip"]
    cells = sum(len(d.writes) for d in decisions)
    profits = sum(1 for d in decisions for _, key, _ in d.writes if key == "profit")
    print(f"\n\U0001f4ca switched {len(switched)} / kept {len(kept)} / skipped {len(skipped)}"
          f" | {cells} cells ({profits} of them \u05e8\u05d5\u05d5\u05d7) | "
          f"{sum(len(d.colours) for d in decisions)} rows recoloured")
    if left:
        print(f"\u23f8  --max-switches {a.max_switches}: {left} more switch"
              f"{'es' if left != 1 else ''} left for a later run, untouched")
    for d in skipped:
        print(f"   \u23ed  {d.sku}: {d.reason} \u2014 left exactly as it is")

    if not a.apply:
        print("\n(dry run \u2014 nothing written; add --apply to write)")
        return 0
    n = apply(svc, decisions, col)
    print(f"\n\u2705 wrote {n} requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
