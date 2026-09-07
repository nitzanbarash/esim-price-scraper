#!/usr/bin/env python3
"""Tests for the same-GB, different-days fallback.

This file exists because of what esim.dog did to 10GB. It prices every
(GB × days) pair separately and quietly withdraws individual pairs: on
2026-09-01 Italy sold 10GB at 31d, 25d and 21d but NOT at 30d, and the 30d
link came back as a 9GB package at a 9GB price. Greece did the same thing.
The owner had been finding the day that still carried the size by hand — the
sheet's 25d and 31d and 21d rows are all his — and this automates that.

The rules under test are his, and every one of them is a way of NOT selling
the customer something else:

    1. the GB never moves. A size the site withdrew is out of stock, and no
       number of days brings it back, so we do not go looking.
    2. 30 days is the product on 10GB and up. Alternatives keep a row on sale
       when 30d cannot; a row that fell back goes BACK to 30d when 30d works.
    3. preference 30, 31, then down to a floor per size — 21d for 10-19GB,
       25d for 20GB and up. Never past 31d.
    4. whatever we adopt is written into the LINK too, because the purchase
       bot parses the days out of the link and refuses to buy when the link
       and the sheet disagree.

Run:  python test_price_fallback.py
"""

import asyncio
import sys
from urllib.parse import parse_qs, urlparse

from esim_price_scraper import (
    DAY_BANDS, DAY_CEILING, DAY_LADDER, ESIMScraper,
    fallback_day_floor, fallback_days, force_fixed_gb_tab, is_profitable,
    profit_floor_pct, with_validity,
)

_fails: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


print("-- which days we are willing to try --")
# The owner's bands (2026-09-07). A bigger package needs more days to be a
# credible product, so the floor climbs with size; 31 is the ceiling for all.
LADDER = [1, 3, 5, 7, 10, 14, 15, 20, 21, 25, 30, 31]
check("1-2GB is searched from day 1 — the whole ladder",
      fallback_days(1.0, 30), LADDER)
check("2GB is still in the first band", fallback_days(2.0, 30), LADDER)
check("just over 2GB starts at 7 days",
      fallback_days(2.1, 30), [7, 10, 14, 15, 20, 21, 25, 30, 31])
check("5GB inclusive is the same band", fallback_days(5.0, 30),
      [7, 10, 14, 15, 20, 21, 25, 30, 31])
check("10GB inclusive starts at 14 days",
      fallback_days(10.0, 30), [14, 15, 20, 21, 25, 30, 31])
check("20GB inclusive starts at 21 days",
      fallback_days(20.0, 30), [21, 25, 30, 31])
check("21GB and up starts at 25 days",
      fallback_days(21.0, 30), [25, 30, 31])
check("50GB is the same top band", fallback_days(50.0, 30), [25, 30, 31])
check("nothing above 31d is ever a candidate",
      [d for d in fallback_days(1.0, 30) if d > DAY_CEILING], [])
check("no GB at all is left alone", fallback_days(None, 30), [])
check("floors", (fallback_day_floor(2.0), fallback_day_floor(5.0),
                 fallback_day_floor(10.0), fallback_day_floor(20.0),
                 fallback_day_floor(100.0)),
      (1, 7, 14, 21, 25))

print("\n-- the day the owner picked by hand is never taken away from him --")
# The 30GB Greece row sits at 21d, under its own 25d floor. The floor governs
# what we SEARCH; it must not drop the incumbent out of the comparison, or a
# "cheapest" verdict is reached without ever pricing the day we actually sell.
check("30GB already on 21d keeps 21d in the list",
      fallback_days(30.0, 21), [21, 25, 30, 31])
check("...and 21d is not offered to a 30GB row that is on 30d",
      fallback_days(30.0, 30), [25, 30, 31])
check("a 60d row is not dragged into the band",
      fallback_days(10.0, 60), [14, 15, 20, 21, 25, 30, 31])

