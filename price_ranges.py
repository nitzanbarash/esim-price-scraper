#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The sell price, derived from the cost inside a range the owner sets.

Until 2026-10-07 'מחיר סופי' (U) was typed by hand and nothing followed the
cost: when Stellar repriced Mexico the packages sold at a loss for four days,
and when a cost fell the margin quietly climbed past 100%. The owner's rule,
given on 2026-10-07, is a RANGE per package in a column of its own
('טווח מחירים', V — between 'מחיר סופי' and the sale percentage):

    5.99 - 7.99

The low end is the price the package should ideally sell at and the lowest it
may ever go; the high end is the price past which the package is too dear to
sell at all. Inside the range the bot moves U on the chooser's 4-hourly run
— and the sheet's own script (waverole_sync.gs applyFee_, a JavaScript copy
of this rule) makes the same move the moment the owner types a cost, a
price, a range or moves the tick; test_waverole_sync.js holds the two
copies to the same answers:

  * every package but 1GB: prices sit on a 50-cent grid ending in .49/.99.
    The price goes UP, as many steps as it takes, the moment the margin falls
    below 30% of cost (RAISE_BELOW_PCT) — and if no step up to the high end
    pays 30%, the package is marked 'לא רווחי — מעל טווח' in Q, which the site
    shows as sold out. It goes DOWN one step a run, and only while the margin
    is above 200% (LOWER_ABOVE_PCT) and the price is above the low end. Easy
    up, hard down — the owner's words.
  * 1GB: a fixed 0.99–1.49 range on a 10-cent grid ending in 9. These are
    the loss leaders: the bot always takes the LOWEST step whose loss, after
    the processor's real fee, is at most 10 cents (LOSS_1GB). Past 1.49 the
    package is off sale.

The margin is judged on 'מחיר שלי' (S) — U less the processor's real cut of
4% + $0.35 (memory: fee-ladder-vs-real-fee) — exactly as 'רווח' (P) reads
it. Every write of U here rewrites S and T too, the way applyFee_ in
waverole_sync.gs does on a hand edit, because API writes never fire onEdit.

A blank range cell is the OFF switch: the bot leaves that package's price
alone. The owner tunes a range by typing over it, on the ✓ row of the SKU
(the chooser mirrors U/S/T/V and the range onto the twin row).

`seed_ranges()` fills the column the first time, by the owner's recipe: for
each size the low end is the cheapest live non-regional price on the site,
the high end a size-dependent width above it; a package priced above that
window gets a window of its own starting at its price, and so does every
regional bundle (a '1.0A.x' / '2.0.x' code — they are priced apart).

Run:  python price_ranges.py --seed            # what the column would get
      python price_ranges.py --seed --apply    # insert the column + fill it
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Iterable, Optional

RANGE_HEADER = "טווח מחירים"          # V — the column this module owns
OVER_RANGE_LABEL = "לא רווחי — מעל טווח"   # Q: no price in the range pays

# The processor's real cut (waverole_sync.gs REAL_FEE_RATE/FIXED) and the
# ladder fee the customer is shown (FEE_LADDER / FEE_OVER_TOP). Both are
# copied from the Apps Script so a bot-written row reads like a typed one.
REAL_FEE_RATE = 0.04
REAL_FEE_FIXED = 0.35
FEE_LADDER = ((2, 0.4), (5, 0.6), (10, 0.8), (15, 1.0), (20, 1.2))
FEE_OVER_TOP = 0

RAISE_BELOW_PCT = 30.0     # margin under this → price goes up
LOWER_ABOVE_PCT = 200.0    # margin over this → price goes down one step
STEP = 0.50                # the grid for every size but 1GB (…49 / …99)
STEP_1GB = 0.10            # the 1GB grid (…9)
LOSS_1GB = 0.10            # 1GB may lose up to this, fee included
RANGE_1GB = (0.99, 1.49)   # 1GB's range, the same for every country

# How wide a seeded range is, by size: (size up to and including, width).
# The owner's own examples were 10GB 5.99–7.99 and 20GB 9.99–12.99, "a bit
# wider as the size goes up"; the rest is interpolated and his to retune.
SEED_WIDTHS = ((1, 0.50), (3, 1.00), (5, 1.50), (10, 2.00), (15, 2.50),
               (20, 3.00), (30, 4.00), (40, 5.00), (50, 6.00), (75, 8.00),
               (100, 10.00))

_EPS = 1e-6


# ── money ────────────────────────────────────────────────────────────────

def real_fee(final: float) -> float:
    """What the processor keeps of a sale at `final`: 4% + 35c, in cents."""
    return round(final * REAL_FEE_RATE + REAL_FEE_FIXED, 2)


def net_of(final: float) -> float:
    """'מחיר שלי' for a sale at `final` — what applyFee_ writes into S."""
    return round(final - real_fee(final), 2)


