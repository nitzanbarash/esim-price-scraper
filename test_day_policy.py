#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for which VALIDITY each size is sold at, per supplier.

These pin the owner's day rules of 2026-09-10 (quoted in full in the module
docstring of day_policy.py). Each one is a way of not selling the customer a
shorter trip than the sheet promised:

    * 30 days does not move for a rounding error (<=1% cheaper stays on 30),
    * closest-to-30 wins next, and a tie goes to the LONGER plan (21/25 -> 25),
    * a size that is out of its day window is not a candidate at all, however
      cheap it is — that is the 10GB-at-14-days trap,
    * Stellar's long plans are only reachable from 30GB up, and only at no
      extra money.

Run:  python test_day_policy.py
"""

import sys

from day_policy import (
    DAY_CEILING, DAY_TOL, ESIMDOG, STELLAR, SUPPLIERS,
    day_ceiling, day_floor, in_window, pick,
)

_fails: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


def days_of(chosen):
    return None if chosen is None else chosen[0]


print("-- the two suppliers, spelled the way the sheet spells them --")
check("suppliers", SUPPLIERS, ("esim.dog", "stellar"))
check("tolerance", DAY_TOL, 0.01)

print("\n-- 30 days does not move for small change --")
# 10GB, so the floor is 20 and both 30 and 25 are in the window.
check("1% cheaper is not cheaper enough",
      days_of(pick(10, ESIMDOG, [(30, 10.00), (25, 9.90)])), 30)
check("equal price stays on 30",
      days_of(pick(10, ESIMDOG, [(30, 10.00), (25, 10.00)])), 30)
check("dearer stays on 30",
      days_of(pick(10, ESIMDOG, [(30, 10.00), (25, 10.40)])), 30)
check("more than 1% cheaper does win",
      days_of(pick(10, ESIMDOG, [(30, 10.00), (25, 9.80)])), 25)

print("\n-- second preference: as close to 30 as possible --")
check("21 or 25 at one price -> 25",
      days_of(pick(10, ESIMDOG, [(21, 8.00), (25, 8.00)])), 25)
check("31 is nearer than 29",
      days_of(pick(10, ESIMDOG, [(31, 8.00), (29, 8.00)])), 31)
check("with no 30 on the page the nearest wins",
      days_of(pick(10, ESIMDOG, [(20, 8.00), (25, 8.00), (21, 8.00)])), 25)
# Every hop is measured against the CURRENT holder, not against 30. So 9.75 —
# a full 2.5% under the 30-day price — still loses once 29 days is holding the
# row at 9.80, because it does not beat 9.80 by 1%.
check("the tolerance is measured against the incumbent, not against 30",
      days_of(pick(10, ESIMDOG, [(30, 10.00), (29, 9.80), (25, 9.75)])), 29)
# And the flip side, stated plainly: the incumbent CAN be walked down step by
# step, as long as every single step is worth more than 1% on its own.
check("each step that clears 1% does move the row",
      days_of(pick(10, ESIMDOG, [(30, 10.00), (29, 9.85), (25, 9.70)])), 25)

print("\n-- Germany 1GB, the real curve (memory: cheapest-day-search) --")
GERMANY_1GB = [(1, 1.99), (7, 0.65), (15, 1.07), (30, 2.99)]
check("1GB takes the cheapest day count",
      days_of(pick(1, ESIMDOG, GERMANY_1GB)), 7)
check("...and 1GB's floor is 1, so nothing on that curve is excluded",
      day_floor(1, ESIMDOG), 1)

print("\n-- a validity below the floor is not a candidate at any price --")
# The 10GB rows the sheet carried at 14 days: cheap, and not for sale.
check("10GB at 14 days is out of the window", in_window(10, ESIMDOG, 14), False)
check("...so a half-price 14d loses to a full-price 30d",
      days_of(pick(10, ESIMDOG, [(30, 14.00), (14, 7.00)])), 30)
check("...and on its own it leaves the row with nothing",
      pick(10, ESIMDOG, [(14, 7.00)]), None)
check("no candidates at all", pick(10, ESIMDOG, []), None)

print("\n-- Stellar's long plans: 30GB and up, and only for free --")
STELLAR_30GB = [(30, 39.00), (60, 39.00)]
check("60 days at the same price as 30 -> 60",
      days_of(pick(30, STELLAR, STELLAR_30GB)), 60)
check("60 days 0.5% dearer -> stay on 30",
      days_of(pick(30, STELLAR, [(30, 39.00), (60, 39.20)])), 30)
check("60 days a shekel cheaper is of course taken",
      days_of(pick(30, STELLAR, [(30, 39.00), (60, 38.00)])), 60)
check("90 and 60 both free -> the longest",
      days_of(pick(30, STELLAR, [(30, 39.00), (60, 39.00), (90, 39.00)])), 90)
check("10GB at 60 days is not a Stellar candidate", in_window(10, STELLAR, 60), False)
check("...even at half price",
      days_of(pick(10, STELLAR, [(30, 14.00), (60, 7.00)])), 30)
check("esim.dog is never asked past 31, whatever the size",
      in_window(30, ESIMDOG, 60), False)
check("...and the free-long-plan exception is Stellar's alone",
      days_of(pick(50, ESIMDOG, [(30, 39.00), (60, 39.00)])), 30)

print("\n-- the ceilings --")
check("esim.dog ceiling, small", day_ceiling(1, ESIMDOG), 31)
check("esim.dog ceiling, big", day_ceiling(50, ESIMDOG), 31)
check("Stellar ceiling under 30GB", day_ceiling(29, STELLAR), 31)
check("Stellar ceiling at 30GB is no ceiling", day_ceiling(30, STELLAR), None)
check("the ceiling constant", DAY_CEILING, 31)

print("\n-- 50GB: 'above 32 days' is a Stellar rule only --")
check("Stellar 50GB floor", day_floor(50, STELLAR), 33)
check("esim.dog 50GB floor stays at 30", day_floor(50, ESIMDOG), 30)
check("esim.dog 50GB still has a window", in_window(50, ESIMDOG, 30), True)
check("Stellar 50GB refuses 30 days", in_window(50, STELLAR, 30), False)
check("Stellar 50GB takes 33", in_window(50, STELLAR, 33), True)
# 30 days is under the floor here, so the cheap row is not on the table at all;
# 33 takes it, and then 60 takes it off 33 for the same money.
check("Stellar 50GB ignores the sub-floor 30d and lands on 60",
      days_of(pick(50, STELLAR, [(30, 55.00), (33, 59.00), (60, 59.00)])), 60)
check("...but a dearer 60 leaves it on 33",
      days_of(pick(50, STELLAR, [(30, 55.00), (33, 59.00), (60, 62.00)])), 33)

print("\n-- the floor bands, on their boundaries --")
for gb, want in ((1, 1), (2, 1), (2.5, 5), (3, 5), (5, 5), (6, 10), (9, 10),
                 (10, 20), (19, 20), (20, 25), (30, 25), (31, 30), (49, 30)):
    check(f"floor({gb}GB)", day_floor(gb, ESIMDOG), want)
check("the bands do not depend on the supplier below 50GB",
      [day_floor(g, STELLAR) for g in (2, 5, 9, 19, 30, 31)], [1, 5, 10, 20, 25, 30])

print("\n-- candidates may be objects or dicts, and ride back whole --")


class Package:
    def __init__(self, days, price, code):
        self.days, self.price, self.code = days, price, code


chosen = pick(10, ESIMDOG, [Package(30, 10.00, "A"), Package(25, 8.00, "B")])
check("an object candidate is returned as given", chosen.code, "B")
chosen = pick(10, ESIMDOG, [{"days": 30, "price": 10.0, "sku": "1.972.01"},
                            {"days": 25, "price": 9.99, "sku": "1.972.02"}])
check("a dict candidate is returned as given", chosen["sku"], "1.972.01")
check("payload after (days, price) is carried through",
      pick(10, ESIMDOG, [(25, 8.00, "link-25"), (30, 10.00, "link-30")])[2], "link-25")

print("\n-- an unknown supplier is refused, not guessed at --")
try:
    day_floor(10, "esimdog")
    check("unknown supplier raises", False, True)
except ValueError:
    check("unknown supplier raises", True, True)


if _fails:
    print(f"\n{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("\nall day-policy tests passed")
sys.exit(0)