print("\n-- does it still pay? --")
check("10GB at 20% clears the bar", is_profitable(6.00, 5.00, 10.0), True)
check("10GB just under 20% does not", is_profitable(5.99, 5.00, 10.0), False)
check("1GB may run at a loss up to 20%", is_profitable(0.80, 1.00, 1.0), True)
check("1GB at a 21% loss may not", is_profitable(0.79, 1.00, 1.0), False)
check("floors", (profit_floor_pct(1.0), profit_floor_pct(10.0)), (-20.0, 20.0))
# A row the owner has not priced yet is not a row that fails the bar. Judging
# it would take brand-new packages off sale for having an empty cell.
check("no sell price: not judged", is_profitable(None, 5.00, 10.0), True)
check("no buy price: not judged", is_profitable(6.00, None, 10.0), True)

print("\n-- the link is rewritten, not rebuilt --")
PIN = "https://esim.dog/de?tab=fixedgb&data=10&validity=30#route=black"
out = with_validity(PIN, 25)
check("days changed", parse_qs(urlparse(out).query)["validity"], ["25"])
check("GB untouched", parse_qs(urlparse(out).query)["data"], ["10"])
check("the fixed-GB tab survives", parse_qs(urlparse(out).query)["tab"], ["fixedgb"])
# The fragment is the owner pinning a route by hand. Rebuilding the URL from
# its parts would drop it silently and change which network is sold.
check("the route pin survives", urlparse(out).fragment, "route=black")
check("a region link keeps its region",
      parse_qs(urlparse(with_validity(
          "https://esim.dog/regions?region=asia&data=10&validity=30", 25)).query)["region"],
      ["asia"])

print("\n-- a country link without tab=fixedgb prices a different product --")
# Without the tab esim.dog swings to the Unlimited plan and ignores data=
# entirely: Greece 30d read $57.05 that way against $3.41 for the package we
# actually sell. Every link in the sheet carries it today; this is the guard
# for the day one does not.
check("tab is forced on",
      parse_qs(urlparse(force_fixed_gb_tab(
          "https://esim.dog/gr?data=10&validity=30")).query)["tab"], ["fixedgb"])
check("an already-correct link is left byte-identical",
      force_fixed_gb_tab(PIN), PIN)
check("/regions has no such tab and is not given one",
      force_fixed_gb_tab("https://esim.dog/regions?region=asia&data=10&validity=30"),
      "https://esim.dog/regions?region=asia&data=10&validity=30")
check("a partial link selects nothing anyway",
      force_fixed_gb_tab("https://esim.dog/gr"), "https://esim.dog/gr")


# ── the search itself ───────────────────────────────────────────────────────
# A fake esim.dog: a catalogue of the (GB, days) pairs it actually sells. Ask
# for a pair it does not have and it answers the way the real site does —
# with a DIFFERENT package, which is what "out of stock" means here.

class FakeDog:
    def __init__(self, catalogue, substitute_gb=None):
        self.catalogue = catalogue          # {(gb, days): price}
        self.substitute_gb = substitute_gb  # what it hands back when GB is gone
        self.asked = []

    async def scrape(self, url, variant=""):
        q = parse_qs(urlparse(url).query)
        gb, days = float(q["data"][0]), int(q["validity"][0])
        self.asked.append(days)
        price = self.catalogue.get((gb, days))
        if price is not None:
            return {"price": f"${price:.2f}", "gb": f"{gb:g}gb",
                    "validity": f"{days}d", "out_of_stock": False, "note": ""}
        if self.substitute_gb is not None:
            sub = self.catalogue.get((self.substitute_gb, days))
            if sub is not None:
                return {"price": f"${sub:.2f}", "gb": f"{self.substitute_gb:g}gb",
                        "validity": f"{days}d", "out_of_stock": True, "note": ""}
        return {"price": f"${(price or 9.99):.2f}", "gb": f"{gb:g}gb",
                "validity": "1d", "out_of_stock": True, "note": ""}