def table_fee(final: float) -> float:
    """'סליקה' for a sale at `final` — the ladder fee the customer is shown."""
    for top, fee in FEE_LADDER:
        if final <= top:
            return fee
    return FEE_OVER_TOP


def margin_pct(final: float, cost: float) -> float:
    """The margin P reports: net over cost, as a percentage of cost."""
    return (net_of(final) - cost) / cost * 100.0


def is_1gb(gb: Optional[float]) -> bool:
    return gb is not None and gb <= 1


def pays(final: float, cost: float, gb: Optional[float]) -> bool:
    """Does a sale at `final` satisfy the owner's floor for this size?"""
    if is_1gb(gb):
        return net_of(final) - cost >= -LOSS_1GB - _EPS
    return margin_pct(final, cost) >= RAISE_BELOW_PCT - _EPS


# ── the grid ─────────────────────────────────────────────────────────────

def _cents(v: float) -> int:
    return int(round(v * 100))


def on_grid(v: float, gb: Optional[float]) -> bool:
    """1GB: any price ending in 9 cents; the rest: .49 or .99."""
    c = _cents(v)
    return c % 10 == 9 if is_1gb(gb) else c % 50 == 49


def _step_cents(gb: Optional[float]) -> int:
    return _cents(STEP_1GB if is_1gb(gb) else STEP)


def grid_up(v: float, gb: Optional[float]) -> float:
    """The smallest grid price at or above `v`."""
    c = _cents(v)
    while not on_grid(c / 100, gb):
        c += 1
    return c / 100


def grid_down(v: float, gb: Optional[float]) -> float:
    """The largest grid price at or below `v` (never below one step)."""
    c = _cents(v)
    while c > 0 and not on_grid(c / 100, gb):
        c -= 1
    return c / 100


def steps(lo: float, hi: float, gb: Optional[float]) -> list[float]:
    """Every grid price inside [lo, hi], ascending. A range typed off the
    grid ('6 - 8') is snapped inwards; one too narrow to hold a step is []."""
    first, last, step = grid_up(lo, gb), grid_down(hi, gb), _step_cents(gb)
    out, c = [], _cents(first)
    while c <= _cents(last):
        out.append(c / 100)
        c += step
    return out


# ── the range cell ───────────────────────────────────────────────────────

_NUM = r"\$?\s*([0-9]+(?:[.,][0-9]+)?)"
_RANGE = re.compile(rf"^\s*{_NUM}\s*[-–—~]+\s*{_NUM}\s*$")
_BIDI = re.compile(r"[​‎‏‪-‮⁦-⁩]")


def parse_range(cell) -> Optional[tuple[float, float]]:
    """'5.99 - 7.99' → (5.99, 7.99); anything else → None (= no automation).
    Both ends must be positive and in order; a single number is not a range."""
    m = _RANGE.match(_BIDI.sub("", "" if cell is None else str(cell)))
    if not m:
        return None
    lo, hi = (float(m.group(i).replace(",", ".")) for i in (1, 2))
    if lo <= 0 or hi < lo:
        return None
    return lo, hi


def format_range(lo: float, hi: float) -> str:
    return f"{lo:.2f} - {hi:.2f}"


# ── the rule ─────────────────────────────────────────────────────────────

def reprice(gb: Optional[float], cost: float, current: Optional[float],
            lo: float, hi: float) -> Optional[float]:
    """The price U should hold now, or None when nothing in the range pays.

    `current` is what U holds today (None when unknown or off the sheet): the
    non-1GB rule moves FROM it — up as far as it takes to pay, down one step
    when the margin is over 200% — so the answer depends on where the price
    is. 1GB ignores it: the lowest paying step, every time.
    """
    grid = steps(lo, hi, gb)
    if not grid or cost is None or cost <= 0:
        return None
    if is_1gb(gb):
        return next((s for s in grid if pays(s, cost, gb)), None)

    if current is None or current < grid[0]:
        cur = grid[0]
    elif current > grid[-1]:
        cur = grid[-1]
    else:
        cur = min(grid_up(current, gb), grid[-1])
    if not pays(cur, cost, gb):
        return next((s for s in grid if s > cur and pays(s, cost, gb)), None)
    if margin_pct(cur, cost) > LOWER_ABOVE_PCT + _EPS and cur > grid[0]:
        return grid[grid.index(cur) - 1]        # cur is on the grid by now
    return cur


def feasible(gb: Optional[float], cost: Optional[float], rng: Optional[tuple]) -> Optional[bool]:
    """Is there ANY price in the range that pays at this cost? None = unjudged
    (no range, or no cost)."""
    if rng is None or cost is None:
        return None
    return reprice(gb, cost, None, *rng) is not None


# ── seeding the column ───────────────────────────────────────────────────

REGIONAL_CODE_RE = re.compile(r"^\d+\.0[A-Z]?\.")   # as esim_price_scraper


def seed_width(gb: float) -> float:
    for top, width in SEED_WIDTHS:
        if gb <= top + _EPS:
            return width
    return SEED_WIDTHS[-1][1]


