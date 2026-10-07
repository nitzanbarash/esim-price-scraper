#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the price-range rule (price_ranges.py) and its seat in the
chooser (choose_supplier.decide).

The owner's rules, 2026-10-07, each pinned below:
    * every size but 1GB: a 50-cent grid ending .49/.99; UP as many steps as
      it takes when the margin is under 30%; DOWN one step a run when it is
      over 200% and the price is above the low end; no step pays → the SKU
      is 'לא רווחי — מעל טווח' in Q,
    * 1GB: 0.99–1.49 on a 10-cent grid, the LOWEST step that loses at most
      10 cents after the processor's real fee,
    * a blank range cell = hands off (the old 20% floor still judges),
    * the range belongs to the package: carried on a switch, mirrored onto
      the twin, and read off the twin when the owner typed it there,
    * a second run writes nothing.

Run:  python test_price_ranges.py
"""

import sys

import price_ranges as pr
from choose_supplier import (
    OVER_CEILING_LABEL, OVER_RANGE_LABEL, TICK, UNPROFITABLE_LABEL, Row,
    decide, price_moves, profit_text,
)

_fails: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


def row(n, sku, cost, *, gb="10gb", source="esim.dog", final=6.49, rng="5.99 - 7.99",
        chosen=TICK, stock="", **kw):
    """A priced row whose S/T/P agree with its U, so a test that is not about
    them sees no write for them."""
    kw.setdefault("validity", "30d")
    kw.setdefault("discount", "")
    mine = pr.net_of(final) if final is not None else ""
    kw.setdefault("my_price", mine)
    kw.setdefault("fee", pr.table_fee(final) if final is not None else "")
    kw.setdefault("profit", profit_text(mine, cost) if (mine and cost) else "")
    return Row(row=n, sku=sku, gb=gb, source=source, price=f"${cost:.2f}" if cost else "",
               final="" if final is None else final, range=rng, chosen=chosen, stock=stock, **kw)


def writes(d, key):
    return [(r, v) for r, k, v in d.writes if k == key]


def apply_to(rows, decisions):
    by = {r.row: r for r in rows}
    for d in decisions:
        for r, k, v in d.writes:
            setattr(by[r], k, v)


print("-- money, as applyFee_ computes it --")
check("net of 0.99 (4% + 35c, rounded)", pr.net_of(0.99), 0.60)
check("net of 6.49", pr.net_of(6.49), 5.88)
check("net of 24.49", pr.net_of(24.49), 23.16)
check("ladder fee under $2", pr.table_fee(0.99), 0.4)
check("ladder fee at $5", pr.table_fee(5.0), 0.6)
check("ladder fee over $20 is 0, not '-'", pr.table_fee(24.49), 0)
check("margin is net over cost", round(pr.margin_pct(6.49, 4.0), 1), 47.0)

print("\n-- the grid --")
check("1GB: ten-cent steps ending in 9", pr.steps(0.99, 1.49, 1), [0.99, 1.09, 1.19, 1.29, 1.39, 1.49])
check("10GB: fifty-cent steps ending .49/.99", pr.steps(5.99, 7.99, 10), [5.99, 6.49, 6.99, 7.49, 7.99])
check("an off-grid range is snapped inwards", pr.steps(6, 8, 10), [6.49, 6.99, 7.49, 7.99])
check("a range too narrow for one step is empty", pr.steps(6.00, 6.40, 10), [])
check("on_grid .49", pr.on_grid(12.49, 20), True)
check("on_grid .99", pr.on_grid(12.99, 20), True)
check("not on grid .00", pr.on_grid(12.00, 20), False)
check("1GB 1.19 on grid", pr.on_grid(1.19, 1), True)
check("1GB 1.10 off grid", pr.on_grid(1.10, 1), False)

print("\n-- the range cell --")
check("plain", pr.parse_range("5.99 - 7.99"), (5.99, 7.99))
check("no spaces", pr.parse_range("5.99-7.99"), (5.99, 7.99))
check("dollars and an en dash", pr.parse_range("$5.99 – $7.99"), (5.99, 7.99))
check("bidi marks", pr.parse_range("‏5.99 - 7.99‎"), (5.99, 7.99))
check("a single number is not a range", pr.parse_range("5.99"), None)
check("backwards is refused", pr.parse_range("8 - 5"), None)
check("prose is refused", pr.parse_range("לא לגעת"), None)
check("blank is None (= hands off)", pr.parse_range(""), None)
check("None is None", pr.parse_range(None), None)
check("a numeric cell is not a range", pr.parse_range(5.99), None)
check("format", pr.format_range(5.99, 7.99), "5.99 - 7.99")

print("\n-- 1GB: the lowest step that loses at most 10c --")
check("cost 0.55 → 0.99 (net 0.60)", pr.reprice(1, 0.55, 1.29, 0.99, 1.49), 0.99)
check("cost 0.70 → 0.99 exactly at the 10c line", pr.reprice(1, 0.70, 0.99, 0.99, 1.49), 0.99)
check("cost 0.71 → 1.09", pr.reprice(1, 0.71, 0.99, 0.99, 1.49), 1.09)
check("cost 0.87 → 1.19 (net 0.79, loss 8c)", pr.reprice(1, 0.87, 1.29, 0.99, 1.49), 1.19)
check("cost 1.18 → 1.49 (net 1.08)", pr.reprice(1, 1.18, 0.99, 0.99, 1.49), 1.49)
check("cost 1.19 → nothing in the range pays", pr.reprice(1, 1.19, 0.99, 0.99, 1.49), None)
check("the current price is ignored: it always comes down", pr.reprice(1, 0.55, 1.49, 0.99, 1.49), 0.99)
check("a regional 1GB with its own range uses the 1GB grid (2.89 nets 2.42, loses 7c)",
      pr.reprice(1, 2.49, 2.49, 2.49, 2.99), 2.89)

print("\n-- the others: easy up, hard down --")
check("under 30% → up as far as it takes (Canada 5GB, cost 4.25)",
      pr.reprice(5, 4.25, 4.99, 4.99, 6.49), 6.49)
check("Greece 50GB cost 15.91 at 20.99 (24%) → 21.99", pr.reprice(50, 15.91, 20.99, 16.99, 22.99), 21.99)
check("30% exactly stays", pr.reprice(10, 4.00, 5.99, 5.99, 7.99), 5.99 if pr.margin_pct(5.99, 4.0) >= 30 else 6.49)
check("inside the band: stays", pr.reprice(10, 3.0, 6.49, 5.99, 7.99), 6.49)
check("over 200% → ONE step down", pr.reprice(10, 2.0, 6.99, 5.99, 7.99), 6.49)
check("over 200% at the low end stays", pr.reprice(10, 1.5, 5.99, 5.99, 7.99), 5.99)
check("still over 200% after one step: the next run takes the next",
      pr.reprice(10, 1.5, 6.49, 5.99, 7.99), 5.99)
check("no step pays → None (Canada 20GB, cost 12.90)", pr.reprice(20, 12.9, 14.49, 14.49, 17.49), None)
check("off-grid current is snapped up first", pr.reprice(10, 3.0, 7.37, 5.99, 7.99), 7.49)
check("current above the high end is pulled to it", pr.reprice(10, 3.0, 9.99, 5.99, 7.99), 7.99)
check("…and if even that does not pay, None", pr.reprice(10, 7.0, 9.99, 5.99, 7.99), None)
check("current below the low end is lifted to it", pr.reprice(10, 3.0, 4.99, 5.99, 7.99), 5.99)
check("no current price: from the low end", pr.reprice(10, 4.5, None, 5.99, 7.99), 6.49)
check("no cost: None", pr.reprice(10, None, 6.49, 5.99, 7.99), None)
check("an empty grid: None", pr.reprice(10, 3.0, 6.49, 6.00, 6.40), None)
check("feasible: some step pays", pr.feasible(10, 5.0, (5.99, 7.99)), True)
check("feasible: none does", pr.feasible(10, 7.0, (5.99, 7.99)), False)
check("feasible: no range → unjudged", pr.feasible(10, 7.0, None), None)
check("feasible: no cost → unjudged", pr.feasible(10, None, (5.99, 7.99)), None)

print("\n-- seeding: the cheapest live price per size, a width above it --")
seed = pr.seed_ranges([
    ("1.66.10", 10.0, 5.99, True), ("2.39.10", 10.0, 6.49, True),
    ("1.63.10", 10.0, 9.99, True),            # an outlier: its own window
    ("2.0.10", 10.0, 6.99, True),             # regional: its own window
    ("3.1C.10", 10.0, 5.49, False),           # off sale: not a floor
    ("1.86.20", 20.0, 9.99, True), ("1.60.20", 20.0, 13.99, True),
    ("2.44.1", 1.0, 1.29, True), ("1.0A.1", 1.0, 2.49, True),
    ("9.9.5", 5.0, None, True),               # no price: no range
])
check("10GB floor is the cheapest live non-regional", seed["1.66.10"], (5.99, 7.99))
check("every ordinary 10GB gets the same window", seed["2.39.10"], (5.99, 7.99))
check("a 10GB above the window gets its own", seed["1.63.10"], (9.99, 11.99))
check("a regional bundle gets its own", seed["2.0.10"], (6.99, 8.99))
check("an off-sale row is not the floor, but is ranged", seed["3.1C.10"], (5.99, 7.99))
check("20GB: three dollars wide", seed["1.86.20"], (9.99, 12.99))
check("20GB outlier (13.99 > 12.99)", seed["1.60.20"], (13.99, 16.99))
check("1GB is 0.99–1.49 whatever it sells at", seed["2.44.1"], (0.99, 1.49))
check("regional 1GB: its own, fifty cents wide", seed["1.0A.1"], (2.49, 2.99))
check("no price, no range", "9.9.5" in seed, False)
check("widths grow with size", [pr.seed_width(g) for g in (5, 10, 20, 30, 50, 100)],
      [1.5, 2.0, 3.0, 4.0, 6.0, 10.0])

print("\n-- in the chooser: a one-supplier SKU --")
r = row(2, "3.1C.5", 4.25, gb="5gb", final=4.99, rng="4.99 - 6.49")
d = decide([r])
check("one decision", len(d), 1)
check("U moves to the lowest paying step", writes(d[0], "final"), [(2, 6.49)])
check("S follows (net of the real fee)", writes(d[0], "my_price"), [(2, 5.88)])
check("T follows (the ladder)", writes(d[0], "fee"), [(2, 0.8)])
check("P is against the NEW S", writes(d[0], "profit"), [(2, profit_text(5.88, 4.25))])
check("Q stays blank: it pays now", writes(d[0], "stock"), [])
check("the move is reported", d[0].price_move, (4.99, 6.49))
check("price_moves lists it", price_moves(d), [("3.1C.5", 2, 4.99, 6.49, 4.25, "4.99 - 6.49")])
apply_to([r], d)
check("a second run writes nothing", decide([r]), [])

r = row(2, "3.1C.20", 12.9, gb="20gb", final=14.49, rng="14.49 - 17.49", stock=UNPROFITABLE_LABEL)
d = decide([r])
check("nothing in the range pays → the range word in Q", writes(d[0], "stock"), [(2, OVER_RANGE_LABEL)])
check("…and the price is NOT moved", writes(d[0], "final"), [])
check("…no move reported", d[0].price_move, None)
apply_to([r], d)
r.price = "$9.00"
d = decide([r])
check("cost falls: the word is cleared", writes(d[0], "stock"), [(2, "")])
check("…and the price stays: 14.49 already pays 50% on $9", writes(d[0], "final"), [])
r.price = "$11.00"
d = decide([r])
check("cost 11: 14.49 nets 13.56 (23%); 14.99 nets 14.04 (28%); 15.49 nets 14.52 (32%)", writes(d[0], "final"), [(2, 15.49)])

r = row(2, "1.66.10", 7.0, final=6.49, rng="")
d = decide([r])
check("blank range: the price is left alone even at a loss", writes(d[0], "final"), [])
check("…and the old 20% floor still judges", writes(d[0], "stock"), [(2, UNPROFITABLE_LABEL)])

r = row(2, "2.44.1", 0.61, gb="1gb", final=0.99, rng="0.99 - 1.49")
d = decide([r])
check("1GB at a 1c loss with a range: no word, no move (margin -1.6% < 20% floor would have)",
      (writes(d[0], "stock"), writes(d[0], "final")) if d else ([], []), ([], []))
r = row(2, "2.31.1", 0.61, gb="1gb", final=1.29, rng="0.99 - 1.49")
d = decide([r])
check("1GB priced high comes DOWN to 0.99", writes(d[0], "final"), [(2, 0.99)])
r = row(2, "1.886.1", 2.49, gb="1gb", final=0.99, rng="0.99 - 1.49", stock=UNPROFITABLE_LABEL)
d = decide([r])
check("1GB that cannot pay inside 0.99–1.49: the range word", writes(d[0], "stock"), [(2, OVER_RANGE_LABEL)])

r = row(2, "2.48.30", 11.89, gb="30gb", final=13.99, rng="13.49 - 17.49", stock=OVER_CEILING_LABEL)
d = decide([r])
check("the buy ceiling is still judged first (30GB over $10)", writes(d[0], "stock"), [])
check("…though the price is moved to where it would pay", writes(d[0], "final"), [(2, 16.49)])

r = row(2, "1.66.10", 2.0, final=6.99)
d = decide([r])
check("over 200%: one step down", writes(d[0], "final"), [(2, 6.49)])
r = row(2, "1.66.10", 2.0, final=5.99)
check("over 200% at the low end: nothing", decide([r]), [])
r = row(2, "1.66.10", 3.0, final=6.49, chosen="")
check("an unticked solo row is left alone", decide([r]), [])
r = row(2, "1.66.10", None, final=6.49)
check("no cost: no move, no word", decide([r]), [])

print("\n-- in the chooser: a two-supplier SKU --")
a = row(2, "1.66.10", 4.5, final=5.99)                               # dog, ticked, 25%
b = row(3, "1.66.10", 5.2, source="Stellar", final=5.99, chosen="")  # dearer twin
d = decide([a, b])
check("one decision, kept", (len(d), d[0].action), (1, "keep"))
check("the ✓ row's U moves up to pay 30% on ITS cost — and the twin is mirrored it",
      writes(d[0], "final"), [(2, 6.49), (3, 6.49)])
check("the twin is mirrored the new S (its T already said 0.8)",
      [w for w in d[0].writes if w[0] == 3 and w[1] in ("my_price", "fee")], [(3, "my_price", 5.88)])
check("P on both rows, each against its own cost and the new S",
      writes(d[0], "profit"), [(2, profit_text(5.88, 4.5)), (3, profit_text(5.88, 5.2))])
check("the twin, which pays at 6.49 too, gets no word", writes(d[0], "stock"), [])
apply_to([a, b], d)
check("second run: nothing", [x.writes for x in decide([a, b])], [[]])

a = row(2, "1.66.10", 4.5, final=5.99, rng="")
b = row(3, "1.66.10", 5.2, source="Stellar", final=5.99, chosen="", rng="5.99 - 7.99")
d = decide([a, b])
check("range typed on the TWIN: obeyed", writes(d[0], "final"), [(2, 6.49), (3, 6.49)])
check("…and copied onto the ✓ row, not wiped off the twin", writes(d[0], "range"), [(2, "5.99 - 7.99")])
apply_to([a, b], d)
check("second run: nothing", [x.writes for x in decide([a, b])], [[]])

a = row(2, "1.66.10", 7.5, final=6.49)                               # dog ticked, cannot pay
b = row(3, "1.66.10", 7.6, source="Stellar", final=6.49, chosen="")
d = decide([a, b])
check("neither row can pay inside the range: both get the word",
      writes(d[0], "stock"), [(2, OVER_RANGE_LABEL), (3, OVER_RANGE_LABEL)])
check("…price untouched", writes(d[0], "final"), [])

a = row(2, "1.66.10", 6.0, final=5.99)                               # ticked dog, costs 6
b = row(3, "1.66.10", 4.0, source="Stellar", final=5.99, chosen="")  # 33% cheaper
d = decide([a, b])
check("a switch", d[0].action, "switch")
check("the range rides the carry onto the winner", writes(d[0], "range"), [(3, "5.99 - 7.99")])
check("the winner's price is judged on the WINNER's cost: 5.99 pays 4.00, stays",
      writes(d[0], "final"), [(3, 5.99)])
a = row(2, "1.66.10", 6.0, final=5.99)
b = row(3, "1.66.10", 4.8, source="Stellar", final=5.99, chosen="")  # cheaper, but 5.99 pays only 17%
d = decide([a, b])
check("switch + reprice: the carried U is the MOVED one (6.49 nets 22%, 6.99 nets 32%)",
      writes(d[0], "final"), [(3, 6.99), (2, 6.99)])
check("…and so is the carried S", writes(d[0], "my_price"), [(3, 6.36), (2, 6.36)])
check("reported once", d[0].price_move, (5.99, 6.99))

print()
if _fails:
    print(f"{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("all price-range tests passed")
