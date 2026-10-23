#!/usr/bin/env python3
"""A regional plan esim.dog widens stays the row's plan — 2026-10-23.

esim.dog added the Philippines to the Asia Mini plan (12 → 13 countries).
The sheet still said "12" in 'וריאנט (אזורי)', and scrape_region treated any
other count on the pinned card as "esim.dog changed what the id points at":
back to the list, the cheapest card with exactly 12 countries. For 1GB and
10GB there was exactly one — a different product, China and Cambodia in,
Australia and New Zealand out — and because the coverage check only ran when
TWO cards matched, it was taken blind. 10GB went $6.81 → $19.99, 1GB $2.49 →
$3.99, both left the site, and the site's Mini country list lost AU and NZ.

The rules under test:
    1. a pinned card that holds every one of the sheet's countries, with a
       count at most REGION_GROWTH_MAX_ADDED above the sheet's, is ours; the
       variant follows it and J says so. A smaller count is never growth.
    2. the sheet's countries are checked even when ONE card has the count.
    3. off the pin, only a card with exactly the sheet's countries is taken —
       never a bigger one. esim.dog's regional plans nest (Asia Grande holds
       every Mini country), so a bigger card that holds ours is as often
       another product as ours under a new id (review, 2026-10-23).
    4. with no card carrying the sheet's countries the row is left alone
       (no price) — never moved to another product.
    5. a sheet with no country list keeps the old cheapest-by-count pick —
       for an UNPINNED row only; a pinned row whose count moved is left.
    6. whatever card the row ends on must carry every country the sheet
       sells, even when its count never moved.

Run:  python test_region_plan_growth.py
"""
import asyncio

from esim_price_scraper import (REGION_GROWTH_MAX_ADDED, ESIMScraper,
                                region_coverage_accepts, region_growth_note,
                                region_pin_verdict, region_search_order)

OLD12 = ["SG", "TH", "VN", "ID", "MO", "TW", "HK", "AU", "JP", "KR", "MY", "NZ"]
NEW13 = ["TH", "VN", "PH", "TW", "HK", "JP", "NZ", "SG", "ID", "MO", "AU", "KR", "MY"]
CHINA12 = ["KH", "CN", "HK", "ID", "JP", "MO", "MY", "SG", "KR", "TW", "TH", "VN"]
GRANDE18 = NEW13 + ["CN", "KH", "LA", "BD", "LK"]

OURS = {'id': 'rp_12fabre_1xee1jy', 'price': 6.81, 'countries': 13, 'isos': NEW13}
CHINA = {'id': 'rp_cuqdhq_xlq7hm', 'price': 19.99, 'countries': 12, 'isos': CHINA12}
GRANDE = {'id': 'rp_grande', 'price': 9.49, 'countries': 18, 'isos': GRANDE18}
OURS_OLD = {'id': 'rp_12fabre_1xee1jy', 'price': 6.81, 'countries': 12, 'isos': OLD12}
# Another 13-country card: China in, Australia out — not our plan.
MINICN = {'id': 'rp_minicn', 'price': 5.99, 'countries': 13,
          'isos': [c for c in NEW13 if c != "AU"] + ["CN"]}


class FakeRegionPage:
    """esim.dog's /regions page, as far as scrape_region looks at it: a list
    of cards, or one card selected (a pinned link opens that way)."""

    def __init__(self, cards, pinned=None):
        self.cards = cards
        self.selected = next((c for c in cards if c['id'] == pinned), None)
        self.opened = []

    @property
    def url(self):
        base = "https://esim.dog/regions?region=asia&data=10&validity=30"
        return base + (f"&plan={self.selected['id']}" if self.selected else "")

    async def inner_text(self, _sel):
        return "plans available\n" + "\n".join(
            f"${c['price']:.2f}\n10GB / 30 days\n{c['countries']} countries\n" for c in self.cards)

    async def wait_for_timeout(self, _ms):
        return None


def scraper_on(page: FakeRegionPage) -> ESIMScraper:
    bot = ESIMScraper.__new__(ESIMScraper)      # no Google, no browser

    async def preselected(_p):
        c = page.selected
        return None if c is None else {'price': c['price'], 'gb': '10GB',
                                       'validity': '30 days', 'countries': c['countries']}

    async def coverage(_p):
        return list(page.selected['isos']) if page.selected else []

    async def choose_another(_p):
        page.selected = None

    async def nothing(_p, *a):
        return None

    async def open_plan(_p, chosen):
        card = next(c for c in page.cards
                    if c['countries'] == chosen['countries'] and c['price'] == chosen['price'])
        page.selected = card
        page.opened.append(card['id'])

    async def price(_p):
        return f"${page.selected['price']:.2f}"

    async def network(_p):
        return {'blocked': '', 'network': 'Networks • LTE + 5G Multiple Networks',
                'breakout_ip': 'Hong Kong', 'tier': ''}

    bot.preselected_region_plan = preselected
    bot.selected_plan_coverage = coverage
    bot.choose_another = choose_another
    bot.expand_region_plans = nothing
    bot.open_region_plan = open_plan
    bot.extract_price = price
    bot.extract_network_info = network
    return bot


