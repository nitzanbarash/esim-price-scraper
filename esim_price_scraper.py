#!/usr/bin/env python3
"""
eSIM.dog Price Scraper — Nitzan's auto-updating price table.

Reads links from the Google Sheet, scrapes esim.dog, and auto-fills:
  code, countries, GB, source, validity, buy price, route, profit, stock status
plus tracking: previous price, last updated, changed?, last change date,
Networks, Breakout IP.

Route selection: QUALITY first, cheapness only when it earns the drop.
  Tiers (Blue,Black) > (Pink) > (Yellow,Green); cheapest within a tier, Blue
  wins ties. Take Pink only if Blue/Black costs >10% above it; take
  Yellow/Green only if it is >=20% below Pink. Amber is never sold — it is
  1 Mbps with no hotspot — and any route whose own info box declares a speed
  cap or 'No hotspot' is refused the same way, whatever it is called.
Stock detection: if the page shows different GB/validity than the URL requested.
Minimum size: packages under 1GB are not sold — flagged out-of-stock, not scraped.
Profitability: 1GB packages allow up to -20% loss; all others require >=20% profit.
Regional codes: A=mini, B=grande (e.g. 1.0A.10, 1.0B.5).
"""

import asyncio
import json
import os
import sys
import re
import time as _time
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from playwright.async_api import async_playwright, Page
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from esim_country_data import (
    country_from_slug, make_country_code, make_region_code, hebrew_name,
)

# ── Google Sheets ────────────────────────────────────────────────
SHEET_ID = "108D3BUV-MNcIuRZuKUgb-E-b1Ra8moxWZZyI5JxnyRo"
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# Columns are located by their HEADER TEXT (row 1), not by fixed positions, so the
# sheet keeps working even if you insert/move/reorder columns. Each logical field
# maps to the exact header string it lives under.
HEADER_KEYS = {
    'code':        "חבילה (קוד)",
    'countries':   "מדינות",
    'gb':          "GB",
    'source':      "מקור",
    'link':        "קישור",
    'validity':    "זמן חבילה",
    'price':       "מחיר קנייה",
    'prev':        "מחיר קודם",
    'updated':     "עודכן לאחרונה",
    'changed':     "השתנה?",
    'last_change': "שינוי אחרון",
    'network':     "Networks",
    'breakout_ip': "Breakout IP",
    'variant':     "וריאנט (אזורי)",
    'route':       "Route",
    'profit':      "רווח (כדאיות)",
    'stock':       "במלאי/רווחי",
    'my_price':    "מחיר שלי",
}

# esim.dog grades every route in its own info box, and that wording — not the
# colour — is the real hierarchy. Read off the live site (GB + TH, 2026-08-29):
#   BEST COVERAGE & RELIABILITY   Blue    full-speed, hotspot, LTE+5G, named carriers
#   GREAT COVERAGE & RELIABILITY  Black   full-speed, hotspot, LTE+5G, named carriers
#   GOOD RELIABILITY              Pink    full-speed, hotspot, LTE+5G, local network
#   GOOD RELIABILITY              Yellow  full-speed, hotspot, LTE only
#   ESSENTIAL COVERAGE            Amber   1 Mbps max, NO hotspot        ← never sell
# The old order put Pink above Black; the site's own labels say otherwise.
ROUTE_QUALITY = ['Blue', 'Black', 'Pink', 'Yellow', 'Green']
TIER_RANK = {'BEST': 0, 'GREAT': 1, 'GOOD': 2}

# Owner's buying policy (2026-08-29). Cheapest-first was never what he wanted:
# it trades a customer's 5G for pennies. Quality is the default and a downgrade
# has to EARN its way in, so each tier is taken only when it undercuts the tier
# above it by enough to be worth the drop.
ROUTE_TIERS = (
    ('Blue', 'Black'),      # BEST / GREAT — full-speed, LTE+5G, named carriers
    ('Pink',),              # GOOD — full-speed, LTE+5G, local network
    ('Yellow', 'Green'),    # GOOD — full-speed, LTE only, no 5G
)
# The two gates, stated against the bases the owner named and deliberately NOT
# normalised into one formula — these are commercial numbers, not arithmetic:
#   drop to Pink         only if Blue/Black costs MORE than 10% above Pink
#   drop to Yellow/Green only if it costs AT LEAST 20% below Pink
TIER_GATE = (1.10, 0.80)

# Refused by name whatever it costs. Amber appeared 2026-08-29 and was instantly
# the cheapest route on every page, so a price-first choice picked it for every
# package — a 1 Mbps, hotspot-less line sold at full-speed prices.
BLOCKED_ROUTES = {'Amber'}

# Smallest package we still sell. Owner's decision (2026-07-22): sub-1GB plans
# are pennies below the 1GB ones, and esim.dog's own 500MB row is unsellable
# (data=0.5 makes their checkout answer "Plan data mismatch: expected 0.5GB,
# got 0.49GB"). Rows below this are flagged, not deleted — the owner keeps the
# link and can raise the size whenever they want the row back.
MIN_SELLABLE_GB = 1.0
BELOW_MIN_LABEL = 'מתחת ל-1GB'

