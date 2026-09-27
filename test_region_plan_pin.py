"""The regional plan id and the country list — 2026-09-27.

esim.dog paints a /regions page truncated ("+ Show 13 more plans") and sells
several plans with the SAME country count on one page. The card the sheet
means is named by esim.dog's own plan id (the `plan=rp_…` the page appends
when a card is chosen) and described by its ISO list; both are written back
to the sheet, and every reader of the link keeps the id where it is.
"""
from esim_price_scraper import (plan_id_from_url, with_plan, with_validity,
                                parse_coverage_codes, parse_region_plans)

LINK = "https://esim.dog/regions?region=asia&data=10&validity=30"
PANEL = ("← Choose another\n$5.96\n10GB / 30 days\n18 countries\nCOUNTRIES\n"
         "🇨🇳\nChina\nCN\n•\nLTE + 5G\n🇹🇭\nThailand\nTH\n•\nLTE + 5G\n"
         "🇭🇰\nHong Kong\nHK\n•\nLTE + 5G\nNETWORKS • LTE + 5G\nChina Unicom\n"
         "Breakout IP: Hong Kong or other\n")


def test_pin_and_read_back():
    pinned = with_plan(LINK, "rp_1u11vml_1ggps09")
    assert pinned == LINK + "&plan=rp_1u11vml_1ggps09"
    assert plan_id_from_url(pinned) == "rp_1u11vml_1ggps09"
    assert plan_id_from_url(LINK) == ""
    assert with_plan(LINK, "") == LINK
    # re-pinning replaces, never stacks
    assert with_plan(pinned, "rp_other").count("plan=") == 1
    assert plan_id_from_url(with_plan(pinned, "rp_other")) == "rp_other"


def test_day_switch_keeps_the_pin():
    pinned = with_plan(LINK, "rp_1u11vml_1ggps09")
    assert plan_id_from_url(with_validity(pinned, 7)) == "rp_1u11vml_1ggps09"


def test_panel_coverage_codes():
    assert parse_coverage_codes(PANEL) == ["CN", "TH", "HK"]
    # the card list (flags only, no names/codes) yields nothing
    assert parse_coverage_codes("$5.96\n10GB / 30 days\n3 countries\nCOUNTRIES • LTE\n🇰🇬\n🇰🇿\n🇵🇰\n") == []
    assert parse_coverage_codes("") == []


def test_selected_panel_parses_as_one_plan():
    plans = parse_region_plans(PANEL)
    assert [(p['price'], p['countries']) for p in plans] == [(5.96, 18)]