def seed_ranges(skus: Iterable[tuple]) -> dict[str, tuple[float, float]]:
    """{sku: (lo, hi)} from (sku, gb, final, live) tuples — one per SKU, the
    ✓ row's size and customer price, `live` = on sale (ticked, Q blank).

    The owner's recipe: per size, the low end is the cheapest LIVE
    non-regional price; a package above low+width is an outlier and gets
    its own window from its own price; regional bundles always get their
    own; 1GB is RANGE_1GB for every country. A SKU with no readable price
    gets nothing — a blank cell is the off switch, and that is the right
    state for a package nobody has priced.
    """
    floor: dict[float, float] = {}
    for sku, gb, final, live in skus:
        if gb is None or final is None or not live or REGIONAL_CODE_RE.match(sku):
            continue
        floor[gb] = min(floor.get(gb, final), final)

    out: dict[str, tuple[float, float]] = {}
    for sku, gb, final, live in skus:
        if gb is None or final is None:
            continue
        width = seed_width(gb)
        if REGIONAL_CODE_RE.match(sku):
            lo = grid_down(final, gb)
        elif is_1gb(gb):
            lo, width = RANGE_1GB[0], RANGE_1GB[1] - RANGE_1GB[0]
        else:
            lo = grid_down(floor.get(gb, final), gb)
            if final > lo + width + _EPS:          # an outlier: its own window
                lo = grid_down(final, gb)
        out[sku] = (round(lo, 2), round(lo + width, 2))
    return out


# ── CLI: the one-off seed ────────────────────────────────────────────────

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:            # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--seed", action="store_true",
                    help="fill blank range cells by the owner's recipe "
                         "(inserting the column if it is missing)")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--credentials", default=None)
    a = ap.parse_args(argv)
    if not a.seed:
        ap.print_help()
        return 0

    import os
    import choose_supplier as cs      # imported here: choose_supplier imports us
    cred = a.credentials or os.environ.get(
        "SHEETS_CREDENTIALS",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json"))
    svc = cs.sheets_service(cred)
    values = cs.read_sheet(svc)
    header = [cs.text(h).strip() for h in values[0]]
    sid = cs.sheet_id(svc)
    reqs: list[dict] = []

    if RANGE_HEADER not in header:
        at = header.index(cs.EXTRA_HEADERS["discount"])
        print(f"➕ inserting column {at + 1} '{RANGE_HEADER}' before '{header[at]}'")
        if a.apply:
            svc.spreadsheets().batchUpdate(spreadsheetId=cs.SHEET_ID, body={"requests": [
                {"insertDimension": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                                               "startIndex": at, "endIndex": at + 1},
                                     "inheritFromBefore": True}},
                {"updateCells": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                                           "startColumnIndex": at, "endColumnIndex": at + 1},
                                 "rows": [{"values": [{"userEnteredValue": {"stringValue": RANGE_HEADER}}]}],
                                 "fields": "userEnteredValue"}},
            ]}).execute()
            values = cs.read_sheet(svc)
        else:
            for row in values:
                row[at:at] = [""]
            values[0][at] = RANGE_HEADER
    rows, col = cs.read_rows(values)

    groups: dict[str, list] = {}
    for r in rows:
        if r.sku and "." in r.sku:
            groups.setdefault(r.sku, []).append(r)
    skus = []
    for sku, group in groups.items():
        chosen = next((r for r in group if cs.text(r.chosen).strip()), None)
        if chosen is None:
            continue
        skus.append((sku, cs.gb_of(chosen.gb), cs.final_usd(chosen.final),
                     not cs.stock_word(chosen)))
    seeded = seed_ranges(skus)

    n = 0
    for sku, (lo, hi) in sorted(seeded.items()):
        group = groups[sku]
        if any(not cs._blank(r.range) for r in group):
            continue                      # the owner (or a run) already set one
        cell = format_range(lo, hi)
        chosen = next(r for r in group if cs.text(r.chosen).strip())
        kind = ("regional" if REGIONAL_CODE_RE.match(sku) else
                "1GB" if is_1gb(cs.gb_of(chosen.gb)) else "")
        print(f"  {sku:<10} {cs.text(chosen.gb):>6}  U={cs.final_usd(chosen.final):>6}"
              f"  → {cell}  {kind}")
        for r in group:
            reqs.append(cs._value_request(sid, r.row, col["range"], cell))
            n += 1
    print(f"\n{len(seeded)} SKUs ranged, {n} cells to write")
    if not a.apply:
        print("(dry run — nothing written; add --apply to write)")
        return 0
    for i in range(0, len(reqs), 200):
        svc.spreadsheets().batchUpdate(
            spreadsheetId=cs.SHEET_ID, body={"requests": reqs[i:i + 200]}).execute(num_retries=3)
    print(f"✅ wrote {n} range cells")
    return 0


if __name__ == "__main__":
    sys.exit(main())