# How long the scrape may run before it stops itself and saves what it has.
# The workflow allows more than this, deliberately: a run killed from OUTSIDE
# dies at an arbitrary point with nothing able to report what it skipped,
# which is how 2026-08-19 produced four "cancelled" runs and no explanation.
# Stopping ourselves keeps the last save and the summary. Healthy runs take
# 17-26 minutes, so this only fires when something is genuinely wrong.
SCRAPE_BUDGET_MIN = float(os.environ.get('SCRAPE_BUDGET_MIN', 45))

def route_name_key(name: str) -> str:
    """'🎁 Amber' -> 'Amber'. Strips emoji and spacing the site decorates with."""
    return re.sub(r'[^\w]', '', name).capitalize()


def route_quality_rank(name: str, tier: str = "") -> int:
    """Lower = better. The site's own tier word wins; colour is the fallback."""
    if tier:
        t = TIER_RANK.get(tier.upper())
        if t is not None:
            return t
    clean = route_name_key(name)
    for i, r in enumerate(ROUTE_QUALITY):
        if clean == r:
            return len(TIER_RANK) + i
    return len(TIER_RANK) + len(ROUTE_QUALITY)


def choose_route(priced: Dict[str, float]) -> Optional[str]:
    """The owner's route policy. `priced` holds ALLOWED routes only.

    Quality first, and a cheaper tier is taken only when it clears its gate
    against the tier above it. Within a tier the cheaper route wins, ties
    going to the one listed first (Blue over Black).

    A tier missing from the page is skipped, and the gate is then measured
    against the best tier that IS there — the alternative, refusing to
    compare, would strand every country that does not offer Pink.
    """
    best = []
    for tier in ROUTE_TIERS:
        cands = [(priced[n], rank, n) for rank, n in enumerate(tier) if n in priced]
        best.append(min(cands) if cands else None)

    chosen = None
    for i, cand in enumerate(best):
        if cand is None:
            continue
        if chosen is None:
            chosen = cand
            continue
        # The gate is measured against the tier DIRECTLY ABOVE this one, not
        # against whatever is winning so far. Those differ whenever a middle
        # tier is present and lost its own gate, and measuring against the
        # winner then inverts the policy: the dearer the premium route, the
        # EASIER it became to sell the LTE-only one. Blue $10 / Pink $9.50 /
        # Yellow $7.80 kept Blue (Pink not 10% cheaper) and then handed the
        # customer Yellow, because $7.80 clears 20% off *Blue* while missing
        # 20% off Pink, which is the number the policy actually names.
        ref = next(best[j][0] for j in range(i - 1, -1, -1) if best[j] is not None)
        price = cand[0]
        # i == 1 is the drop to Pink, i == 2 the drop to Yellow/Green.
        good_enough = (ref > price * TIER_GATE[0]) if i == 1 else (price <= ref * TIER_GATE[1])
        if good_enough:
            chosen = cand

    return chosen[2] if chosen else None


def route_disqualified(info: str) -> str:
    """Why this route must not be sold, or '' if it is fine.

    Judged on what the info box CLAIMS, not on the colour, so the next route
    esim.dog invents is caught the same way Amber was — by declaring a speed
    cap or no hotspot. An unrecognised colour that claims neither is still
    allowed, ranked last: refusing every unknown would empty the sheet the
    first time the wording changes.
    """
    m = re.search(r'(\d+(?:\.\d+)?)\s*Mbps\s*max', info, re.IGNORECASE)
    if m:
        return f"capped at {m.group(1)} Mbps"
    if re.search(r'\bNo\s+hotspot\b', info, re.IGNORECASE):
        return "no hotspot"
    if re.search(r'\bESSENTIAL\s+COVERAGE\b', info, re.IGNORECASE):
        return "Essential Coverage tier"
    return ""


def col_letter(idx: int) -> str:
    """0-based column index -> spreadsheet letter (0->A, 25->Z, 26->AA)."""
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


def force_vpn_false(url: str) -> str:
    """
    Force vpn=false on every link. Merely REMOVING the param is not enough:
    on some packages (e.g. il 500MB) the site bundles the VPN by default when
    the param is absent, which replaces the "One-time payment" line in the
    Payment Summary with a VPN-inclusive "Total due today" — breaking price
    extraction and inflating the price with the VPN subscription.
    """
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query['vpn'] = ['false']
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def parse_url(url: str) -> Dict:
    """Classify the URL and pull out gb/validity/slug/region."""
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    q = parse_qs(parsed.query)
    gb = q.get('data', [''])[0]
    validity = q.get('validity', [''])[0]

    first = path.split('/')[0] if path else ""
    if first.startswith('regions'):
        return {'type': 'region', 'region': q.get('region', [''])[0], 'gb': gb, 'validity': validity}
    if len(first) == 2 and first.isalpha():
        return {'type': 'country', 'slug': first, 'gb': gb, 'validity': validity}
    return {'type': 'unknown', 'gb': gb, 'validity': validity}


