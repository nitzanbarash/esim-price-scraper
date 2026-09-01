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
    FALLBACK_DAYS, FALLBACK_MIN_GB, ESIMScraper,
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
check("10GB: 30 first, then 31, then down to its 21d floor",
      fallback_days(10.0, 30), [30, 31, 25, 21])
check("20GB stops at 25 — three weeks is not a monthly package",
      fallback_days(20.0, 30), [30, 31, 25])
check("30GB is a 20GB-and-up size, same floor",
      fallback_days(30.0, 30), [30, 31, 25])
check("nothing above 31d is ever a candidate",
      [d for d in fallback_days(10.0, 30) if d > 31], [])
check("5GB is left alone — a short-trip package, not a monthly one",
      fallback_days(5.0, 30), [])
check("1GB is left alone", fallback_days(1.0, 1), [])
check("no GB at all is left alone", fallback_days(None, 30), [])
check("floors", (fallback_day_floor(10.0), fallback_day_floor(19.9),
                 fallback_day_floor(20.0), fallback_day_floor(100.0)),
      (21, 21, 25, 25))

print("\n-- the day the owner picked by hand is never taken away from him --")
# The 30GB Greece row sits at 21d, under its own 25d floor. The floor governs
# what we SEARCH; it must not pull a working package off sale to enforce
# itself, so the day we already sell stays in the list, ranked last.
check("30GB already on 21d keeps 21d as the last resort",
      fallback_days(30.0, 21), [30, 31, 25, 21])
check("...and 21d is still not offered to a 30GB row that is on 30d",
      fallback_days(30.0, 30), [30, 31, 25])
