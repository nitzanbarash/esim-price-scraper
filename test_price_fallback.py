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
    2. 30 days is the product. Alternatives keep a row on sale when 30d cannot,
       and a row that fell back goes BACK to 30d the moment 30d works again —
       but 30d is only given up for a price MORE than 1% cheaper.
    3. after that, closest to 30 wins, and a tie goes to the longer plan. The
       floor per size and the 31-day ceiling are day_policy's (its own tests
       pin the table); what this file pins is that the SEARCH obeys them.
    4. whatever we adopt is written into the LINK too, because the purchase
       bot parses the days out of the link and refuses to buy when the link
       and the sheet disagree.

The verdict itself moved into day_policy.pick() on 2026-09-10. Until then this
file's rule was "cheapest day in the band wins", and it was quietly selling
shorter trips: a 20-day plan two agorot under the 30-day one won, every time.

Run:  python test_price_fallback.py
"""

import asyncio
import sys
from urllib.parse import parse_qs, urlparse

import esim_price_scraper as scraper
from day_policy import DAY_CEILING, DAY_TOL, ESIMDOG
from esim_price_scraper import (
    DAY_LADDER, DAY_SUPPLIER, ESIMScraper, OUT_OF_WINDOW_NOTE,
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
# The owner's bands, restated 2026-09-10 and now living in day_policy.py. A
# bigger package needs more days to be a credible product, so the floor climbs
# with size; 31 is esim.dog's ceiling for every size. What this block pins is
# that the SCRAPER's ladder is filtered by those bands and by nothing else.
LADDER = [1, 3, 5, 7, 10, 14, 15, 20, 21, 25, 30, 31]
check("this scraper only ever speaks to one supplier", DAY_SUPPLIER, ESIMDOG)
check("1-2GB is searched from day 1 — the whole ladder",
      fallback_days(1.0, 30), LADDER)
check("2GB is still in the first band", fallback_days(2.0, 30), LADDER)
check("just over 2GB starts at 5 days",
      fallback_days(2.1, 30), [5, 7, 10, 14, 15, 20, 21, 25, 30, 31])
check("5GB inclusive is the same band", fallback_days(5.0, 30),
      [5, 7, 10, 14, 15, 20, 21, 25, 30, 31])
check("6GB starts at 10 days",
      fallback_days(6.0, 30), [10, 14, 15, 20, 21, 25, 30, 31])
check("9GB inclusive is the same band",
      fallback_days(9.0, 30), [10, 14, 15, 20, 21, 25, 30, 31])
check("10GB inclusive starts at 20 days",
      fallback_days(10.0, 30), [20, 21, 25, 30, 31])
check("19GB inclusive is the same band",
      fallback_days(19.0, 30), [20, 21, 25, 30, 31])
check("20GB inclusive starts at 25 days",
      fallback_days(20.0, 30), [25, 30, 31])
check("30GB inclusive is still the 25-day band",
      fallback_days(30.0, 30), [25, 30, 31])
# Over 30GB the floor is 30 days, and at 50GB the owner's "above 32 days" rule
# cannot apply here at all: esim.dog is capped at 31, so a 33-day floor would
# leave the row with an empty window and nothing to sell. That reading is
# day_policy's; this pins that the scraper still has two days to ask for.
check("31GB starts at 30 days", fallback_days(31.0, 30), [30, 31])
check("50GB keeps a window on esim.dog", fallback_days(50.0, 30), [30, 31])
check("nothing above 31d is ever a candidate",
      [d for d in fallback_days(1.0, 30) if d > DAY_CEILING], [])
check("no GB at all is left alone", fallback_days(None, 30), [])
check("floors", (fallback_day_floor(2.0), fallback_day_floor(5.0),
                 fallback_day_floor(10.0), fallback_day_floor(20.0),
                 fallback_day_floor(100.0)),
      (1, 5, 20, 25, 30))

print("\n-- a day outside the window is not a candidate, even the one we hold --")
# This is the 2026-09-07 rule REVERSED, on purpose. Back then `current` was
# always priced, so the 30GB Greece row could keep the 21 days it had been set
# to by hand. But the bands are the product — a 30GB buyer is promised 25 days
# — so a sub-floor incumbent is not a cheap option to weigh against, it is a
# row being sold wrong. Leaving it out of the list is what lets the row climb.
check("30GB on 21d is not offered 21d back",
      fallback_days(30.0, 21), [25, 30, 31])
check("10GB on 14d is not offered 14d back",
      fallback_days(10.0, 14), [20, 21, 25, 30, 31])
check("a 60d row is not dragged into the band",
      fallback_days(10.0, 60), [20, 21, 25, 30, 31])
check("a current day already in the window changes nothing",
      fallback_days(10.0, 25), [20, 21, 25, 30, 31])

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
# opened nothing when 30d was in stock and paying; a preference you cannot
# price is not a preference, so the band is read every run. The reads go out
# concurrently, which is what keeps it affordable.
alt, asked = search({(10.0, 30): 4.00}, 10.0, 30, "6.00")
check("nothing beat the day we hold", alt, None)
check("but the band was priced", sorted(asked), [20, 21, 25, 31])

# Italy, read off the live site 2026-09-01: 10GB exists at 31/25/21 but the
# 30d pair was withdrawn and answers with 9GB.
print("\n-- the 30d pair is gone: the policy picks from what is left --")
# 31d is nearest to 30 and holds the row first, but 21d at $3.17 is 34% under
# it — far past the 1% the owner will move for — so the saving is real and the
# nine days are given up for it. This is pick(), not "cheapest": the same
# catalogue with 21d at $4.78 would keep the row on 31d (pinned further down).
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

# The same shape with the saving taken out. 21d at $4.78 is 0.6% under 31d's
# $4.81 — under the bar — so the row stays on the day nearest 30 and the nine
# days are kept. Cheapest-first would have moved it for three cents.
alt, asked = search({(10.0, 31): 4.81, (10.0, 25): 5.49, (10.0, 21): 4.78,
                     (9.0, 30): 3.17}, 10.0, 30, "6.50", substitute_gb=9.0)
check("a 0.6% cheaper 21d leaves the row on 31d", (alt or {}).get("days"), 31)

print("\n-- a substituted GB is a reason to search, not to stop --")
# The tempting shortcut: the page answered 9GB, so 10GB must be gone, so skip
# the search. Italy is the counter-example that killed it — the same site that
# substitutes 9GB for the 30d link sells 10GB at 21d, 25d and 31d. What gets
# withdrawn is a (size x days) PAIR.
alt, asked = search({(9.0, 30): 3.17, (9.0, 31): 3.20, (9.0, 25): 3.30,
                     (9.0, 21): 3.40}, 10.0, 30, "6.50", substitute_gb=9.0)
check("nothing adopted — every day answered with the wrong size", alt, None)
check("bounded by the band", sorted(asked), [20, 21, 25, 31])

print("\n-- 30d is in stock but has stopped paying --")
# $6.00 against a $6.50 sell price is 8% — under the 20% bar. 21d at $3.17 is
# the cheapest day that clears it; 25d at $5.49 is only 18% and never counts.
alt, asked = search({(10.0, 30): 6.00, **ITALY}, 10.0, 30, "6.50")
check("21d taken", (alt or {}).get("days"), 21)
check("25d was priced but is unprofitable, so it never wins", 25 in asked, True)

print("\n-- MUCH cheaper does move a healthy 30d row --")
# 30d at $4.00 is in stock and pays; 25d at $3.50 is in stock, pays, and costs
# 12.5% less. That clears the owner's 1% bar many times over, so we take it.
alt, asked = search({(10.0, 30): 4.00, (10.0, 25): 3.50}, 10.0, 30, "6.50")
check("25d taken", (alt or {}).get("days"), 25)
check("at its price", (alt or {}).get("res", {}).get("price"), "$3.50")

print("\n-- ...but small change does not, and this is the 2026-09-10 rule --")
# 21d at $3.98 is 0.5% under 30d's $4.00. The old cheapest-first search took
# that trade every time: two agora saved, NINE DAYS of the customer's trip
# spent. "אם השינוי קטן או שווה ל-1% להישאר על 30 יום."
alt, asked = search({(10.0, 30): 4.00, (10.0, 21): 3.98}, 10.0, 30, "6.50")
check("0.5% cheaper does not displace 30d", alt, None)
check("...and it was priced before being refused", 21 in asked, True)
check("exactly 1% cheaper stays on 30d too",
      search({(10.0, 30): 4.00, (10.0, 21): 3.96}, 10.0, 30, "6.50")[0], None)
check("a hair past 1% does move it",
      (search({(10.0, 30): 4.00, (10.0, 21): 3.95}, 10.0, 30, "6.50")[0]
       or {}).get("days"), 21)
check("the tolerance itself", DAY_TOL, 0.01)

print("\n-- at one price, the day closest to 30 wins — ties go to the longer --")
# 25d and 21d both at $3.50, and no 30d on the page at all. Neither beats the
# other on price, so nothing displaces the first holder — and the first holder
# is the one nearest 30. "21 או 25 באותו מחיר -> 25."
alt, asked = search({(10.0, 21): 3.50, (10.0, 25): 3.50}, 10.0, 30, "6.50",
                    substitute_gb=9.0)
check("25d, not 21d", (alt or {}).get("days"), 25)
check("both were priced, and only the winner was confirmed",
      sorted(d for d in asked if d in (21, 25)), [21, 25, 25])

print("\n-- ...and a dearer day is never taken just because it is longer --")
# The row is on 25d and 30d costs MORE. 30d is first preference by position,
# but preference is not a licence to pay more, so the incumbent holds.
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
check("searched anyway, and the policy found a day to sell",
      (alt or {}).get("days"), 21)

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

print("\n-- a price that does not repeat drops to the policy's runner-up --")
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
# 21d read $3.17 once and $9.99 on the confirming read, so it is dropped and
# pick() is asked again over what is left. 25d at $5.49 is 18% against a $6.50
# sell price — under the bar, never a candidate at all. That leaves 31d at
# $4.81, which is both steady and the day nearest 30.
check("21d flickered and was not taken", (alt or {}).get("days") != 21, True)
check("31d taken instead — the search did not just give up",
      (alt or {}).get("days"), 31)

print("\n-- the clock still owns the run --")
alt, asked = search(ITALY, 10.0, 30, "6.50", substitute_gb=9.0, deadline=0)
check("past the budget: nothing adopted", alt, None)
check("and nothing opened", asked, [])

print("\n-- a row stuck outside its window says so out loud --")
# The silent case. A 30GB row on 14 days is below its 25-day floor, so 14d is
# not offered back to it (pinned above) — and when 25/30/31 all come back
# unsellable there is nothing to adopt either. The row then keeps the 14 days
# it is being sold wrong at, and every other column looks healthy. The note is
# left on the read the caller already holds; the scraper writes it into J.
dog = FakeDog({}, substitute_gb=None)
bot = ESIMScraper.__new__(ESIMScraper)
bot.scrape = dog.scrape
stuck_link = "https://esim.dog/it?tab=fixedgb&data=30&validity=14"
stuck = asyncio.run(dog.scrape(stuck_link))
dog.asked.clear()
alt = asyncio.run(bot.find_alternative(
    {"link": stuck_link, "variant": "", "my_price": "60.00"}, stuck, 1e18))
check("nothing to adopt", alt, None)
check("the window was searched first", sorted(dog.asked), [25, 30, 31])
check("and the row is flagged with the day it is stuck on",
      stuck.get("day_window_note"),
      "⚠️ מחוץ לטווח הימים (14d, אין חלופה)")
check("the scraper and the test agree on the wording",
      OUT_OF_WINDOW_NOTE.format(current=14), stuck["day_window_note"])

# The note is for THAT case only. A row whose day is inside the window and
# simply found nothing better is not a problem, and a J full of warnings that
# mean nothing is a J nobody reads.
dog = FakeDog({(10.0, 30): 4.00})
bot = ESIMScraper.__new__(ESIMScraper)
bot.scrape = dog.scrape
healthy_link = "https://esim.dog/it?tab=fixedgb&data=10&validity=30"
healthy = asyncio.run(dog.scrape(healthy_link))
asyncio.run(bot.find_alternative(
    {"link": healthy_link, "variant": "", "my_price": "6.00"}, healthy, 1e18))
check("a healthy row is not flagged", healthy.get("day_window_note"), None)


print("\n-- the browser cap counts drivers, not just browsers --")
# 2026-09-10: `async with async_playwright()` sat OUTSIDE `async with
# browser_slots()`. Entering that context manager spawns playwright's node
# driver subprocess, so every read in flight had a driver of its own while it
# queued for a slot — SCRAPE_CONCURRENCY x DAY_SCAN_CONCURRENCY of them — and
# BROWSER_SLOTS only ever capped the browsers behind them. On a 2-core runner
# that is the half that was never measured. This pins the slot around both.

class Census:
    """How many of each thing are alive at once, and the worst it ever got."""
    def __init__(self):
        self.live = {'driver': 0, 'browser': 0}
        self.peak = {'driver': 0, 'browser': 0}

    def opened(self, kind):
        self.live[kind] += 1
        self.peak[kind] = max(self.peak[kind], self.live[kind])

    def closed(self, kind):
        self.live[kind] -= 1


class StubDriver:
    """Stands in for async_playwright(), for Chromium, and for the browser.

    One object plays all three parts because scrape() only ever walks the one
    chain: enter the context, launch, new_context, new_page, goto, close.
    """
    def __init__(self, census):
        self.census = census
        self.chromium = self

    async def __aenter__(self):
        self.census.opened('driver')     # the node subprocess starts HERE
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc):
        self.census.closed('driver')
        return False

    async def launch(self, **kw):
        self.census.opened('browser')
        await asyncio.sleep(0.01)        # long enough for the others to pile up
        return self

    async def new_context(self):
        return self

    async def new_page(self):
        return self

    async def goto(self, *a, **kw):
        await asyncio.sleep(0.01)
        raise RuntimeError("stubbed page")   # scrape() catches it and closes

    async def close(self):
        self.census.closed('browser')


SLOTS = 4
CALLS = 12

async def storm(bot):
    return await asyncio.gather(*(
        bot.scrape("https://esim.dog/it?tab=fixedgb&data=10&validity=30")
        for _ in range(CALLS)))

census = Census()
kept_slots, kept_playwright = scraper.BROWSER_SLOTS, scraper.async_playwright
try:
    scraper.BROWSER_SLOTS = SLOTS
    scraper._browser_slots = None        # rebuilt from the number above
    scraper.async_playwright = lambda: StubDriver(census)
    bot = ESIMScraper.__new__(ESIMScraper)   # no Google, no real browser
    reads = asyncio.run(storm(bot))
finally:
    scraper.BROWSER_SLOTS, scraper.async_playwright = kept_slots, kept_playwright
    scraper._browser_slots = None

check("all 12 reads ran", len(reads), CALLS)
check("never more than BROWSER_SLOTS drivers alive", census.peak['driver'], SLOTS)
check("never more than BROWSER_SLOTS browsers alive", census.peak['browser'], SLOTS)
# A cap that is never reached proves nothing about a cap, so pin that the
# reads really did run together and that every slot was handed back.
check("and the slots were all in use", census.peak['driver'] > 1, True)
check("nothing left running", census.live, {'driver': 0, 'browser': 0})

print("\n-- the policy table itself --")
# The ladder stays here: it is a fact about esim.dog's URLs (which day numbers
# have ever been seen to sell), not about what the owner wants sold. The bands
# and the ceiling left this file on 2026-09-10 — test_day_policy.py pins those,
# and pinning them twice is how the two copies drift apart.
check("the ladder", DAY_LADDER, (1, 3, 5, 7, 10, 14, 15, 20, 21, 25, 30, 31))
check("every rung the scraper asks for is inside the ceiling",
      [d for d in DAY_LADDER if d > DAY_CEILING], [])
check("the ceiling", DAY_CEILING, 31)


if _fails:
    print(f"\n{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("\nall fallback tests passed")
sys.exit(0)
