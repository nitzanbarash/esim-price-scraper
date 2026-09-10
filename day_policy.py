#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Which VALIDITY (number of days) a given size is sold at, per supplier.

esim.dog prices every (GB x days) pair separately, so the same 10GB can cost
$14 at 30 days and $9 at 25 — and Stellar, on the big sizes, sells 60- and
90-day plans at the same price as the 30-day one. Left to a cheapest-first
search, both of those facts sell the customer a SHORTER trip than the sheet
promised for a few agorot, or quietly hand a supplier's long plan to a size it
was never meant for. The owner wrote the rules down on 2026-09-10; this module
is those rules and nothing else. It is pure — no sheet, no network, no I/O.

THE OWNER'S RULES, verbatim (2026-09-10):

    "30 ימים תמיד עדיפות ראשונה. אם השינוי קטן או שווה ל-1% להישאר על 30 יום.
     עדיפות שנייה: כמה שיותר קרוב ל-30 ימים (21 או 25 באותו מחיר -> 25).
     ב-esim.dog לא נבדוק חבילות מעל 31 יום (לא כולל 31). בסטלר נבדוק [מעל 31]
     רק ב-30 גיגה ומעלה ובתנאי שהם באותו מחיר.
     1-2 גיגה: מספר הימים הכי זול; 3-5: החל מ-5 ימים; 6-9: החל מ-10;
     10-19: החל מ-20; 20-30: החל מ-25; מעל 30: 30 ומעלה;
     50 גיגה: החל ממעל ל-32 ימים."

THE AGREED READING (this is what the code below does):

    1. 30 days is always first preference, and it is only given up for a price
       that is MORE than 1% cheaper (DAY_TOL). Equal, dearer, or a rounding
       error's worth cheaper all leave the row on 30 days.
    2. Second preference is whatever sits CLOSEST to 30 — key (|days-30|, -days),
       so the walk order is 30, 31, 29, 28, ... and on a tie the LONGER plan
       wins (21 and 25 at one price -> 25).
    3. A floor per size — the shortest validity the owner will sell that size
       as. 1-2GB has no real floor (the cheapest day count wins); from 3GB up
       the floor climbs with the size.
    4. A ceiling per supplier. esim.dog is never asked for more than 31 days.
       Stellar is asked past 31 ONLY from 30GB up, and a longer Stellar plan is
       only taken when it costs no MORE than the incumbent ("באותו מחיר") —
       which is a weaker test than rule 1, on purpose: extra validity at the
       same money is free, so it does not have to be 1% cheaper to be taken.
    5. "50 גיגה: החל ממעל ל-32 ימים" is read as a floor of 33 days, and it can
       only ever apply to Stellar: esim.dog is capped at 31, so on esim.dog a
       50GB row keeps the 30-day floor of the band below it. A rule that made
       esim.dog's floor 33 under a ceiling of 31 would leave 50GB with an empty
       window and nothing to sell.

