#!/usr/bin/env python3
"""
eSIM.dog Price Scraper — Nitzan's auto-updating price table.

Reads links from the Google Sheet, scrapes esim.dog, and auto-fills:
  code, countries, GB, source, validity, buy price, route, profit, stock status
plus tracking: previous price, last updated, changed?, last change date,
Networks, Breakout IP.

Route selection: cheapest first, ties broken by quality (Blue>Pink>Black>Yellow>Green).
Stock detection: if the page shows different GB/validity than the URL requested.
Profitability: 1GB packages allow up to -20% loss; all others require >=20% profit.
Regional codes: A=mini, B=grande (e.g. 1.0A.10, 1.0B.5).
"""

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Optional, Dict, List
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

ROUTE_QUALITY = ['Blue', 'Pink', 'Black', 'Yellow', 'Green']

def route_quality_rank(name: str) -> int:
    """Lower = better. Strips emoji prefixes like '🎁'."""
    clean = re.sub(r'[^\w]', '', name).capitalize()
    for i, r in enumerate(ROUTE_QUALITY):
        if clean == r:
            return i
    return len(ROUTE_QUALITY)


def col_letter(idx: int) -> str:
    """0-based column index -> spreadsheet letter (0->A, 25->Z, 26->AA)."""
    result = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        result = chr(ord('A') + rem) + result
    return result


def strip_vpn_from_url(url: str) -> str:
    """Remove any vpn parameter so the unwanted VPN service is never auto-added."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query.pop('vpn', None)
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

    async def get_all_routes(self, page: Page) -> Dict[str, Dict]:
        """Click every route option and record price + network info."""
        routes = {}
        container = await self.find_route_container(page)
        if not container:
            return routes
        for name in await self.list_route_names(page, container):
            if await self.select_route(page, container, name):
                price = await self.extract_price(page)
                net_info = await self.extract_network_info(page)
                if price:
                    routes[name] = {
                        'price': price,
                        'network': net_info['network'],
                        'breakout_ip': net_info['breakout_ip'],
                    }
                    print(f"    {name}: {price}  |  {net_info['network'][:50]}")
        return routes

    async def extract_network_info(self, page: Page) -> Dict[str, str]:
        """
        Read the blue info box for the CURRENTLY SELECTED route:
          "Networks • LTE + 5G <carrier>"  and  "Breakout IP: <city>"
        """
        try:
            page_text = await page.inner_text("body")
        except Exception:
            return {'network': '', 'breakout_ip': ''}

        # The header line varies ("LTE + 5G China Mobile" / "LTE\nLocal network" / ...),
        # so capture everything up to the next blank line / "Breakout" and normalize.
        network = ""
        m = re.search(r'Networks?\s*•\s*(.+?)(?:\n\s*\n|\nBreakout|\Z)',
                       page_text, re.IGNORECASE | re.DOTALL)
        if m:
            content = re.sub(r'\s+', ' ', m.group(1)).strip()
            network = f"Networks • {content}"

        breakout_ip = ""
        m2 = re.search(r'Breakout IP:\s*(.+)', page_text, re.IGNORECASE)
        if m2:
            breakout_ip = m2.group(1).strip()

        return {'network': network, 'breakout_ip': breakout_ip}

    async def read_page_package_info(self, page: Page) -> Dict[str, str]:
        """Read the actual GB and validity shown on the page (for stock detection)."""
        try:
            text = await page.inner_text("body")
        except Exception:
            return {}
        result = {}
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

        routes = await self.get_all_routes(page)
        if routes:
            print(f"  📌 Routes found (default {default_price}): {len(routes)}")
            for name, rdata in routes.items():
                try:
                    candidates.append((
                        float(rdata['price'].replace('$', '')),
                        route_quality_rank(name),
                        name,
                        rdata,
                    ))
                except Exception:
                    pass
        elif default_price:
            net_info = await self.extract_network_info(page)
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
            # Sort by price first, then by route quality rank for ties
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

        # Stock detection: page shows different package than requested
        out_of_stock = False
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
            'note': "" if price else "Could not read price",
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
        clean_url = strip_vpn_from_url(url)
        if clean_url != url:
            print("  🔐 Removed VPN parameter")
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
                })
        return items, col_index

    async def run(self):
        if not self.sheet_service:
            print("⚠️  Google Sheets not configured. Cannot run.")
            return
        items, col = self.read_rows()
        if not items:
            print("ℹ️  No links found in column E.")
            return

        print(f"\n📋 Checking {len(items)} packages...\n")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        updates = []

        def put(row, key, value):
            if key not in col:
                return
            updates.append({'range': f'{col_letter(col[key])}{row}',
                            'values': [[value]]})

        def to_val(p):
            try:
                return float(str(p).replace('$', '').replace(',', ''))
            except Exception:
                return None

        for it in items:
            r = it['row']
            res = await self.scrape_confirmed(it['link'], it['variant'], it['old_price'])
            new_price = res['price']

            if new_price is None:
                put(r, 'updated', ts)
                put(r, 'changed', res['note'] or 'Check failed')
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
                put(r, 'changed', f"לא במלאי — הדף הציג {res['gb']}/{res['validity']}")
                print(f"  ❌ Row {r}: out of stock")
                continue

            # ── Update all fields ──
            if res['code']:
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

        if updates:
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
            print(f"\n📊 Sheet updated for {len(items)} packages at {ts}")
        print("\n✅ Done!")


async def main():
    scraper = ESIMScraper()
    await scraper.run()


if __name__ == "__main__":
    asyncio.run(main())