def parse_region_plans(text: str) -> List[Dict]:
    """Parse the 'Available Plans' list on a /regions page into plan dicts."""
    if 'plans available' in text:
        text = text.split('plans available', 1)[1]
    plans = []
    pattern = re.compile(
        r'\$([\d.]+)\s+([\d.]+GB)\s*/\s*([\dA-Za-z ]+?)\s+(\d+)\s+countries',
        re.S,
    )
    for m in pattern.finditer(text):
        plans.append({
            'price': float(m.group(1)),
            'gb': m.group(2),
            'validity': m.group(3).strip(),
            'countries': int(m.group(4)),
        })
    return plans


class ESIMScraper:
    def __init__(self):
        self.sheet_service = None
        self.setup_google_sheets()

    def setup_google_sheets(self):
        """
        Load Google credentials. In the cloud (GitHub Actions) the service-account
        JSON is provided via the GOOGLE_CREDENTIALS_JSON secret; locally it's read
        from credentials.json.
        """
        try:
            env_creds = os.environ.get('GOOGLE_CREDENTIALS_JSON')
            if env_creds:
                info = json.loads(env_creds)
                creds = Credentials.from_service_account_info(info, scopes=SCOPES)
                print("✓ Google Sheets API connected (env credentials)")
            else:
                creds = Credentials.from_service_account_file('credentials.json', scopes=SCOPES)
                print("✓ Google Sheets API connected (file credentials)")
            self.sheet_service = build('sheets', 'v4', credentials=creds)
        except FileNotFoundError:
            print("⚠️  credentials.json not found (and GOOGLE_CREDENTIALS_JSON not set)")
            self.sheet_service = None

    # ── price extraction ─────────────────────────────────────────
    async def extract_price(self, page: Page) -> Optional[str]:
        """Extract 'One-time payment' from the Payment Summary."""
        try:
            await page.wait_for_selector("text=Payment Summary", timeout=8000)
            page_text = await page.inner_text("body")
            if "One-time payment" in page_text:
                idx = page_text.find("One-time payment")
                after = page_text[idx:idx + 200]
                m = re.search(r'(?:USD\s*\$?|\$)\s*([\d]+\.?[\d]*)', after)
                if m:
                    return f"${float(m.group(1)):.2f}"
            # Fallback: no "One-time payment" line (e.g. the site bundled an
            # add-on and switched to "Total due today"). The eSIM item line
            # right after "Payment Summary" still shows the package price —
            # take the lowest price listed before the fee/total lines, which
            # skips crossed-out prices and never includes VPN/fee amounts.
            idx = page_text.find("Payment Summary")
            if idx >= 0:
                seg = page_text[idx:idx + 400]
                cut = re.search(r'VPN|Service fee|Total due|One-time', seg)
                if cut:
                    seg = seg[:cut.start()]
                prices = [float(x) for x in re.findall(r'\$\s*([\d]+\.[\d]{2})', seg)]
                if prices:
                    return f"${min(prices):.2f}"
            return None
        except Exception as e:
            print(f"  ❌ Error extracting price: {e}")
            return None

    async def find_route_container(self, page: Page):
        """
        Locate the section containing all Route buttons.
        Route names/colors vary by country (Blue/Pink/Black, Yellow/Black, ...), so we
        locate the route selector generically.

        The Route section can span MULTIPLE rows:
          <div class="mt-6">               ← section root (2 levels up from <label>)
            <div class="mb-3"><label>Route</label></div>
            <div class="flex gap-1 ...">   ← row 1: Blue, Yellow
            <div class="flex gap-1 ...">   ← row 2: Pink, Black, Green  (often missed!)
        We go up to the section root to capture all rows.
        """
        # Find the leaf element whose text is exactly "Route"
        route_label = await page.query_selector(
            "xpath=//*[normalize-space(text())='Route' and not(*)]")
        if not route_label:
            return None
        # Go up TWO levels to reach the section container that holds all route rows.
        # Structure: section-root > label-wrapper(div.mb-3) > label "Route"
        return await route_label.evaluate_handle(
            "el => el.parentElement && el.parentElement.parentElement")

    async def expand_routes(self, page: Page, container) -> None:
        """Click 'Show more routes' if present, revealing the hidden route row."""
        btns = await container.query_selector_all("button")
        for b in btns:
            text = (await b.text_content() or "").strip().lower()
            if "show more" in text:
                try:
                    await b.click()
                    await page.wait_for_timeout(1000)
                except Exception:
                    pass
                return

    async def list_route_names(self, page: Page, container) -> List[str]:
        """Expand 'Show more routes' and return the names of all route buttons."""
        await self.expand_routes(page, container)
        btns = await container.query_selector_all("button")
        names = []
        for b in btns:
            name = (await b.text_content() or "").strip()
            if name and not name.lower().startswith("show ") and "vpn" not in name.lower():
                names.append(name)
        seen = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    async def select_route(self, page: Page, container, name: str) -> bool:
        """
        Click the route button matching `name`.
        Selecting any route re-collapses the "Show more routes" section, hiding
        not-yet-clicked hidden routes — so re-expand + re-find before each click.
        """
        await self.expand_routes(page, container)
        btns = await container.query_selector_all("button")
        for b in btns:
            if (await b.text_content() or "").strip() == name:
                try:
                    await b.click()
                    await page.wait_for_timeout(2500)
                    return True
                except Exception:
                    return False
        return False

    async def get_all_routes(self, page: Page) -> Tuple[Dict[str, Dict], List[str]]:
        """Click every route option and record price, tier and network info.

        Returns (allowed, refused). The refusals are returned rather than only
        printed because "this page offers nothing we may sell" and "this page
        has no route selector" have to be told apart by the caller — they lead
        to opposite outcomes for the row.

        A blocked route is never clicked. That keeps it out of the running by
        construction rather than by out-scoring it later — and skipping one
        button per package buys back most of the runtime Amber cost us, since
        every extra route also forces the hidden row to be re-expanded.
        """
        routes: Dict[str, Dict] = {}
        refused: List[str] = []
        container = await self.find_route_container(page)
        if not container:
            return routes, refused
        for name in await self.list_route_names(page, container):
            if route_name_key(name) in BLOCKED_ROUTES:
                print(f"    {name}: skipped — blocked route")
                refused.append(f"{name} (blocked)")
                continue
            if await self.select_route(page, container, name):
                price = await self.extract_price(page)
                net_info = await self.extract_network_info(page)
                why = net_info['blocked']
                if why:
                    print(f"    {name}: {price} — REFUSED ({why})")
                    refused.append(f"{name} ({why})")
                    continue
                if price:
                    routes[name] = {
                        'price': price,
                        'network': net_info['network'],
                        'breakout_ip': net_info['breakout_ip'],
                        'tier': net_info['tier'],
                    }
                    print(f"    {name}: {price}  [{net_info['tier'] or '?'}]"
                          f"  |  {net_info['network'][:50]}")
        return routes, refused

    async def extract_network_info(self, page: Page) -> Dict[str, str]:
        """
        Read the blue info box for the CURRENTLY SELECTED route:
          "Networks • LTE + 5G <carrier>"  and  "Breakout IP: <city>"
        """
        try:
            page_text = await page.inner_text("body")
        except Exception:
            return {'network': '', 'breakout_ip': '', 'tier': '', 'blocked': ''}

        # The box always ends at "Breakout IP:", so the 400 characters before it
        # are the box and nothing else. Anchoring the speed-cap and hotspot
        # checks here keeps them away from the page's own marketing copy and
        # from the FAQ line "Is there a speed limit on these plans?".
        box = ""
        m0 = re.search(r'Breakout IP:', page_text, re.IGNORECASE)
        if m0:
            box = page_text[max(0, m0.start() - 400):m0.start()]

        tier = ""
        mt = re.search(r'\b(BEST|GREAT|GOOD|ESSENTIAL)\b[^\n]*?\b(?:COVERAGE|RELIABILITY)\b',
                       box, re.IGNORECASE)
        if mt:
            tier = mt.group(1).upper()

        # The header line varies ("LTE + 5G China Mobile" / "LTE\nLocal network" / ...),
        # so capture everything up to the next blank line / "Breakout" and normalize.
        # Inside the box the bullet is optional — Amber's line is a bare
        # "NETWORKS", which is why its Networks cell came back empty on
        # 2026-08-29 — but the page-wide fallback still demands the bullet, or
        # it would match the "premium networks in ..." marketing paragraph.
        network = ""
        for hay, pat in ((box, r'NETWORKS?\s*(?:•\s*)?(.+?)(?:\n\s*\n|\Z)'),
                         (page_text, r'Networks?\s*•\s*(.+?)(?:\n\s*\n|\nBreakout|\Z)')):
            if not hay:
                continue
            m = re.search(pat, hay, re.IGNORECASE | re.DOTALL)
            if m:
                content = re.sub(r'\s+', ' ', m.group(1)).strip()
                if content:
                    network = f"Networks • {content}"
                    break

        breakout_ip = ""
        m2 = re.search(r'Breakout IP:\s*(.+)', page_text, re.IGNORECASE)
        if m2:
            breakout_ip = m2.group(1).strip()

        return {'network': network, 'breakout_ip': breakout_ip,
                'tier': tier, 'blocked': route_disqualified(box)}

    async def read_page_package_info(self, page: Page) -> Dict[str, str]:
        """Read the actual GB and validity shown on the page (for stock detection)."""
        try:
            text = await page.inner_text("body")
        except Exception:
            return {}
        result = {}
        # Prefer the Payment Summary item line ("eSIM Israel • 500MB • 1d") —
        # it shows the package actually selected. A body-wide GB search can hit
        # the data-size selector chips instead (e.g. "15GB" while the selected
        # package is 500MB, which doesn't even end in GB).
        m = re.search(r'•\s*([\d.]+)\s*(GB|MB)\s*•\s*(\d+)\s*d\b', text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if m.group(2).upper() == 'MB':
                val /= 1000.0
            result['page_gb'] = ('%g' % val)
            result['page_validity'] = m.group(3)
            return result
        m = re.search(r'(\d+(?:\.\d+)?)\s*GB', text)
        if m:
            result['page_gb'] = m.group(1)
        m2 = re.search(r'(\d+)\s*(?:Days?|days?)', text)
        if m2:
            result['page_validity'] = m2.group(1)
        return result

    # ── per-type scraping ────────────────────────────────────────
    async def scrape_country(self, page: Page, info: Dict) -> Dict:
        slug = info['slug']
        country_name = hebrew_name(slug)

        page_info = await self.read_page_package_info(page)

        # candidates: (price_float, route_quality_rank, route_name, net_info)
        candidates = []
        default_price = await self.extract_price(page)

        routes, refused = await self.get_all_routes(page)
        if routes:
            print(f"  📌 Routes found (default {default_price}): {len(routes)}")
            for name, rdata in routes.items():
                try:
                    candidates.append((
                        float(rdata['price'].replace('$', '')),
                        route_quality_rank(name, rdata.get('tier', '')),
                        name,
                        rdata,
                    ))
                except Exception:
                    pass
        elif default_price:
            # No route selector on this page — the page default is all there is.
            # It still has to clear the same bar: a page that offers only a
            # capped line is a package we do not sell, not a cheap one.
            net_info = await self.extract_network_info(page)
            if net_info['blocked']:
                print(f"  ⛔ Default route refused ({net_info['blocked']})")
                refused.append(net_info['blocked'])
            else:
                candidates.append((
                    float(default_price.replace('$', '')),
                    999,
                    None,
                    {'price': default_price, 'network': net_info['network'],
                     'breakout_ip': net_info['breakout_ip']},
                ))

        network = ""
        breakout_ip = ""
        route_name = ""
        if candidates:
            # Quality first, cheapness only when it clears the owner's gates.
            # This used to be min(price, quality) — pure cheapest-first — which
            # is how a $1.99 route beat a $2.99 one on every package regardless
            # of what the customer actually got.
            priced = {c[2]: c[0] for c in candidates if c[2]}
            pick = choose_route(priced) if priced else None
            if pick:
                best = next(c for c in candidates if c[2] == pick)
            else:
                best = min(candidates, key=lambda c: (c[0], c[1]))
            price_val, _, route_name, rdata = best
            price = f"${price_val:.2f}"
            route_display = route_name or ""
            print(f"  ✓ Best = {price}" + (f" [{route_display}]" if route_display else ""))

            # Re-select the best route to ensure page state is correct
            if route_name:
                container = await self.find_route_container(page)
                if container:
                    await self.select_route(page, container, route_name)
                    rdata_fresh = await self.extract_network_info(page)
                    rdata['network'] = rdata_fresh['network']
                    rdata['breakout_ip'] = rdata_fresh['breakout_ip']

            network = rdata['network']
            breakout_ip = rdata['breakout_ip']
        else:
            price = None

        gb = (info['gb'] or "").lower()
        actual_gb = page_info.get('page_gb', gb)
        actual_validity = page_info.get('page_validity', info['validity'])

        # Every route the page offered was refused. That is NOT "could not read
        # the price" — we read them all and would sell none. Reported as a read
        # failure the row simply keeps yesterday's price and yesterday's Route,
        # and the purchase bot keeps buying that route: a package we just judged
        # unsellable stays on sale, silently. So the row is pulled from sale
        # instead, the same as any other package we cannot supply today.
        unsellable = bool(refused) and not candidates
        if unsellable:
            print(f"  ⛔ Nothing sellable here — refused: {', '.join(refused)}")

        # Stock detection: page shows different package than requested
        out_of_stock = unsellable
        if info['gb'] and actual_gb and str(info['gb']) != str(actual_gb):
            print(f"  ⚠️ Stock mismatch: requested {info['gb']}GB but page shows {actual_gb}GB")
            out_of_stock = True
        if info['validity'] and actual_validity and str(info['validity']) != str(actual_validity):
            print(f"  ⚠️ Stock mismatch: requested {info['validity']}d but page shows {actual_validity}d")
            out_of_stock = True

        return {
            'price': price,
            'countries': country_name,
            'gb': f"{actual_gb}gb" if actual_gb else "",
            'validity': f"{actual_validity}d" if actual_validity else "",
            'code': make_country_code(slug, actual_gb) if actual_gb else "",
            'network': network,
            'breakout_ip': breakout_ip,
            'route': route_display if route_name else "",
            'out_of_stock': out_of_stock,
            'note': ("לא נמכר — כל המסלולים נפסלו: " + ", ".join(refused)) if unsellable
                    else ("" if price else "Could not read price"),
        }

    async def scrape_region(self, page: Page, info: Dict, variant: str) -> Dict:
        text = await page.inner_text("body")
        plans = parse_region_plans(text)
        if not plans:
            return {'price': None, 'countries': '', 'gb': '', 'validity': '',
                    'code': '', 'network': '', 'breakout_ip': '', 'route': '',
                    'out_of_stock': False,
                    'note': 'No plans found on region page'}

        chosen = None
        m = re.search(r'\d+', variant or '')
        if m:
            want = int(m.group())
            matches = [p for p in plans if p['countries'] == want]
            if matches:
                chosen = min(matches, key=lambda p: p['price'])
        if chosen is None:
            options = ", ".join(f"{p['countries']} מדינות ${p['price']:.2f}" for p in plans)
            return {'price': None, 'countries': '', 'gb': '', 'validity': '',
                    'code': '', 'network': '', 'breakout_ip': '', 'route': '',
                    'out_of_stock': False,
                    'note': f"בחר וריאנט (מספר מדינות) בעמודת 'וריאנט (אזורי)' — "
                            f"{len(plans)} חבילות: {options}"}

        price = f"${chosen['price']:.2f}"
        try:
            await page.locator(f"text={chosen['countries']} countries").first.click()
            await page.wait_for_timeout(2500)
            real_price = await self.extract_price(page)
            if real_price:
                price = real_price
        except Exception as e:
            print(f"  ⚠️  Could not open plan, using listed price: {e}")

        net_info = await self.extract_network_info(page)
        if net_info['blocked']:
            print(f"  ⛔ Region route refused ({net_info['blocked']})")
            return {'price': None, 'countries': '', 'gb': '', 'validity': '',
                    'code': '', 'network': '', 'breakout_ip': '', 'route': '',
                    'out_of_stock': True,
                    'note': f"לא נמכר — המסלול נפסל: {net_info['blocked']}"}

        gb = (info['gb'] or chosen['gb'].replace('GB', '')).lower().replace('gb', '')
        print(f"  ✓ Region plan: {chosen['countries']} מדינות = {price}")
        return {
            'price': price,
            'countries': f"{chosen['countries']} מדינות",
            'gb': f"{gb}gb" if gb else "",
            'validity': f"{info['validity']}d" if info['validity'] else chosen['validity'],
            'code': make_region_code(info['region'], gb, chosen['countries']) if gb else "",
            'network': net_info['network'],
            'breakout_ip': net_info['breakout_ip'],
            'route': '',
            'out_of_stock': False,
            'note': "",
        }

    async def scrape(self, url: str, variant: str = "") -> Dict:
        print(f"\n🔗 {url}")
        clean_url = force_vpn_false(url)
        if clean_url != url:
            print("  🔐 Forced vpn=false")
        info = parse_url(clean_url)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await (await browser.new_context()).new_page()
            try:
                await page.goto(clean_url, wait_until='domcontentloaded', timeout=30000)
                if info['type'] == 'region':
                    await page.wait_for_timeout(5000)
                    return await self.scrape_region(page, info, variant)
                else:
                    try:
                        await page.wait_for_selector("text=Payment Summary", timeout=15000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(2000)
                    if info['type'] == 'country':
                        return await self.scrape_country(page, info)
                    price = await self.extract_price(page)
                    return {'price': price, 'countries': '', 'gb': '', 'validity': '',
                            'code': '', 'network': '', 'breakout_ip': '', 'route': '',
                            'out_of_stock': False,
                            'note': 'Partial link — add data/validity params'}
            except Exception as e:
                print(f"  ❌ {e}")
                return {'price': None, 'countries': '', 'gb': '', 'validity': '',
                        'code': '', 'network': '', 'breakout_ip': '', 'route': '',
                        'out_of_stock': False, 'note': f'Error: {e}'}
            finally:
                await browser.close()

    async def scrape_confirmed(self, link: str, variant: str, expected: str) -> Dict:
        """
        Reliable read with confirmation against transient misreads.
        - If the first read matches the stored price → trust it (1 read, fast).
        - Otherwise (first check or a change) read again; if two reads agree, use it.
        - If still disagreeing, read a 3rd time and take the value that repeats,
          or the lowest price if all three differ (flag it as unstable).
        """
        def val(p):
            try:
                return float(p.replace('$', ''))
            except:
                return None

        r1 = await self.scrape(link, variant)
        v1 = r1['price']
        # Stable day-to-day case: matches stored price → done, no extra reads
        if expected and v1 and abs((val(v1) or -1) - (val(expected) or -2)) < 0.001:
            return r1

        # Needs confirmation (first check or apparent change)
        print(f"  🔁 Confirming read ({v1})...")
        r2 = await self.scrape(link, variant)
        v2 = r2['price']
        if v1 and v2 and abs(val(v1) - val(v2)) < 0.001:
            return r1  # two reads agree

        # Third read to break the tie
        r3 = await self.scrape(link, variant)
        v3 = r3['price']
        candidates = [(r1, v1), (r2, v2), (r3, v3)]
        valid = [(r, v) for r, v in candidates if v]
        if not valid:
            return r1
        # majority value if any repeats
        from collections import Counter
        counts = Counter(v for _, v in valid)
        best, n = counts.most_common(1)[0]
        if n >= 2:
            for r, v in valid:
                if v == best:
                    return r
        # all differ → take the lowest price, flag as unstable
        r, v = min(valid, key=lambda rv: val(rv[1]))
        r = dict(r)
        r['note'] = (r.get('note', '') + f' ⚠️ קריאה לא יציבה ({v1}/{v2}/{v3})').strip()
        print(f"  ⚠️  Unstable ({v1}/{v2}/{v3}) — took lowest {v}")
        return r

    # ── sheet I/O ────────────────────────────────────────────────
    def read_rows(self):
        """
        Returns (items, col_index). Columns are located by matching the header row
        (row 1) against HEADER_KEYS, so the sheet keeps working even after columns
        are inserted, moved, or reordered.
        """
        result = self.sheet_service.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range='A1:Z').execute()
        rows = result.get('values', [])
        if not rows:
            return [], {}

        header = rows[0]
        col_index = {}
        for key, header_text in HEADER_KEYS.items():
            try:
                col_index[key] = header.index(header_text)
            except ValueError:
                pass  # header missing — that field is simply skipped

        width = max(col_index.values(), default=0) + 1
        items = []
        for idx, row in enumerate(rows[1:], start=2):
            row = row + [""] * (width - len(row))
            link = row[col_index['link']] if 'link' in col_index else ""
            if link and link.startswith("http"):
                def _get(key):
                    return row[col_index[key]] if key in col_index else ""
                items.append({
                    'row': idx,
                    'link': link,
                    'old_price': _get('price'),
                    'variant': _get('variant'),
                    'old_changed': _get('changed'),
                    'my_price': _get('my_price'),
                    'old_gb': _get('gb'),
                    'old_validity': _get('validity'),
                    'old_code': _get('code'),
                })
        return items, col_index

    async def run(self):
        if not self.sheet_service:
            print("⚠️  Google Sheets not configured. Cannot run.")
            return -1
        items, col = self.read_rows()
        if not items:
            print("ℹ️  No links found in column E.")
            return -1

        print(f"\n📋 Checking {len(items)} packages...\n")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        updates = []

        def put(row, key, value):
            if key not in col:
                return
            updates.append({'range': f'{col_letter(col[key])}{row}',
                            'values': [[value]]})

        def flush(done_count):
            if not updates:
                return
            self.setup_google_sheets()
            CHUNK = 50
            for i in range(0, len(updates), CHUNK):
                batch = updates[i:i + CHUNK]
                for attempt in range(4):
                    try:
                        self.sheet_service.spreadsheets().values().batchUpdate(
                            spreadsheetId=SHEET_ID,
                            body={'data': batch, 'value_input_option': 'USER_ENTERED'}
                        ).execute(num_retries=3)
                        break
                    except Exception as e:
                        print(f"  ⚠️ write chunk {i // CHUNK + 1} attempt {attempt + 1} "
                              f"failed: {e}")
                        if attempt == 3:
                            raise
                        self.setup_google_sheets()
            print(f"  💾 saved through package {done_count}/{len(items)} "
                  f"({_time.time() - t0:.0f}s elapsed)")
            updates.clear()

        def to_val(p):
            try:
                return float(str(p).replace('$', '').replace(',', ''))
            except Exception:
                return None

        done = 0
        skipped = 0
        t0 = _time.time()
        deadline = t0 + SCRAPE_BUDGET_MIN * 60
        for it in items:
            # Out of time: save, say exactly what was left unchecked, and stop.
            if _time.time() > deadline:
                skipped = len(items) - done
                print(f"\n⏳ Reached the {SCRAPE_BUDGET_MIN:g}-minute budget — "
                      f"stopping with {skipped} of {len(items)} packages unchecked.")
                break
            # Save every 10 packages. The write used to happen once at the very
            # end, so a run killed by the workflow time limit (4x on
            # 2026-08-19) lost EVERYTHING and the sheet went a full day stale.
            # Each package's updates are self-contained - flushing loses at
            # most the tail.
            if done and done % 10 == 0:
                flush(done)
            done += 1
            r = it['row']

            # Below the minimum size: flag and move on WITHOUT scraping. A
            # non-empty "במלאי/רווחי" is what the site sync reads as
            # out-of-stock, so the package disappears from waverole.com while
            # the row itself stays intact in the sheet.
            req_gb = to_val(parse_url(it['link']).get('gb'))
            if req_gb is not None and req_gb < MIN_SELLABLE_GB:
                put(r, 'updated', ts)
                put(r, 'stock', BELOW_MIN_LABEL)
                put(r, 'changed', f'{BELOW_MIN_LABEL} — לא נמכר')
                print(f"  ⛔ Row {r}: {req_gb}GB is under {MIN_SELLABLE_GB:g}GB — not sold, skipped")
                continue

            t_pkg = _time.time()
            res = await self.scrape_confirmed(it['link'], it['variant'], it['old_price'])
            # A package that takes minutes is the whole story of a run that ran
            # out of time, and the per-row cost is invisible in a total.
            dt = _time.time() - t_pkg
            if dt > 60:
                print(f"  🐌 Row {r} took {dt:.0f}s")
            new_price = res['price']

            if new_price is None:
                put(r, 'updated', ts)
                put(r, 'changed', res['note'] or 'Check failed')
                # A refused row has NO price precisely because we refused it,
                # so it lands here and never reaches the stock handling below.
                # Without this the stock cell stays empty, the row still reads
                # as on sale, and the purchase bot goes on buying yesterday's
                # route — which is the exact silence this whole change exists
                # to end. A missing price on its own is still just a bad read:
                # that row keeps its old value and is retried next run.
                if res.get('out_of_stock'):
                    put(r, 'stock', 'לא במלאי')
                    print(f"  ⛔ Row {r}: pulled from sale — {res['note']}")
                continue

            new_val = to_val(new_price)
            old = it['old_price']
            old_val = to_val(old) if old else None

            price_changed = (old_val is not None and new_val is not None
                             and abs(new_val - old_val) > 0.001)
            first_time = (old_val is None and new_val is not None)

            # ── Stock detection ──
            if res.get('out_of_stock'):
                put(r, 'updated', ts)
                put(r, 'stock', 'לא במלאי')
                put(r, 'changed', res.get('note')
                    or f"לא במלאי — הדף הציג {res['gb']}/{res['validity']}")
                print(f"  ❌ Row {r}: out of stock")
                continue

            # ── Update all fields ──
            # A code already in the sheet is the package's identity (the site
            # and the purchase bot key on it) — never overwrite it. Only fill
            # the auto-generated code into an EMPTY cell.
            if res['code'] and not (it.get('old_code') or '').strip():
                put(r, 'code', res['code'])
            if res['countries']:
                put(r, 'countries', res['countries'])
            if res['gb']:
                put(r, 'gb', res['gb'])
            put(r, 'source', 'esim.dog')
            if res['validity']:
                put(r, 'validity', res['validity'])
            put(r, 'price', new_price)
            put(r, 'updated', ts)

            if res.get('network'):
                put(r, 'network', res['network'])
            if res.get('breakout_ip'):
                put(r, 'breakout_ip', res['breakout_ip'])
            if res.get('route'):
                put(r, 'route', res['route'])
            elif '/regions' in it['link']:
                put(r, 'route', '')  # regional plans have no route colour — keep O clean

            # ── Price change tracking ──
            if price_changed:
                diff = new_val - old_val
                pct = (diff / old_val) * 100
                arrow = '↑' if diff > 0 else '↓'
                sign = '+' if diff > 0 else '-'
                changed = f"{arrow} {sign}${abs(diff):.2f} ({sign}{abs(pct):.1f}%)"
                put(r, 'prev', old)
                put(r, 'changed', changed)
                put(r, 'last_change', datetime.now().strftime("%Y-%m-%d"))
            elif first_time:
                put(r, 'changed', "First check")
            else:
                oc = (it.get('old_changed') or "").strip()
                is_real = oc.startswith('↑') or oc.startswith('↓') or oc == "First check"
                if oc and not is_real:
                    put(r, 'changed', "")

            # ── Profitability check ──
            my_price_val = to_val(it['my_price'])
            if my_price_val and new_val:
                profit_abs = my_price_val - new_val
                profit_pct = (profit_abs / new_val) * 100
                sign = '+' if profit_abs >= 0 else '-'
                # Leading emoji keeps Sheets from parsing "+..."/"-..." as a formula,
                # and doubles as a green/red profit indicator.
                emoji = '🟢' if profit_abs >= 0 else '🔴'
                put(r, 'profit',
                    f"{emoji} {sign}${abs(profit_abs):.2f} ({sign}{abs(profit_pct):.1f}%)")

                gb_num = to_val(res['gb'].replace('gb', '')) if res['gb'] else None
                is_1gb = gb_num is not None and gb_num <= 1

                if is_1gb:
                    # 1GB: flag if loss exceeds 20%
                    if profit_pct < -20:
                        put(r, 'stock', 'לא רווחי')
                        print(f"  💸 Row {r}: 1GB unprofitable ({profit_pct:+.1f}%)")
                    else:
                        put(r, 'stock', '')
                else:
                    # All others: flag if profit below 20%
                    if profit_pct < 20:
                        put(r, 'stock', 'לא רווחי')
                        print(f"  💸 Row {r}: unprofitable ({profit_pct:+.1f}%)")
                    else:
                        put(r, 'stock', '')
            else:
                put(r, 'stock', '')

        flush(done)
        mins = (_time.time() - t0) / 60
        print(f"\n📊 Sheet updated for {done} of {len(items)} packages "
              f"at {ts} ({mins:.0f} min)")
        if skipped:
            print(f"⚠️  {skipped} packages still hold yesterday's prices.")
        else:
            print("\n✅ Done!")
        return skipped


async def main():
    scraper = ESIMScraper()
    skipped = await scraper.run()
    # The daily watchdog reads this run's conclusion. A green tick over a
    # half-checked sheet is worse than a red one: it tells the owner to stop
    # looking at the exact moment the prices went stale.
    if skipped:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