Read in the same voice as fallback_day_floor() in esim_price_scraper.py, which
implements the OLD single-supplier ladder; this module is the two-supplier
replacement and knows nothing about URLs, tabs or the sheet.
"""

from typing import Any, Iterable, List, Optional, Tuple

ESIMDOG = 'esim.dog'
STELLAR = 'stellar'
SUPPLIERS = (ESIMDOG, STELLAR)

# How much cheaper a rival validity must be before 30 days gives way.
# "אם השינוי קטן או שווה ל-1% להישאר על 30 יום" — 1% exactly stays put, so the
# test is strictly-less-than against price * (1 - DAY_TOL).
DAY_TOL = 0.01

# The preferred validity itself. Everything is measured as distance from here.
PREFERRED_DAYS = 30

# (size ceiling, floor) — the first band whose ceiling the size fits under wins.
# "1-2 גיגה: מספר הימים הכי זול" is a floor of 1: every validity is allowed and
# price decides.
DAY_FLOOR_BANDS: Tuple[Tuple[float, int], ...] = (
    (2.0, 1),      # 1-2GB   : cheapest day count, no floor
    (5.0, 5),      # 3-5GB   : from 5 days
    (9.0, 10),     # 6-9GB   : from 10 days
    (19.0, 20),    # 10-19GB : from 20 days
    (30.0, 25),    # 20-30GB : from 25 days
)
# "מעל 30: 30 ומעלה" — everything above 30GB starts at 30 days...
DAY_FLOOR_ABOVE_BANDS = 30
# ...except that from 50GB up, Stellar starts above 32.
BIG_GB = 50.0
BIG_GB_FLOOR = 33

# esim.dog is never asked past 31 days. Stellar is, but only on the big sizes.
DAY_CEILING = 31
STELLAR_UNCAPPED_FROM_GB = 30.0


def _supplier(supplier: str) -> str:
    """Normalise and reject anything that is not one of our two suppliers."""
    key = (supplier or '').strip().lower()
    if key not in SUPPLIERS:
        raise ValueError(f"unknown supplier {supplier!r}; expected one of {SUPPLIERS}")
    return key


def day_floor(gb: float, supplier: str) -> int:
    """Shortest validity the owner will sell this size as, at this supplier."""
    key = _supplier(supplier)
    gb = float(gb)
    if gb >= BIG_GB:
        # esim.dog cannot honour "above 32 days" under a 31-day ceiling, so its
        # 50GB rows keep the floor of the band below.
        return BIG_GB_FLOOR if key == STELLAR else DAY_FLOOR_ABOVE_BANDS
    for max_gb, floor in DAY_FLOOR_BANDS:
        if gb <= max_gb:
            return floor
    return DAY_FLOOR_ABOVE_BANDS


def day_ceiling(gb: float, supplier: str) -> Optional[int]:
    """Longest validity worth asking for. None = no ceiling (Stellar, 30GB+)."""
    key = _supplier(supplier)
    if key == STELLAR and float(gb) >= STELLAR_UNCAPPED_FROM_GB:
        return None
    return DAY_CEILING


def in_window(gb: float, supplier: str, days: int) -> bool:
    """Is this validity one we are allowed to sell this size as?"""
    days = int(days)
    if days < day_floor(gb, supplier):
        return False
    ceiling = day_ceiling(gb, supplier)
    return ceiling is None or days <= ceiling


def candidate_days(candidate: Any) -> int:
    """Days out of a candidate, whether it is an object, a mapping or a tuple."""
    if hasattr(candidate, 'days'):
        return int(candidate.days)
    try:
        return int(candidate['days'])
    except (TypeError, KeyError, IndexError):
        return int(candidate[0])


def candidate_price(candidate: Any) -> float:
    """Price out of a candidate, whether object, mapping or tuple."""
    if hasattr(candidate, 'price'):
        return float(candidate.price)
    try:
        return float(candidate['price'])
    except (TypeError, KeyError, IndexError):
        return float(candidate[1])


def preference_key(candidate: Any) -> Tuple[int, int]:
    """Closest to 30 wins; on a tie the LONGER plan wins (21 vs 25 -> 25)."""
    days = candidate_days(candidate)
    return (abs(days - PREFERRED_DAYS), -days)


def in_window_candidates(gb: float, supplier: str, candidates: Iterable[Any]) -> List[Any]:
    """The candidates we are allowed to consider, in preference order."""
    kept = [c for c in candidates if in_window(gb, supplier, candidate_days(c))]
    return sorted(kept, key=preference_key)


def pick(gb: float, supplier: str, candidates: Iterable[Any]) -> Optional[Any]:
    """The validity to sell `gb` at, from this supplier's priced candidates.

    `candidates` carry (days, price) — as attributes, mapping keys or the first
    two positions of a tuple — plus whatever payload the caller needs back; the
    chosen candidate is returned as given, so a link or a package code rides
    along untouched. Returns None when nothing is in the window at all: that is
    a row with no sellable validity, not a row to sell at any validity.
    """
    key = _supplier(supplier)
    ordered = in_window_candidates(gb, key, candidates)
    if not ordered:
        return None

    best = ordered[0]
    long_plans_free = key == STELLAR and float(gb) >= STELLAR_UNCAPPED_FROM_GB
    for cand in ordered[1:]:
        price, days = candidate_price(cand), candidate_days(cand)
        best_price, best_days = candidate_price(best), candidate_days(best)
        if long_plans_free and days > DAY_CEILING and days > best_days:
            # "בתנאי שהם באותו מחיר": more days for no more money is free, so
            # equal price is enough — it does not have to beat DAY_TOL.
            if price <= best_price:
                best = cand
            continue
        if price < best_price * (1 - DAY_TOL):
            best = cand
    return best
