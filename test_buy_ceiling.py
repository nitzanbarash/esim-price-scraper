#!/usr/bin/env python3
"""Tests for the owner's buy ceilings (2026-09-17).

A 30GB that costs more than $10 cannot be sold at a price that keeps the
ladder proportional — 14.99/15.99 — so it is not sold at all, whatever the
margin against the sell price says. Spain's 30GB at $11.30 cleared the 20%
bar at 17.99 and still sold 30GB for the price of two 20GB; the buyer who
noticed called it a bug. The same for 40GB over $14 and 50GB over $18.

The rule is judged on the BUY price alone, before and apart from the 20%
profit test, and the regional bundles (1.0A, 1.0B, 2.0, 2.0B) are exempt —
they sell on a ladder of their own.

Run:  python test_buy_ceiling.py
"""
import sys

from esim_price_scraper import (BUY_CEILINGS, CEILING_EXCEPTIONS, OVER_CEILING_LABEL,
                                buy_ceiling, is_profitable, over_ceiling)

failures = 0


def check(label, got, want):
    global failures
    ok = got == want
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: got {got!r}, want {want!r}")


print("-- the ceilings, on their boundaries --")
for gb, want in ((1, None), (5, None), (10, None), (20, None), (29.9, None),
                 (30, 10.0), (39, 10.0), (40, 14.0), (49, 14.0), (50, 18.0), (74.9, 18.0),
                 (75, None), (100, None)):
    check(f"ceiling({gb}GB)", buy_ceiling(gb, '2.34.30'), want)
check("the table itself is what the owner said", BUY_CEILINGS,
      ((30.0, 40.0, 10.0), (40.0, 50.0, 14.0), (50.0, 75.0, 18.0)))

print("\n-- over or not --")
check("Spain 30GB at $11.30 is over", over_ceiling(11.30, 30, '2.34.30'), True)
check("UK 30GB at $8.34 is not", over_ceiling(8.34, 30, '2.44.30'), False)
check("exactly $10 is not over", over_ceiling(10.00, 30, '2.34.30'), False)
check("Germany 40GB at $11.84 is not", over_ceiling(11.84, 40, '2.49.40'), False)
check("Romania 40GB at $18.85 is", over_ceiling(18.85, 40, '2.40.40'), True)
check("Thailand 50GB at $23.49 is", over_ceiling(23.49, 50, '1.66.50'), True)

print("\n-- per-SKU exceptions, by name only --")
check("the list is exactly what was approved by name", CEILING_EXCEPTIONS,
      frozenset({'5.212.50', '1.66.40'}))
check("Thailand 40GB at $14.13 (13 cents over, +40%, 2026-10-06 — pending the owner's word)",
      over_ceiling(14.13, 40, '1.66.40'), False)
check("Morocco 50GB at $18.27 has no ceiling", buy_ceiling(50, '5.212.50'), None)
check("Morocco 50GB at $18.27 is not over", over_ceiling(18.27, 50, '5.212.50'), False)
check("padding does not hide the code", over_ceiling(18.27, 50, ' 5.212.50 '), False)
check("Morocco 20GB keeps no ceiling of its own", buy_ceiling(20, '5.212.20'), None)
check("the exception is one SKU, not a country", over_ceiling(18.27, 50, '5.212.500'), True)
check("nor a size: another 50GB at $18.27 is over", over_ceiling(18.27, 50, '2.34.50'), True)
check("the profit floor still judges it: 24.99 nets 23.64 on $18.27",
      is_profitable(23.64, 18.27, 50), True)
check("and would stop it at $19.71", is_profitable(23.64, 19.71, 50), False)
check("a 20GB has no ceiling at any price", over_ceiling(99.0, 20, '2.49.20'), False)
check("no price is not judged", over_ceiling(None, 30, '2.34.30'), False)
check("no size is not judged", over_ceiling(11.30, None, '2.34.30'), False)

print("\n-- regional bundles are exempt --")
for code in ('1.0A.30', '1.0B.30', '2.0.30', '2.0B.20'):
    check(f"{code} at $16.89", over_ceiling(16.89, 30, code), False)
check("a blank code is judged like a country", over_ceiling(11.30, 30, ''), True)

print("\n-- the ceiling is independent of the 20% test --")
check("Spain 30GB: 17.99 nets 16.92 on 11.30 -> profitable by the old test",
      is_profitable(16.92, 11.30, 30), True)
check("...and over the ceiling all the same", over_ceiling(11.30, 30, '2.34.30'), True)
check("the label the sheet shows", OVER_CEILING_LABEL, 'לא רווחי — מעל תקרה')

print()
if failures:
    print(f"{failures} buy-ceiling test(s) FAILED")
    sys.exit(1)
print("all buy-ceiling tests passed")