def search(catalogue, gb, days, my_price, substitute_gb=None, deadline=1e18):
    """Run find_alternative against the fake and report (winner, days asked)."""
    dog = FakeDog(catalogue, substitute_gb)
    bot = ESIMScraper.__new__(ESIMScraper)      # no Google, no browser
    bot.scrape = dog.scrape
    link = f"https://esim.dog/it?tab=fixedgb&data={gb:g}&validity={days}"
    it = {"link": link, "variant": "", "my_price": my_price}
    primary = asyncio.run(dog.scrape(link))
    dog.asked.clear()
    alt = asyncio.run(bot.find_alternative(it, primary, deadline))
    return alt, dog.asked


ITALY = {(10.0, 31): 4.81, (10.0, 25): 5.49, (10.0, 21): 3.17, (9.0, 30): 3.17}

print("\n-- a healthy row still prices its whole band --")
# This is the cost of the 2026-09-07 rule and it is deliberate. The old policy
# opened nothing when 30d was in stock and paying; "cheapest" is a claim you
# cannot make without pricing the alternatives, so now the band is read every
# run. The reads go out concurrently, which is what keeps it affordable.
alt, asked = search({(10.0, 30): 4.00}, 10.0, 30, "6.00")
check("nothing beat the day we hold", alt, None)
check("but the band was priced", sorted(asked), [14, 15, 20, 21, 25, 31])

# Italy, read off the live site 2026-09-01: 10GB exists at 31/25/21 but the
# 30d pair was withdrawn and answers with 9GB.
print("\n-- the 30d pair is gone: take the cheapest day that still has the size --")
alt, asked = search(ITALY, 10.0, 30, "6.50", substitute_gb=9.0)
check("21d adopted — $3.17 against 31d's $4.81", (alt or {}).get("days"), 21)
check("it says where it came from", (alt or {}).get("from_days"), 30)
check("the link carries the new day",
      parse_qs(urlparse((alt or {}).get("link", "?")).query).get("validity"), ["21"])
check("the link still asks for 10GB",
      parse_qs(urlparse((alt or {}).get("link", "?")).query).get("data"), ["10"])
# The old rule stopped at 31d, the first day that worked, and paid $1.64 more
# per eSIM for the privilege. This is the whole reason the rule changed.
check("31d was priced and beaten, not skipped", 31 in asked, True)

print("\n-- a substituted GB is a reason to search, not to stop --")
# The tempting shortcut: the page answered 9GB, so 10GB must be gone, so skip
# the search. Italy is the counter-example that killed it — the same site that
# substitutes 9GB for the 30d link sells 10GB at 21d, 25d and 31d. What gets
# withdrawn is a (size x days) PAIR.
alt, asked = search({(9.0, 30): 3.17, (9.0, 31): 3.20, (9.0, 25): 3.30,
                     (9.0, 21): 3.40}, 10.0, 30, "6.50", substitute_gb=9.0)
check("nothing adopted — every day answered with the wrong size", alt, None)
check("bounded by the band", sorted(asked), [14, 15, 20, 21, 25, 31])

print("\n-- 30d is in stock but has stopped paying --")
# $6.00 against a $6.50 sell price is 8% — under the 20% bar. 21d at $3.17 is
# the cheapest day that clears it; 25d at $5.49 is only 18% and never counts.
alt, asked = search({(10.0, 30): 6.00, **ITALY}, 10.0, 30, "6.50")
check("21d taken", (alt or {}).get("days"), 21)
check("25d was priced but is unprofitable, so it never wins", 25 in asked, True)

print("\n-- cheaper wins even when the day we hold is perfectly healthy --")
# The reversal of the old rule, stated plainly. 30d at $4.00 is in stock and
# pays; 25d at $3.50 is in stock, pays, and costs $0.50 less. We take the $0.50.
alt, asked = search({(10.0, 30): 4.00, (10.0, 25): 3.50}, 10.0, 30, "6.50")
check("25d taken", (alt or {}).get("days"), 25)
check("at its price", (alt or {}).get("res", {}).get("price"), "$3.50")

