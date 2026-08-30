#!/usr/bin/env python3
"""Tests for which route the scraper puts in the sheet.

This file exists because of 2026-08-29. esim.dog added a route called Amber,
priced it below everything else, and the scraper — which chose by
min(price, quality) — picked it for 43 of 68 rows before anyone looked. Amber
is capped at 1 Mbps with no hotspot. The purchase bot buys whatever the Route
column says, so those rows were 43 customers about to be sold a 1 Mbps line at
a full-speed price.

The bug was never Amber. It was cheapest-first: ANY new cheap colour would have
won exactly the same way. So these tests pin the two things that make that
impossible to repeat:

    1. a route is refused for what its own info box CLAIMS (speed cap, no
       hotspot), not for being a colour we happen to know about, and
    2. a cheaper tier is only ever taken when it clears the owner's gate
       against the tier DIRECTLY ABOVE it.

Run:  python test_route_policy.py
"""

import sys

from esim_price_scraper import (
    BLOCKED_ROUTES, ROUTE_TIERS, TIER_GATE,
    choose_route, route_disqualified, route_name_key, route_quality_rank,
)

_fails: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


print("-- a route is judged by what its box claims, not by its name --")
# Verbatim shapes from the live info box (GB + TH, 2026-08-29).
AMBER = "ESSENTIAL COVERAGE\n1 Mbps max\nNo hotspot\nNETWORKS\n"
BLUE = "BEST COVERAGE & RELIABILITY\nLTE + 5G\nHotspot\nNETWORKS • EE, Vodafone\n"
PINK = "GOOD RELIABILITY\nLTE + 5G\nHotspot\nNETWORKS • Local network\n"
check("Amber refused", bool(route_disqualified(AMBER)), True)
check("Amber's reason names the cap", route_disqualified(AMBER), "capped at 1 Mbps")
check("Blue allowed", route_disqualified(BLUE), "")
check("Pink allowed", route_disqualified(PINK), "")
# The point of the rule: the NEXT cheap trap is caught without us knowing it.
check("unknown colour, capped speed, still refused",
      route_disqualified("SOMETHING NEW\n2 Mbps max\nNETWORKS\n"), "capped at 2 Mbps")
check("unknown colour, no hotspot, still refused",
      route_disqualified("TEAL DEAL\nLTE\nNo hotspot\n"), "no hotspot")
check("unknown colour that claims nothing bad is allowed",
      route_disqualified("TEAL\nLTE + 5G\nHotspot\nNETWORKS • Local\n"), "")
check("Amber is blocked by name as well", route_name_key("🎁 Amber") in BLOCKED_ROUTES, True)

print("\n-- the near-miss: fair-use copy sits INSIDE the info box --")
# Verbatim from esim.dog/gb, 2026-08-30. The box window that the speed-cap
# check reads also catches the fair-use table, and that table is full of the
# word "Mbps". "then 1 Mbps unlimited" describes what happens AFTER the
# high-speed allowance on a perfectly good route; "1 Mbps max" is a route that
# never goes faster. One character of sloppiness in that regex pulls healthy
# packages off sale, which is why this whole live block is kept verbatim.
LIVE_GB_BOX = (
    " buy.\n\nFair-use policy:\n500MB/day high speed\nthen 512 Kbps unlimited\n"
    "-$0.62\n1GB/day high speed\nthen 512 Kbps unlimited\n-$0.49\n"
    "3GB/day high speed\nthen 1 Mbps unlimited\n5GB/day high speed\n"
    "then 512 Kbps unlimited\n+$0.87\n\U0001F389 FRESH DEAL\n$6.12\n"
    "BEST COVERAGE & RELIABILITY\nNETWORKS \u2022 LTE + 5G\nThree, O2, EE\n\n"
)
check("a real Blue page is NOT refused by its fair-use table",
      route_disqualified(LIVE_GB_BOX), "")
check("'Mbps max' still refused next to the same fair-use table",
      route_disqualified(LIVE_GB_BOX + "ESSENTIAL COVERAGE\n1 Mbps max\n"),
      "capped at 1 Mbps")

print("\n-- the site's own tier word outranks the colour --")
check("tier word wins", route_quality_rank("Teal", "BEST") < route_quality_rank("Blue"), True)
check("Blue beats Black", route_quality_rank("Blue") < route_quality_rank("Black"), True)
check("Black beats Pink", route_quality_rank("Black") < route_quality_rank("Pink"), True)
check("unknown ranks last",
      route_quality_rank("Teal") > route_quality_rank("Green"), True)

print("\n-- quality is the default; a downgrade must earn it --")
check("only one tier present", choose_route({"Blue": 9.0, "Black": 8.0}), "Black")
check("ties inside a tier go to Blue", choose_route({"Blue": 8.0, "Black": 8.0}), "Blue")
# Pink gate: take Pink only if Blue/Black costs MORE than 10% above it.
check("Pink 5% cheaper is not enough", choose_route({"Blue": 10.0, "Pink": 9.50}), "Blue")
check("Pink exactly 10% cheaper is not enough (strict)",
      choose_route({"Blue": 11.0, "Pink": 10.0}), "Blue")
check("Pink 20% cheaper earns it", choose_route({"Blue": 10.0, "Pink": 8.0}), "Pink")
# Yellow gate: take Yellow/Green only if it is AT LEAST 20% below Pink.
check("Yellow 15% below Pink is not enough",
      choose_route({"Blue": 10.0, "Pink": 8.0, "Yellow": 6.80}), "Pink")
check("Yellow exactly 20% below Pink earns it (inclusive)",
      choose_route({"Blue": 10.0, "Pink": 8.0, "Yellow": 6.40}), "Yellow")

print("\n-- the gate is measured against the tier above, not against the winner --")
# The bug this pins: Pink is present and lost its own gate, so Blue is winning.
# Measuring Yellow against the WINNER (Blue) inverts the policy — the dearer
# the premium route, the easier it becomes to sell the LTE-only one.
#   vs Pink  (correct): 7.80 <= 8.00*0.80 = 6.40?  no  -> keep Blue
#   vs Blue  (bug):     7.80 <= 10.00*0.80 = 8.00?  yes -> hand over Yellow
check("losing middle tier is still the yardstick",
      choose_route({"Blue": 10.0, "Pink": 9.50, "Yellow": 7.80}), "Blue")
check("...and the same prices without Pink DO reach Yellow",
      choose_route({"Blue": 10.0, "Yellow": 7.80}), "Yellow")
check("a missing tier is skipped, gate measured against the best present",
      choose_route({"Blue": 10.0, "Green": 9.0}), "Blue")

print("\n-- nothing sellable on the page --")
check("no routes at all", choose_route({}), None)
check("only unknown colours: policy declines to pick", choose_route({"Teal": 3.0}), None)

print("\n-- the policy table itself --")
check("tiers", ROUTE_TIERS, (("Blue", "Black"), ("Pink",), ("Yellow", "Green")))
check("gates", TIER_GATE, (1.10, 0.80))


if _fails:
    print(f"\n{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("\nall route-policy tests passed")
sys.exit(0)