def run(cards, pinned, variant, coverage):
    page = FakeRegionPage(cards, pinned)
    bot = scraper_on(page)
    info = {'region': 'asia', 'gb': '10', 'validity': '30'}
    res = asyncio.run(bot.scrape_region(page, info, variant, ",".join(coverage)))
    return res, page


def test_verdict():
    assert region_pin_verdict(12, [], 12, OLD12) == 'keep'
    assert region_pin_verdict(13, NEW13, None, OLD12) == 'keep'
    assert region_pin_verdict(13, NEW13, 12, OLD12) == 'grew'
    # the panel lists exactly the sheet's countries under a new count: the id
    # and the countries agree, the count lags — still ours
    assert region_pin_verdict(13, OLD12, 12, OLD12) == 'grew'
    # lost a country, or a different set: not ours to assume
    assert region_pin_verdict(11, OLD12[:11], 12, OLD12) == 'search'
    assert region_pin_verdict(18, GRANDE18, 12, CHINA12 + ["XX"]) == 'search'
    # many more countries behind the old id: more likely another product
    assert len(set(GRANDE18) - set(OLD12)) > REGION_GROWTH_MAX_ADDED
    assert region_pin_verdict(18, GRANDE18, 12, OLD12) == 'search'
    # bounded on the CARD's count: a lagging panel behind 18 is not growth
    assert region_pin_verdict(18, OLD12, 12, OLD12) == 'search'
    # a smaller count is never growth, whatever the panel still lists
    assert region_pin_verdict(11, OLD12, 12, OLD12) == 'search'
    assert region_pin_verdict(5, OLD12, 12, OLD12) == 'search'
    # nothing to compare with: back to the list, as before
    assert region_pin_verdict(13, NEW13, 12, []) == 'search'
    assert region_pin_verdict(13, [], 12, OLD12) == 'search'


def test_search_order_and_tests():
    plans = [{'countries': c['countries'], 'price': c['price']} for c in (CHINA, OURS, GRANDE)]
    # only cards with the sheet's count, never a bigger one
    assert [p['countries'] for p in region_search_order(plans, 12, OLD12)] == [12]
    # no list in the sheet: the cheapest card with the count, and only that
    assert [p['price'] for p in region_search_order(plans, 12, [])] == [19.99]
    assert region_coverage_accepts(OLD12, OLD12)
    assert not region_coverage_accepts(CHINA12, OLD12)
    assert not region_coverage_accepts(NEW13, OLD12)        # bigger is not ours
    assert not region_coverage_accepts([], OLD12)
    assert region_coverage_accepts([], [])
    note = region_growth_note(12, 13, ["PH"])
    assert "12 → 13" in note and "PH" in note and "13" in note
    assert "נוספו" not in region_growth_note(12, 13, [])
    note.encode("utf-8")                         # real characters, no lone surrogates


def test_the_2026_10_23_incident():
    """Pinned to our plan, which now has 13; the sheet still says 12."""
    res, page = run([OURS, CHINA, GRANDE], OURS['id'], "12", OLD12)
    assert res['price'] == "$6.81", res
    assert res['plan_id'] == OURS['id']
    assert res['countries'] == "13 מדינות"
    assert res['coverage'].split(",") == NEW13
    assert res['variant_now'] == 13
    assert "PH" in res['grew_note']
    assert page.opened == []                    # never left the pinned card


def test_pinned_and_matching_is_untouched():
    res, page = run([OURS, CHINA], OURS['id'], "13", NEW13)
    assert res['price'] == "$6.81" and res['variant_now'] is None and res['grew_note'] == ""
    assert page.opened == []


def test_pinned_panel_matches_sheet_under_new_count():
    """The card says 13, the panel lists the sheet's 12 (the new country has
    no bullet line yet): the pinned plan stays the row's plan."""
    lagging = dict(OURS, isos=OLD12)
    res, page = run([lagging, CHINA, GRANDE], lagging['id'], "12", OLD12)
    assert res['plan_id'] == OURS['id'] and res['price'] == "$6.81"
    assert res['variant_now'] == 13
    assert page.opened == []