print("\n-- ...and a dearer day is never taken just because it is longer --")
alt, asked = search({(10.0, 30): 4.00, (10.0, 25): 3.50}, 10.0, 25, "6.50")
check("no swap — 30d costs more", alt, None)

print("\n-- what we already sell wins when nothing undercuts it --")
alt, asked = search({(10.0, 25): 3.50}, 10.0, 25, "6.50")
check("no swap", alt, None)

print("\n-- a 21GB row is never offered 21 days --")
alt, asked = search({(21.0, 21): 3.00}, 21.0, 30, "20.00")
check("nothing adopted", alt, None)
check("21d never opened — its floor is 25", sorted(asked), [25, 31])

print("\n-- no price, but we know why: still worth searching --")
# Italy's 10GB/30d link lands on a 9GB page whose only route is capped at
# 1 Mbps, so the read comes back with NO price. That is a verdict, not a
# failed read. A blanket "no price, do not search" rule would switch the
# feature off on the rows it exists for.
dog = FakeDog(ITALY, substitute_gb=9.0)
bot = ESIMScraper.__new__(ESIMScraper)
bot.scrape = dog.scrape
link = "https://esim.dog/it?tab=fixedgb&data=10&validity=30"
alt = asyncio.run(bot.find_alternative(
    {"link": link, "variant": "", "my_price": "6.50"},
    {"price": None, "gb": "9gb", "validity": "30d", "out_of_stock": True}, 1e18))
check("searched anyway, and found the cheapest", (alt or {}).get("days"), 21)

# The other half of the same rule: a page that simply did not load says
# nothing at all, and moving a package on that is worse than waiting a day.
dog2 = FakeDog(ITALY, substitute_gb=9.0)
bot2 = ESIMScraper.__new__(ESIMScraper)
bot2.scrape = dog2.scrape
alt2 = asyncio.run(bot2.find_alternative(
    {"link": link, "variant": "", "my_price": "6.50"},
    {"price": None, "gb": "", "validity": "", "out_of_stock": False}, 1e18))
check("a failed read is not searched on", alt2, None)
check("and opens nothing", dog2.asked, [])

print("\n-- a price that does not repeat drops to the next-cheapest --")
class Flickers(FakeDog):
    async def scrape(self, url, variant=""):
        r = await FakeDog.scrape(self, url, variant)
        # The confirming read of 21d disagrees with the read that won.
        if self.asked.count(21) == 2 and r["price"] == "$3.17":
            r = dict(r, price="$9.99")
        return r

dog = Flickers(ITALY, substitute_gb=9.0)
bot = ESIMScraper.__new__(ESIMScraper)
bot.scrape = dog.scrape
primary = asyncio.run(dog.scrape(link))
dog.asked.clear()
alt = asyncio.run(bot.find_alternative(
    {"link": link, "variant": "", "my_price": "6.50"}, primary, 1e18))
# 21d read $3.17 once and $9.99 on the confirming read, so it is not taken.
# 25d at $5.49 is 18% against a $6.50 sell price — under the bar, never a
# candidate. 31d at $4.81 is the next-cheapest that is both steady and pays.
check("21d flickered and was not taken", (alt or {}).get("days") != 21, True)
check("31d taken instead — the search did not just give up",
      (alt or {}).get("days"), 31)

print("\n-- the clock still owns the run --")
alt, asked = search(ITALY, 10.0, 30, "6.50", substitute_gb=9.0, deadline=0)
check("past the budget: nothing adopted", alt, None)
check("and nothing opened", asked, [])

print("\n-- the policy table itself --")
check("the ladder", DAY_LADDER, (1, 3, 5, 7, 10, 14, 15, 20, 21, 25, 30, 31))
check("the bands", DAY_BANDS, ((2.0, 1), (5.0, 7), (10.0, 14), (20.0, 21)))
check("the ceiling", DAY_CEILING, 31)


if _fails:
    print(f"\n{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("\nall fallback tests passed")
sys.exit(0)