check("a 60d row is not dragged into the band",
      fallback_days(10.0, 60), [30, 31, 25, 21])

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
            sub = self.substitute_gb
            return {"price": f"${self.catalogue[(sub, days)]:.2f}", "gb": f"{sub:g}gb",
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


# Italy, read off the live site 2026-09-01: 10GB exists at 31/25/21 but the
# 30d pair was withdrawn and answers with 9GB.
ITALY = {(10.0, 31): 4.81, (10.0, 25): 5.49, (10.0, 21): 3.17, (9.0, 30): 3.17}

print("\n-- a healthy row costs nothing --")
alt, asked = search({(10.0, 30): 4.00}, 10.0, 30, "6.00")
check("30d in stock and paying: no alternative", alt, None)
check("...and not one extra page was opened", asked, [])

print("\n-- the 30d pair is gone: find the day that still has the size --")
alt, asked = search(ITALY, 10.0, 30, "6.50", substitute_gb=9.0)
check("31d adopted", (alt or {}).get("days"), 31)
check("it says where it came from", (alt or {}).get("from_days"), 30)
check("the link carries the new day",
      parse_qs(urlparse((alt or {}).get("link", "?")).query).get("validity"), ["31"])
check("the link still asks for 10GB",
      parse_qs(urlparse((alt or {}).get("link", "?")).query).get("data"), ["10"])
# 31 twice = the probe and its confirming read. Nothing below 31 is opened.
check("stopped at the first day that worked", asked, [31, 31])

print("\n-- a substituted GB is a reason to search, not to stop --")
# The tempting shortcut: the page answered 9GB, so 10GB must be gone, so skip
# the search. Italy is the counter-example that killed it — the same site that
# substitutes 9GB for the 30d link sells 10GB at 31d. What gets withdrawn is a
# (size x days) PAIR. When the size really is gone the search costs at most
# three reads and ends where it started, which is the price of not being wrong.
alt, asked = search({(9.0, 30): 3.17, (9.0, 31): 3.20, (9.0, 25): 3.30,
                     (9.0, 21): 3.40}, 10.0, 30, "6.50", substitute_gb=9.0)
check("nothing adopted — every day answered with the wrong size", alt, None)
check("bounded: it tried the band and gave up", asked, [31, 25, 21])

print("\n-- 30d is in stock but has stopped paying --")
# $6.00 against a $6.50 sell price is 8% — under the 20% bar. 31d at $4.81 is
# 35% and wins. This is the owner's own example, in his numbers.
alt, asked = search({(10.0, 30): 6.00, **ITALY}, 10.0, 30, "6.50")
check("31d taken instead", (alt or {}).get("days"), 31)
check("31d was the first thing tried", asked[0], 31)

print("\n-- 31d does not pay either: keep walking down --")
alt, asked = search({(10.0, 30): 6.00, (10.0, 31): 6.20, (10.0, 25): 4.00,
                     (10.0, 21): 3.00}, 10.0, 30, "6.50")
check("25d taken", (alt or {}).get("days"), 25)
check("in the owner's order, and stopping at the first that pays",
      asked, [31, 25, 25])

print("\n-- and back up again when 30d recovers --")
# The whole point of running this on healthy rows too. A row parked on 25d
# after an outage must not stay there once 30d is sellable again.
alt, asked = search({(10.0, 30): 4.00, (10.0, 25): 3.50}, 10.0, 25, "6.50")
check("30d reclaimed", (alt or {}).get("days"), 30)
check("even though 25d was cheaper for us — days are ranked, not prices",
      (alt or {}).get("res", {}).get("price"), "$4.00")

print("\n-- what we already sell wins when nothing above it does --")
alt, asked = search({(10.0, 25): 3.50}, 10.0, 25, "6.50")
check("no swap", alt, None)
check("tried 30 and 31, then stopped at the day we hold", asked, [30, 31])

print("\n-- a 20GB row is never offered 21 days --")
alt, asked = search({(20.0, 21): 3.00}, 20.0, 30, "20.00")
check("nothing adopted", alt, None)
check("21d never opened", asked, [31, 25])

print("\n-- no price, but we know why: still worth searching --")
# Italy's 10GB/30d link lands on a 9GB page whose only route is capped at
# 1 Mbps, so the read comes back with NO price. That is a verdict, not a
# failed read, and 10GB/31d is one probe away. A blanket "no price, do not
# search" rule would switch the feature off on the rows it exists for.
dog = FakeDog(ITALY, substitute_gb=9.0)
bot = ESIMScraper.__new__(ESIMScraper)
bot.scrape = dog.scrape
link = "https://esim.dog/it?tab=fixedgb&data=10&validity=30"
alt = asyncio.run(bot.find_alternative(
    {"link": link, "variant": "", "my_price": "6.50"},
    {"price": None, "gb": "9gb", "validity": "30d", "out_of_stock": True}, 1e18))
check("searched anyway, and found 31d", (alt or {}).get("days"), 31)

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

print("\n-- a price that does not repeat is not moved to --")
class Flickers(FakeDog):
    async def scrape(self, url, variant=""):
        r = await FakeDog.scrape(self, url, variant)
        if self.asked.count(31) == 2 and r["price"] == "$4.81":
            r = dict(r, price="$9.99")      # the confirming read disagrees
        return r

dog = Flickers(ITALY, substitute_gb=9.0)
bot = ESIMScraper.__new__(ESIMScraper)
bot.scrape = dog.scrape
link = "https://esim.dog/it?tab=fixedgb&data=10&validity=30"
primary = asyncio.run(dog.scrape(link))
dog.asked.clear()
alt = asyncio.run(bot.find_alternative(
    {"link": link, "variant": "", "my_price": "6.50"}, primary, 1e18))
# 31d read $4.81 once and $9.99 the second time, so it is not taken. 25d is
# real at $5.49 but that is 18% against a $6.50 sell price, under the bar. 21d
# is the first day that is both steady and pays.
check("31d flickered and was skipped", 31 not in [(alt or {}).get("days")], True)
check("21d taken — the first steady day that pays", (alt or {}).get("days"), 21)

print("\n-- the clock still owns the run --")
alt, asked = search(ITALY, 10.0, 30, "6.50", substitute_gb=9.0, deadline=0)
check("past the budget: nothing adopted", alt, None)
check("and nothing opened", asked, [])

print("\n-- the policy table itself --")
check("days", FALLBACK_DAYS, (30, 31, 25, 21))
check("smallest size that gets a fallback", FALLBACK_MIN_GB, 10.0)


if _fails:
    print(f"\n{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("\nall fallback tests passed")
sys.exit(0)