def test_single_count_match_is_checked():
    """Our id is gone; the one 12-country card is someone else's plan. Ours
    may be the 13-country card under a new id — or that may be another
    product: the row is left for the owner, never moved."""
    moved = dict(OURS, id='rp_new_id')
    res, page = run([moved, CHINA, GRANDE], None, "12", OLD12)
    assert page.opened == [CHINA['id']], page.opened
    assert res['price'] is None and "לא הוחלפה" in res['note']
    assert res.get('variant_now') is None


def test_no_card_with_our_countries_leaves_the_row():
    """Grande holds every Mini country — and is still not the row's plan."""
    res, page = run([CHINA, GRANDE], None, "12", OLD12)
    assert res['price'] is None
    assert "לא הוחלפה" in res['note']
    assert page.opened == [CHINA['id']]


def test_cheaper_bigger_card_is_never_taken():
    """Two 13-country cards, one of them ours: a 12 sheet takes neither."""
    res, page = run([OURS, CHINA, MINICN], None, "12", OLD12)
    assert res['price'] is None and "לא הוחלפה" in res['note']
    assert 'rp_minicn' not in page.opened and OURS['id'] not in page.opened


def test_pinned_plan_that_shrank_finds_the_same_countries():
    shrunk = dict(OURS_OLD, countries=11, isos=OLD12[:11])
    twin = dict(OURS_OLD, id='rp_twin', price=7.25)
    res, page = run([shrunk, twin, CHINA, GRANDE], shrunk['id'], "12", OLD12)
    assert res['plan_id'] == 'rp_twin' and res['price'] == "$7.25"
    assert res['variant_now'] is None


def test_pinned_plan_that_shrank_never_lands_on_a_bigger_card():
    shrunk = dict(OURS_OLD, countries=11, isos=OLD12[:11])
    res, page = run([shrunk, CHINA, GRANDE], shrunk['id'], "12", OLD12)
    assert res['price'] is None and "לא הוחלפה" in res['note']
    assert GRANDE['id'] not in page.opened


def test_pinned_id_behind_a_much_bigger_plan_is_not_kept():
    """The old id now opens an 18-country plan: past the growth bound, so
    not assumed to be ours; with no exact card the row is left alone."""
    behind = dict(GRANDE, id=OURS['id'])
    res, page = run([behind, CHINA], OURS['id'], "12", OLD12)
    assert res['price'] is None and "לא הוחלפה" in res['note']
    assert res.get('variant_now') is None


def test_pinned_without_a_country_list_is_left_alone():
    """The 2026-10-23 incident with an empty 'מדינות בחבילה' cell: no list
    to compare, so the pinned row is not handed to the cheapest 12-card."""
    res, page = run([OURS, CHINA, GRANDE], OURS['id'], "12", [])
    assert res['price'] is None and "לא הוחלפה" in res['note'], res
    assert page.opened == []


def test_same_count_swap_on_the_pin_is_caught():
    """Our id now opens the China card — 12 countries, as the sheet says."""
    res, page = run([dict(CHINA, id=OURS['id']), GRANDE], OURS['id'], "12", OLD12)
    assert res['price'] is None and "לא הוחלפה" in res['note'], res
    assert "AU" in res['note'] and "NZ" in res['note']


def test_country_swapped_under_the_same_count():
    swapped = dict(OURS_OLD, isos=[c for c in OLD12 if c != "AU"] + ["PH"])
    res, page = run([swapped, CHINA], swapped['id'], "12", OLD12)
    assert res['price'] is None and "AU" in res['note']


def test_lagging_panel_behind_a_much_bigger_card_is_not_kept():
    behind = dict(GRANDE, id=OURS['id'], isos=OLD12)
    res, page = run([behind, CHINA], OURS['id'], "12", OLD12)
    assert res['price'] is None and "לא הוחלפה" in res['note']
    assert res.get('variant_now') is None


def test_shrunk_card_with_a_lagging_panel_is_not_growth():
    shrunk = dict(OURS_OLD, countries=11, isos=OLD12)
    res, page = run([shrunk, CHINA, GRANDE], shrunk['id'], "12", OLD12)
    assert res['price'] is None and res.get('variant_now') is None


def test_no_country_list_keeps_cheapest_by_count():
    res, page = run([CHINA, OURS], None, "12", [])
    assert res['price'] == "$19.99" and res['plan_id'] == CHINA['id']
    assert res['variant_now'] is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ✓ {t.__name__}")
    print(f"\n{len(tests)} passed")
