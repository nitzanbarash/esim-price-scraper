#!/usr/bin/env python3
"""Refresh the Stellar rows of the price sheet from Stellar's catalogue.

esim.dog has no API, so its prices are read by driving its website with a
browser — 54 minutes a day, at the edge of the run's budget. Stellar is the
other way round: the whole catalogue is one JSON file, read in four seconds,
no browser, no login. This script is that read, plus the one piece of
judgment the read cannot make for us: WHICH of Stellar's listings is the
product we sell under a given SKU.

Where the price comes from
--------------------------
With STELLAR_READ_KEY in the environment (a plans:read-only key — memory:
stellar-key-placement) the catalogue is read from the wholesale API,
GET /api/v1/plans, paged at 100 a call: the price on each listing is the
one we actually pay, in EUR, and every listing carries the plan UUID an
order is placed against. Without the key the script falls back to the
public retail feed and ESTIMATES wholesale as retail / RETAIL_OVER_WHOLESALE
(1.2192, measured 2026-09-09 over 58 rows, sd 0.0041). The fallback says so
in the run log; it exists so a lost key degrades the prices, not the run.

Two things the API settles that the feed could not: `data.megabytes` is the
true size (3 GB is 3072), and `coverage.codes` is the plan's own coverage,
not its product page's. The regional rule is kept as it was — a code sold
under ANY multi-country listing is regional — the conservative reading of
both sources. A read that makes most coded rows vanish at once is treated
as a broken read and nothing is written (see sanity()).

Why the listing has to be CHOSEN
--------------------------------
A Stellar package code is a family, not a product: CKH995 is sold as
6/7/8/9/10 GB x 20/30 days across ten listings. The 20-day and the 30-day
listing usually cost the same, and the 20-day one is strictly worse. The
sheet's Stellar rows were first written from a cheapest-first read, and 19 of
them landed on the 20-day twin: they say Stellar gives fewer days than it
does, for no saving at all. This script no longer decides that for itself: the
owner's day rules live in day_policy.py and this script asks them.

    GB must equal the SKU's GB exactly.
    The validity must sit inside the window day_policy draws for that size at
    this supplier -- day_floor(gb, 'stellar') .. day_ceiling(gb, 'stellar') --
    and day_policy.pick() chooses inside it: 30 days first, given up only for
    more than 1% off; then whatever sits closest to 30; and from 30GB up, a
    Stellar plan longer than 31 days is taken when it costs no more.
    A tie between two identical listings goes to the plainer name, so a
    '(nonhkip)' twin never wins on the order the API happened to list it in.

The floor is the OWNER's product definition, not the esim.dog twin's validity:
the chooser now compares the two suppliers, so a row must not inherit its
window from the row it is competing with.

Two traps in the feed, both handled here and pinned by test_stellar_prices.py:
  * `variant.data_gb` is ROUNDED UP — a 750MB plan says 1. `meta.data_gb`
    holds the true size and is the only size field read.
  * `meta.coverage_codes` echoes the product page, not the plan. The feed says
    Taiwan's plans cover TW, but their code P32JCTKR0 is also listed under the
    12-area Asia product — it IS that regional. A code is regional if it is
    sold under any regional product, and a regional code under a country SKU
    is refused, never priced as the country.

What this script owns on a Stellar row
--------------------------------------
Only the cells the catalogue answers: days, buy price, the bookkeeping beside
it (previous price / updated / changed / last change), and the stock column —
that one only while it holds one of this script's own markers. A value the
owner typed there is never cleared. The Networks cell is written from the
API listing's coverage (operators • generations), and column X holds the plan
UUID of the listing that price came from — the buyer fetches that one plan
instead of the catalogue. SKU, country, GB, code and breakout are read, not
written. A row with no code ('—') is left alone.

Run:
    python stellar_prices.py              dry run — prints the plan, writes nothing
    python stellar_prices.py --apply      writes to the sheet
    python stellar_prices.py --feed F     read a saved retail feed instead of fetching
    python stellar_prices.py --plans F    read a saved wholesale-API dump instead of fetching
    python stellar_prices.py --inspect TH,US   every raw wholesale listing of those
                                          countries and why each is used or not;
                                          no sheet (stellar-inspect.yml runs it)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

import day_policy
from choose_supplier import PROFIT_MARKS, usd_outside_parens
from esim_price_scraper import HEADER_KEYS, SHEET_ID, col_letter

FEED_URL = "https://stellarsecurity.com/assets/esim/products.index.json"   # retail; fallback only
API_URL = "https://wholesale.stellarsecurity.com/api/v1/plans"
API_PER_PAGE = 100            # the API's maximum
# A runaway guard, NOT a guess at the catalogue's size. It was 60 (6,000
# listings) until 2026-10-02, when the catalogue grew past exactly that: the
# read stopped at 6,000 without a word, and since the API sorts by destination,
# every country after Singapore read as GONE. See fetch_plans.
API_MAX_PAGES = 300
API_CALLS_PER_MINUTE = 55     # Stellar allows 60 a minute per IP (X-RateLimit-Limit)
API_RETRY_WAIT = 61.0         # a 429 that names no better time
FX_URL = "https://api.frankfurter.app/latest"
# Measured 2026-09-09: 58 rows, median 1.2192, stdev 0.0041. Re-measure the
# day the wholesale API is wired in — a drift here mis-prices every row.
RETAIL_OVER_WHOLESALE = 1.2192
FX_FALLBACK = 1.1614          # frankfurter, 2026-09-08 — last resort only
SOURCE = "stellar"            # column D, compared lower-case

# Column X holds the plan UUID of the listing this script priced, so the buyer
# can fetch that ONE plan (GET /plans/{id}) instead of paging the whole
# catalogue before a paid order. The header is not in HEADER_KEYS: the scraper
# neither reads nor writes it, and it is this script's alone. X1 and Y1 were
# empty when the column was taken (2026-09-13); Z1 onwards are the owner's
# reference blocks, and nothing here ever writes past X.
PLAN_ID_HEADER = "Stellar plan_id"
PLAN_ID_COL = 23              # X, 0-based
FEED_STALE_HOURS = 12         # the feed is a snapshot; say so when it is old

# Column Q means "out of stock when non-empty". These are the values THIS
# script writes there. It clears the cell only when it finds one of them, so
# a word the owner typed is never overwritten.
MARK_GONE = "לא זמין"                 # the code disappeared from the catalogue
MARK_SHORT = "פחות ימים מהמובטח"      # nothing at this GB with enough days
MARK_REGIONAL = "אזורי"               # a regional code under a country SKU
OUR_MARKS = frozenset({MARK_GONE, MARK_SHORT, MARK_REGIONAL})

# MB-named listings (750MB, 500MB) are kept so the family is whole; their
# size still comes from meta.data_gb, never from the name or the ceil'd field.
_SKU_RE = re.compile(r"^ESIM-(.+?)-([\d.]+)(?:GB|MB)-(\d+)D-([A-Z0-9]+)$")


# ── catalogue ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Variant:
    code: str          # package code — the family, from the SKU's tail
    gb: float          # meta.data_gb: the TRUE size (top-level data_gb is ceil'd)
    days: int
    wholesale_eur: float   # what WE pay: the API's price, or retail / 1.2192 from the feed
    slug: str          # the product the listing sat under
    name: str
    plan_id: str = ""  # the API's UUID — what an order is placed against; '' from the feed
    networks: str = ""  # 'Vodafone/Wind • 5G' from coverage.networks; '' from the feed


def networks_label(coverage: dict) -> str:
    """'Operator/Operator • 4G + 5G' out of a listing's coverage.networks —
    the one format the sheet's Networks column, the order page and the
    receipts row all use. '' when the listing names none."""
    nets = (coverage or {}).get("networks") or []
    ops = sorted({str(n.get("operator") or "") for n in nets if n.get("operator")})
    gens = sorted({str(n.get("network") or "") for n in nets if n.get("network")})
    label = "/".join(ops)
    if gens:
        label = f"{label} • {' + '.join(gens)}" if label else " + ".join(gens)
    return label


class Catalogue:
    def __init__(self, variants, regional_codes, generated_at: str = ""):
        self.variants = list(variants)
        self.regional_codes = set(regional_codes)
        self.generated_at = generated_at
        self.by_code: dict[str, list[Variant]] = {}
        for v in self.variants:
            self.by_code.setdefault(v.code, []).append(v)

    @classmethod
    def from_feed(cls, feed: dict) -> "Catalogue":
        variants, regional = [], set()
        for product in feed.get("data", []):
            slug = product.get("slug", "")
            pmeta = product.get("meta") or {}
            # A product covering more than one area is a regional. Its codes
            # are what make a country-named listing regional too.
            is_regional = len(pmeta.get("coverage_codes") or []) > 1
            for v in product.get("variants", []):
                meta = v.get("meta") or {}
                if meta.get("data_type") != "Data in Total":
                    continue          # per-day unlimited plans are another product
                if not v.get("active", True):
                    continue
                m = _SKU_RE.match(str(v.get("sku", "")).upper())
                if not m:
                    continue
                gb, days, cents = meta.get("data_gb"), v.get("duration_days"), v.get("unit_price_cents")
                if gb is None or days is None or cents is None:
                    continue
                code = m.group(4)
                variants.append(Variant(code, float(gb), int(days),
                                        round(cents / 100.0 / RETAIL_OVER_WHOLESALE, 2),
                                        slug, str(v.get("name", ""))))
                if is_regional:
                    regional.add(code)
        return cls(variants, regional, str(feed.get("snapshot_generated_at", "")))

    @classmethod
    def from_api(cls, plans: list) -> "Catalogue":
        """The wholesale API's listings (GET /plans, every page). The price is
        what we pay. Daily-unlimited plans are billed per day and their SKU
        reads 3GBD-1D, so both the billing unit and the SKU shape drop them."""
        variants, regional, synced = [], set(), ""
        for p in plans:
            if drop_reason(p):
                continue
            price = p.get("price") or {}
            m = _SKU_RE.match(str(p.get("sku") or "").upper())
            mb, days, cents = (p.get("data") or {}).get("megabytes"), p.get("validity_days"), price.get("amount_cents")
            code = m.group(4)
            variants.append(Variant(code, _gb_from_mb(int(mb)), int(days), cents / 100.0,
                                    str(p.get("product_slug") or ""), str(p.get("name", "")),
                                    str(p.get("id", "")), networks_label(p.get("coverage") or {})))
            if len((p.get("coverage") or {}).get("codes") or []) > 1:
                regional.add(code)
            synced = max(synced, str(p.get("catalogue_synced_at") or ""))
        return cls(variants, regional, synced)

    def age_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        if not self.generated_at:
            return None
        try:
            gen = datetime.fromisoformat(self.generated_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - gen).total_seconds() / 3600


def drop_reason(p: dict) -> str:
    """Why from_api leaves this raw API listing out, or '' when it keeps it.

    from_api asks this and nothing else, so the explanation printed for a
    'GONE' row can never drift from the filter that made it gone. 'GONE' used
    to be one word for four different things -- Stellar removed the plan,
    Stellar switched it off, Stellar changed the SKU spelling, or the plan
    became a per-day product -- and each needs a different answer from us.
    """
    price = p.get("price") or {}
    if price.get("billing_unit", "plan") != "plan":
        return "billed per day"          # daily-unlimited: another product
    if (p.get("duration") or {}).get("configurable"):
        return "configurable duration"
    if not p.get("available", True):
        return "available=false"
    if not _SKU_RE.match(str(p.get("sku") or "").upper()):
        return f"SKU not in the ESIM-…-<GB>-<days>D-<code> format ({p.get('sku')!r})"
    if ((p.get("data") or {}).get("megabytes") is None or p.get("validity_days") is None
            or price.get("amount_cents") is None):
        return "no size, days or price"
    return ""


def _listing_text(p: dict) -> str:
    """Everything a code can hide in: the SKU, the slugs, the name."""
    return " ".join(str(p.get(k) or "") for k in ("sku", "slug", "product_slug", "name")).upper()


def explain_gone(plans: list, codes) -> list[str]:
    """One line per vanished package code: what the RAW catalogue still says
    about it. Read-only -- it changes no decision, it only says which of the
    four 'GONE's this one is."""
    out = []
    for code in sorted(set(codes)):
        hits = [p for p in plans if code in _listing_text(p)]
        if not hits:
            out.append(f"  {code:<11} not in the raw catalogue at all — removed (or renamed past recognition)")
            continue
        why: dict[str, int] = {}
        for p in hits:
            r = drop_reason(p)
            if not r:
                m = _SKU_RE.match(str(p.get("sku") or "").upper())
                r = f"kept, but under code {m.group(4)}" if m else "kept"
            why[r] = why.get(r, 0) + 1
        sample = hits[0]
        out.append(f"  {code:<11} {len(hits)} raw listing(s): "
                   + "; ".join(f"{r} ×{n}" for r, n in sorted(why.items()))
                   + f"  — e.g. {sample.get('sku')!r} / {sample.get('name')!r}")
    return out


def drop_summary(plans: list) -> str:
    """'kept 5816 | available=false 120 | …' over the whole raw read."""
    tally: dict[str, int] = {}
    for p in plans:
        r = drop_reason(p)
        r = "kept" if not r else ("SKU not in our format" if r.startswith("SKU") else r)
        tally[r] = tally.get(r, 0) + 1
    return " | ".join(f"{r} {n}" for r, n in sorted(tally.items(), key=lambda kv: -kv[1]))


def inspect_lines(plans: list, countries) -> list[str]:
    """Every raw listing that covers exactly one of these ISO countries, with
    the verdict from_api gives it. For looking, not deciding: --inspect."""
    want = {c.strip().upper() for c in countries if c.strip()}
    rows = []
    for p in plans:
        codes = [str(c).upper() for c in ((p.get("coverage") or {}).get("codes") or [])]
        cc = codes[0] if len(codes) == 1 else ""
        if cc not in want:
            continue
        mb = (p.get("data") or {}).get("megabytes") or 0
        cents = (p.get("price") or {}).get("amount_cents")
        rows.append((cc, mb, p.get("validity_days") or 0, cents if cents is not None else -1, p))
    out = []
    for cc in sorted(want):
        mine = sorted((r for r in rows if r[0] == cc), key=lambda r: r[1:4])
        kept = sum(1 for r in mine if not drop_reason(r[4]))
        out.append(f"\n## {cc}: {len(mine)} single-country listings, {kept} usable by stellar_prices")
        for _, mb, days, cents, p in mine:
            size = f"{_gb_from_mb(int(mb)):g}GB" if mb else "?GB"
            eur = f"€{cents / 100:.2f}" if cents >= 0 else "€?"
            bo = (p.get("coverage") or {}).get("breakout_ip_country_code") or "?"
            out.append(f"  {size:>6} {days:>3}d {eur:>8}  {str(p.get('sku')):<38} "
                       f"{drop_reason(p) or 'ok':<24} bo={bo:<3} {p.get('name')}")
    return out


def _gb_from_mb(mb: int) -> float:
    """Stellar counts 1 GB as 1024 MB (3 GB is 3072). Sub-GB plans are named in
    round decimal sizes and the sheet says 0.75 for 750MB, so a size that is
    not a clean binary multiple is read as decimal — 750 and 768 both land on
    0.75, 500 and 512 on 0.5."""
    return round(mb / 1024, 3) if mb % 256 == 0 else round(mb / 1000, 3)


def fetch_feed() -> dict:
    r = requests.get(FEED_URL, timeout=60)
    r.raise_for_status()
    return r.json()


class CatalogueShortRead(RuntimeError):
    """The read ended before the catalogue did. Never a smaller catalogue: a
    caller that prices or buys off a partial read turns every missing listing
    into a 'GONE' row or a refused order."""


def _retry_after(resp) -> float:
    h = getattr(resp, "headers", None) or {}
    for name in ("Retry-After", "X-RateLimit-Reset"):
        try:
            v = float(h.get(name))
        except (TypeError, ValueError):
            continue
        if v > 1e9:                       # an epoch, not a delay
            v -= time.time()
        if 0 < v <= 300:
            return v + 0.5
    return API_RETRY_WAIT


def fetch_plans(key: str, session=None, sleep=time.sleep, clock=time.monotonic) -> list:
    """Every listing of the wholesale catalogue -- ALL of it, or CatalogueShortRead.

    2026-10-02: Stellar's catalogue grew to 6,000+ listings and the old 60-page
    cap read exactly 6,000 and stopped as if that were the end. The API sorts
    by destination, so every country after Singapore -- South Korea, Spain,
    Switzerland, Thailand, the UK, the US, Vietnam -- vanished from the read:
    41 rows were marked GONE and the chooser moved them to esim.dog at up to
    2.5x the cost, while every step reported success. So the end of the read is
    now the API's own last_page, and a read that ends short of meta.total is
    an error, not a catalogue.

    Pages are paced to API_CALLS_PER_MINUTE in any rolling minute (Stellar
    allows 60): the first ~55 go out a quarter-second apart as before, and only
    a catalogue past that waits for the window. A 429 that slips through waits
    for the time Stellar names and asks once more.
    """
    s = session or requests.Session()
    s.headers.update({"Authorization": f"Bearer {key}", "Accept": "application/json"})
    sent: list[float] = []

    def get(page):
        # Wait for the OLDEST call to leave the minute, not for the whole
        # window to empty: the other 54 are still inside it, and forgetting
        # them sends 55 more at once.
        while True:
            now = clock()
            while sent and now - sent[0] >= 60.0:
                sent.pop(0)
            if len(sent) < API_CALLS_PER_MINUTE:
                break
            sleep(60.0 - (now - sent[0]) + 0.05)
        sent.append(clock())
        return s.get(API_URL, params={"per_page": API_PER_PAGE, "page": page}, timeout=60)

    out, page, total = [], 1, None
    while True:
        if page > API_MAX_PAGES:
            raise CatalogueShortRead(f"the catalogue runs past {API_MAX_PAGES} pages "
                                     f"({len(out)} listings read) -- raise API_MAX_PAGES")
        r = get(page)
        if getattr(r, "status_code", 0) == 429:
            sleep(_retry_after(r))
            sent.clear()
            r = get(page)
        r.raise_for_status()
        body = r.json()
        meta = body.get("meta") or {}
        out.extend(body.get("data") or [])
        if meta.get("total") is not None:
            total = int(meta["total"])
        if page >= int(meta.get("last_page") or page):
            break
        page += 1
        sleep(0.25)
    if total is not None and len(out) < total:
        raise CatalogueShortRead(f"read {len(out)} of the {total} listings the API says it has "
                                 f"-- a partial catalogue would mark the rest GONE")
    return out


# ── the sheet ───────────────────────────────────────────────────────────────

@dataclass
class StellarRow:
    row: int
    sku: str
    country: str
    code: str                   # column 'Route' holds Stellar's package code
    gb: Optional[float]
    days: Optional[int]
    eur: Optional[float]        # parsed out of '(€2.21) $2.57'
    price_cell: str
    changed: str
    stock: str
    networks: str = ""                 # the Networks cell as it stands
    plan_id: str = ""                  # column X: the UUID of the listing we priced

    @property
    def floor_days(self) -> int:
        """Shortest validity the owner sells this SIZE as (day_policy's bands).

        It used to be the esim.dog twin's days -- what the site promises today.
        That let one supplier's catalogue define the other's window, and the
        chooser now compares the two: the bands belong to the size, not to the
        row we are competing with.
        """
        return day_policy.day_floor(self.gb, SOURCE) if self.gb is not None else 0

    @property
    def ceiling_days(self):
        """Longest validity worth asking for; None = unbounded (30GB+)."""
        return day_policy.day_ceiling(self.gb, SOURCE) if self.gb is not None else None

    @property
    def window_text(self) -> str:
        """'25-31' / '33+' -- the window, for a note that has to name it."""
        ceil = self.ceiling_days
        return f"{self.floor_days}-{ceil}" if ceil else f"{self.floor_days}+"


def _num(s) -> Optional[float]:
    m = re.search(r"[\d.]+", str(s or "").replace(",", ""))
    return float(m.group()) if m else None


def _eur(cell) -> Optional[float]:
    m = re.search(r"€\s*([\d.]+)", str(cell or ""))
    return float(m.group(1)) if m else None


def is_regional_sku(sku: str) -> bool:
    """The sheet's own legend: a second segment starting with 0 is regional
    (2.0B.10 is Stellar's 35-area Europe; 2.49.10 is Germany)."""
    parts = sku.split(".")
    return len(parts) >= 2 and parts[1].startswith("0")


REQUIRED = ("code", "countries", "gb", "source", "validity", "price", "prev",
            "updated", "changed", "last_change", "route", "stock")


def read_rows(values: list[list[str]]) -> tuple[list[StellarRow], dict[str, int]]:
    """Every Stellar row of the price sheet, as read.

    Columns are found by header text through the scraper's own HEADER_KEYS,
    so the two scripts can never disagree about which column is which.
    """
    if not values:
        return [], {}
    header = [str(h).strip() for h in values[0]]
    col = {k: header.index(h) for k, h in HEADER_KEYS.items() if h in header}
    missing = [HEADER_KEYS[k] for k in REQUIRED if k not in col]
    if missing:
        raise SystemExit(f"price sheet header missing: {missing}")
    # The plan_id column is ours by header text wherever the owner moved it to,
    # and otherwise column X -- but only while X1 is still empty. A word
    # standing in X1 that is not our header belongs to someone else: the column
    # is then simply not available, and every plan_id write is dropped by
    # write_updates rather than landing on top of it.
    if PLAN_ID_HEADER in header:
        col["plan_id"] = header.index(PLAN_ID_HEADER)
    elif len(header) <= PLAN_ID_COL or not header[PLAN_ID_COL].strip():
        col["plan_id"] = PLAN_ID_COL
    width = max(col.values()) + 1

    stellar: list[StellarRow] = []
    for idx, raw in enumerate(values[1:], start=2):
        r = [str(x) for x in raw] + [""] * (width - len(raw))
        sku = r[col["code"]].strip()
        if not sku or "." not in sku:
            continue
        src = r[col["source"]].strip().lower()
        days = _num(r[col["validity"]])
        # The esim.dog twin is another supplier's row, not this row's window:
        # the day bands come from day_policy (see StellarRow.floor_days).
        if src != SOURCE:
            continue
        price = r[col["price"]].strip()
        stellar.append(StellarRow(
            row=idx, sku=sku, country=r[col["countries"]].strip(),
            code=r[col["route"]].strip(), gb=_num(r[col["gb"]]),
            days=int(days) if days else None, eur=_eur(price), price_cell=price,
            changed=r[col["changed"]].strip(), stock=r[col["stock"]].strip(),
            networks=r[col["network"]].strip() if "network" in col else "",
            plan_id=r[col["plan_id"]].strip() if "plan_id" in col else ""))
    return stellar, col


# ── the decision ────────────────────────────────────────────────────────────

@dataclass
class Decision:
    row: StellarRow
    pick: Optional[Variant]
    reason: str = ""            # "" | "gone" | "short" | "regional"
    have_days: tuple = ()
    paid_up: float = 0.0        # EUR paid over the cheapest, to hold the longer plan
    won_days: int = 0           # days the cheapest listing would have cost us


def decide(cat: Catalogue, row: StellarRow, min_days: Optional[int] = None) -> Optional[Decision]:
    """None means the row is not ours to touch (an owner's '—' note).

    `min_days` is a promise made OUTSIDE the catalogue — the validity a paying
    customer was already shown (stellar_buyer passes the order token's `d`).
    It can only RAISE the floor day_policy drew for the size, never lower it:
    the effective floor is max(band floor, min_days). The ceiling is untouched,
    so a promise longer than the size's ceiling empties the window and the row
    comes back reason='short' — the same answer as a size with nothing long
    enough in it, and the right one: refusing to buy is cheaper than buying a
    customer fewer days than he paid for.
    """
    if not row.code or row.gb is None:
        return None
    if row.code in cat.regional_codes and not is_regional_sku(row.sku):
        return Decision(row, None, "regional")
    family = cat.by_code.get(row.code)
    if not family:
        return Decision(row, None, "gone")
    same_gb = [v for v in family if abs(v.gb - row.gb) < 1e-9]
    # Inside one package code the listings are the SAME product cut at different
    # (GB, days), so a cent between the 20-day and the 30-day twin is a pricing
    # artifact, not a difference in what the buyer receives -- which is why the
    # owner's bands, not the price, draw the window. day_policy.pick() sorts
    # stably and keeps the incumbent whenever it cannot separate two listings,
    # so handing it the plainer name FIRST is how that tie is broken: a
    # '(nonhkip)' twin never wins on the order the API happened to list it in.
    plain_first = sorted(same_gb, key=lambda v: (len(v.name), v.name))
    window = day_policy.in_window_candidates(
        row.gb, SOURCE, [(v.days, v.wholesale_eur, v) for v in plain_first])
    floor = max(day_policy.day_floor(row.gb, SOURCE), int(min_days or 0))
    # A no-op unless a promise raised the floor; pick() re-checks the band
    # window itself, and every survivor here already satisfies both.
    window = [c for c in window if c[0] >= floor]
    if not window:
        return Decision(row, None, "short", tuple(sorted({v.days for v in same_gb})))
    best = day_policy.pick(row.gb, SOURCE, window)[2]
    # Not a rule any more, just the arithmetic the dry run prints: what a
    # cheapest-first read would have bought, and what the owner's validity cost.
    thrift = min(window, key=lambda c: (c[1], -c[0]))
    return Decision(row, best, paid_up=round(best.wholesale_eur - thrift[1], 4),
                    won_days=best.days - thrift[0])


def sanity(decisions) -> str:
    """A read that makes MOST coded rows vanish at once is a broken read (an
    empty page, a renamed SKU format, a revoked key answering 200 with nothing)
    — not a supplier that dropped half its catalogue overnight. The reason to
    stop, or '' to go on."""
    coded = [d for d in decisions if d.reason != "regional"]
    gone = sum(1 for d in coded if d.reason == "gone")
    if len(coded) >= 4 and gone * 2 > len(coded):
        return (f"{gone} of {len(coded)} package codes vanished at once — that is a broken "
                f"catalogue read, not a catalogue; nothing written")
    return ""


# ── money ───────────────────────────────────────────────────────────────────

def price_cell(eur: float, fx: float) -> str:
    """'(€2.21) $2.57' — euro first, dollar last, as the owner asked on
    2026-09-10.

    Column G carries a LEFT_TO_RIGHT text direction so this reads as written
    inside the RTL sheet — keep the format and the cell format in step (see
    memory: rtl-sheet-price-direction). The euro is the number the owner
    checks, so it now opens the cell instead of trailing it.

    Nothing downstream reads the ORDER: every reader takes the dollars from
    outside the parentheses (choose_supplier.usd_outside_parens), which is why
    the sheet can hold both spellings while it migrates."""
    return f"(€{eur:.2f}) ${eur * fx:.2f}"


def _frankfurter() -> Optional[float]:
    r = requests.get(FX_URL, params={"from": "EUR", "to": "USD"}, timeout=15)
    if r.status_code == 200:
        return float(r.json()["rates"]["USD"])
    return None


def fetch_fx(existing_cells, fetch: Callable[[], Optional[float]] = _frankfurter) -> tuple[float, str]:
    """EUR->USD, and where it came from.

    Live rate first. If that fails, the sheet's own price cells hold the rate
    that was used last time (dollars / euros), and staying consistent with
    yesterday beats a constant that goes stale for months. The constant is the
    last resort.
    """
    try:
        rate = fetch()
        if rate:
            return rate, "frankfurter"
    except Exception:            # noqa: BLE001 — any failure means fall back
        pass
    ratios = []
    for cell in existing_cells:
        # Both spellings of the pair are in the sheet while it migrates, so the
        # dollars are taken from outside the parentheses and the euros from
        # anywhere — position says nothing about which number is which.
        usd = usd_outside_parens(cell)
        eur = _eur(cell)
        if usd and eur and eur > 0:
            ratios.append(usd / eur)
    if ratios:
        return round(statistics.median(ratios), 4), "derived from the sheet's own cells"
    return FX_FALLBACK, "hard-coded fallback"


# ── what to write ───────────────────────────────────────────────────────────

def _real_note(note: str) -> bool:
    # Mirrors the scraper: a price-change note stays on the row until the next
    # change; anything else ('↔', an error text) is cleared once resolved.
    return note.startswith(("↑", "↓")) or note == "First check"


def header_update(values, col: dict[str, int]) -> list[tuple[int, str, str]]:
    """The one row-1 cell this script may write: the plan_id header, X1.

    It is written only when the column read_rows claimed is still unlabelled,
    and only that single cell — Y1 onwards, where the owner's reference blocks
    start, are never touched.
    """
    if "plan_id" not in col:
        return []
    header = [str(h).strip() for h in (values[0] if values else [])]
    label = header[col["plan_id"]] if col["plan_id"] < len(header) else ""
    return [] if label == PLAN_ID_HEADER else [(1, "plan_id", PLAN_ID_HEADER)]


def plan_updates(decisions, fx: float, ts: str, today: str) -> list[tuple[int, str, str]]:
    """(row, HEADER_KEYS key, value) — the whole write, before any of it happens."""
    out: list[tuple[int, str, str]] = []

    def put(row, key, val):
        out.append((row, key, val))

    for d in decisions:
        r = d.row
        put(r.row, "updated", ts)

        if d.pick is None:
            if d.reason == "gone":
                note, mark = "הקוד נעלם מהקטלוג של Stellar", MARK_GONE
            elif d.reason == "short":
                have = ", ".join(f"{x}d" for x in d.have_days) or "כלום"
                note = f"אין {r.gb:g}GB ל-{r.window_text} ימים בקוד {r.code} (יש: {have})"
                mark = MARK_SHORT
            else:
                note, mark = "קוד אזורי תחת שם מדינה — לא הושווה", MARK_REGIONAL
            put(r.row, "changed", note)
            # Nothing was priced, so there is no listing to buy: the id must go
            # with the price it belonged to, or the buyer's fast path would
            # fetch last week's plan for a row we refused today.
            if r.plan_id:
                put(r.row, "plan_id", "")
            # Over an empty cell, our own marker, or a MARGIN word — never over
            # the owner's word. The margin words (choose_supplier.PROFIT_MARKS)
            # are derived from the price and re-judged by the chooser every
            # run; a plan that is gone has no price to judge, and "cannot be
            # bought" outranks "does not pay". Left in place, the margin word
            # kept a vanished plan looking buyable at its frozen price.
            if (r.stock == "" or r.stock in OUR_MARKS or r.stock in PROFIT_MARKS) \
                    and r.stock != mark:
                put(r.row, "stock", mark)
            continue

        v = d.pick
        new_eur = v.wholesale_eur
        put(r.row, "price", price_cell(new_eur, fx))
        # The UUID of the listing this very price came from, so stellar_buyer
        # can fetch that one plan instead of the whole catalogue. A retail-feed
        # run carries no ids and therefore blanks the cell; that is the honest
        # answer — the buyer treats a missing id as "no fast path" and reads the
        # catalogue, which is what it did before this column existed.
        if v.plan_id != r.plan_id:
            put(r.row, "plan_id", v.plan_id)
        # The Networks cell comes from the listing's coverage — never typed by
        # hand (127 Stellar rows had none on 2026-09-10). The feed carries no
        # networks, so a feed run leaves the cell as it is.
        if v.networks and v.networks != r.networks:
            put(r.row, "network", v.networks)

        notes = []
        if r.eur is None:
            notes.append("First check")
        elif abs(new_eur - r.eur) > 0.001:
            diff = new_eur - r.eur
            pct = diff / r.eur * 100
            arrow, sign = ("↑", "+") if diff > 0 else ("↓", "-")
            notes.append(f"{arrow} {sign}€{abs(diff):.2f} ({sign}{abs(pct):.1f}%)")
            put(r.row, "prev", r.price_cell)
        if r.days != v.days:
            put(r.row, "validity", f"{v.days}d")
            notes.append(f"↔ {r.days}d → {v.days}d" if r.days else f"↔ {v.days}d")

        if notes:
            put(r.row, "changed", " | ".join(notes))
            if any(n[0] in "↑↓↔" for n in notes):
                put(r.row, "last_change", today)
        elif r.changed and not _real_note(r.changed):
            put(r.row, "changed", "")
        if r.stock in OUR_MARKS:
            put(r.row, "stock", "")
    return out


# ── Sheets I/O ──────────────────────────────────────────────────────────────

def sheets_service(cred_path: str):
    """Credentials from the environment in the cloud, from a file on a desktop.

    Same order as the scraper's setup_google_sheets: GitHub Actions has no
    credentials.json to read, only the GOOGLE_CREDENTIALS_JSON secret.
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    env = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if env:
        creds = Credentials.from_service_account_info(json.loads(env), scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(cred_path, scopes=scopes)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_sheet(svc) -> list[list[str]]:
    return svc.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="A1:Z").execute().get("values", [])


def stamp_price_direction(svc, col: dict[str, int]) -> int:
    """Make the money columns render left-to-right; report how many needed it.

    The sheet is right-to-left, so a cell that does not say otherwise renders
    its two currencies in the reverse of the order they were written in. The
    string is not the price; the string PLUS this format is. Since 2026-09-10
    the string is euro-first, '(€2.21) $2.57', and this stamp is what makes it
    reach the owner's eye that way round instead of flipped back to dollars.

    Both money columns get it, not just the buy price: on a price move the OLD
    two-currency string is copied verbatim into 'מחיר קודם', so a column that
    was never stamped shows yesterday's price backwards while today's reads
    correctly. It runs over the whole of each column so rows added later are
    covered, and it runs BEFORE the values are written, because a value that
    lands in an unstamped cell is a wrong number until the second call returns.
    """
    targets = [col[k] for k in ("price", "prev") if k in col]
    sheet_id, rows = _grid(svc)
    before = sum(_unstamped(svc, c, rows) for c in targets)
    svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [{
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": rows,
                      "startColumnIndex": c, "endColumnIndex": c + 1},
            "cell": {"userEnteredFormat": {"textDirection": "LEFT_TO_RIGHT",
                                           "horizontalAlignment": "RIGHT"}},
            "fields": ("userEnteredFormat.textDirection,"
                       "userEnteredFormat.horizontalAlignment"),
        }} for c in targets]}).execute(num_retries=3)
    return before


def _grid(svc) -> tuple[int, int]:
    """(sheetId, rowCount) of the first tab — the one read_sheet's bare A1:Z hits."""
    sheet = svc.spreadsheets().get(
        spreadsheetId=SHEET_ID,
        fields="sheets(properties(sheetId,gridProperties(rowCount)))",
    ).execute()["sheets"][0]["properties"]
    return sheet["sheetId"], sheet["gridProperties"]["rowCount"]


def _unstamped(svc, g: int, rows: int) -> int:
    """How many two-currency cells in this column render backwards right now."""
    data = svc.spreadsheets().get(
        spreadsheetId=SHEET_ID, includeGridData=True,
        ranges=[f"{col_letter(g)}2:{col_letter(g)}{rows}"],
        fields="sheets(data(rowData(values(userEnteredValue,"
               "userEnteredFormat/textDirection))))",
    ).execute()["sheets"][0]["data"][0].get("rowData", [])
    n = 0
    for rd in data:
        v = (rd.get("values") or [{}])[0]
        text = (v.get("userEnteredValue") or {}).get("stringValue", "")
        if text.strip() and (v.get("userEnteredFormat") or {}).get(
                "textDirection") != "LEFT_TO_RIGHT":
            n += 1
    return n


def write_updates(svc, col: dict[str, int], updates) -> int:
    data = [{"range": f"{col_letter(col[k])}{row}", "values": [[v]]}
            for row, k, v in updates if k in col]
    # RAW: nothing written here is a formula or a number for Sheets to
    # interpret, and RAW is the one mode that cannot turn '(€2.21) $2.57'
    # into something else.
    for i in range(0, len(data), 200):
        svc.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"data": data[i:i + 200], "valueInputOption": "RAW"},
        ).execute(num_retries=3)
    return len(data)


# ── CLI ─────────────────────────────────────────────────────────────────────

def _describe(d: Decision, fx: float) -> str:
    r = d.row
    head = f"row {r.row:<4}{r.sku:<9}{r.country:<12}{r.code:<11}"
    if d.pick is None:
        why = {"gone": "GONE from catalogue", "regional": "REGIONAL code under a country SKU",
               "short": f"no {r.gb:g}GB at {r.window_text}d (has {', '.join(str(x) for x in d.have_days) or 'none'})"}[d.reason]
        return f"{head} ✗ {why}"
    v = d.pick
    old = f"€{r.eur:.2f}" if r.eur is not None else "—"
    days = f"{r.days}d" if r.days == v.days else f"{r.days}d→{v.days}d"
    mark = " " if r.eur is not None and abs(v.wholesale_eur - r.eur) < 0.001 else "$"
    held = f"   +{d.won_days}d for +{d.paid_up * 100:.0f}c" if d.won_days > 0 else ""
    return (f"{head} {mark} {old} → €{v.wholesale_eur:.2f}  {days:<9} "
            f"{price_cell(v.wholesale_eur, fx)}{held}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:            # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write to the sheet (default: dry run)")
    ap.add_argument("--allow-estimate", action="store_true",
                    help="write even when the prices are the retail-feed estimate (normally refused)")
    ap.add_argument("--feed", help="read a saved products.index.json (retail feed) instead of fetching")
    ap.add_argument("--plans", help="read a saved wholesale-API dump (JSON list of plans) instead of fetching")
    ap.add_argument("--inspect", metavar="CC,CC",
                    help="print every raw wholesale listing of these ISO countries and exit "
                         "(needs STELLAR_READ_KEY or --plans; never touches the sheet)")
    ap.add_argument("--credentials",
                    default=os.environ.get("SHEETS_CREDENTIALS",
                                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")))
    a = ap.parse_args(argv)

    key = os.environ.get("STELLAR_READ_KEY", "").strip()

    # The retail feed does not hold our cost: it holds the shop price, divided
    # by a ratio MEASURED once over 58 rows. That is fine to LOOK at and wrong
    # to write, because nothing downstream can tell an estimate from a price —
    # stellar_buyer reads the sheet's euros back as the real wholesale figure
    # and refuses a purchase whose listing costs more than it (PRICE_RISE_TOL).
    # An estimate a few cents low therefore does not just mis-report a margin,
    # it stops sales. CI always has STELLAR_READ_KEY (memory: stellar-key-
    # placement), so a run that reaches here without one is a rotated or
    # dropped secret — which must fail loudly, not quietly write guesses.
    estimate = bool(a.feed) or not (a.plans or key)
    if a.apply and estimate and not a.allow_estimate:
        why = (f"a saved RETAIL feed ({a.feed})" if a.feed
               else "the public RETAIL feed — STELLAR_READ_KEY is not set")
        print(f"🛑 --apply refused: the prices would come from {why}, so every euro "
              f"written would be retail / {RETAIL_OVER_WHOLESALE} — an ESTIMATE, not our cost.\n"
              f"   The buyer trusts these cells as the real wholesale price, so a wrong one "
              f"refuses purchases.\n"
              f"   Restore STELLAR_READ_KEY (or pass --plans), or re-run with --allow-estimate "
              f"if the estimate really is what you want written.")
        return 3

    plans = None                 # the RAW API listings, kept for the explanations
    if a.plans:
        with open(a.plans, encoding="utf-8") as f:
            dump = json.load(f)
        plans = dump if isinstance(dump, list) else dump.get("data") or []
        cat = Catalogue.from_api(plans)
        source = f"saved API dump {a.plans}"
    elif a.feed:
        with open(a.feed, encoding="utf-8") as f:
            cat = Catalogue.from_feed(json.load(f))
        source = f"saved retail feed {a.feed}"
    elif key:
        plans = fetch_plans(key)
        cat, source = Catalogue.from_api(plans), "wholesale API — real cost"
    elif a.inspect:
        print("🛑 --inspect reads the wholesale API: set STELLAR_READ_KEY or pass --plans")
        return 2
    else:
        cat = Catalogue.from_feed(fetch_feed())
        source = f"public RETAIL feed / {RETAIL_OVER_WHOLESALE} — an ESTIMATE, no STELLAR_READ_KEY"
    age = cat.age_hours()
    print(f"📦 Stellar catalogue via {source}: {len(cat.variants)} fixed-data listings, "
          f"{len(cat.by_code)} package codes, {len(cat.regional_codes)} regional codes; "
          f"snapshot {cat.generated_at or '?'}"
          + (f" ({age:.1f}h old)" if age is not None else ""))
    if age is not None and age > FEED_STALE_HOURS:
        print(f"⚠️  feed snapshot is {age:.0f} hours old — prices below may already be stale")
    if plans is not None:
        print(f"🧮 {len(plans)} raw listings: {drop_summary(plans)}")
    if a.inspect:
        for line in inspect_lines(plans or [], a.inspect.split(",")):
            print(line)
        return 0

    svc = sheets_service(a.credentials)
    values = read_sheet(svc)
    rows, col = read_rows(values)
    fx, fx_src = fetch_fx([r.price_cell for r in rows])
    print(f"💱 EUR→USD {fx:.4f} ({fx_src})")

    decisions = [d for d in (decide(cat, r) for r in rows) if d is not None]
    now = datetime.now()
    updates = header_update(values, col) + plan_updates(
        decisions, fx, now.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d"))

    print(f"\n📋 {len(rows)} Stellar rows, {len(decisions)} with a code, "
          f"{len(rows) - len(decisions)} left alone\n")
    for d in decisions:
        print("  " + _describe(d, fx))
    gone_codes = [d.row.code for d in decisions if d.reason == "gone"]
    if gone_codes and plans is not None:
        print(f"\n🔎 what the raw catalogue says about the {len(set(gone_codes))} vanished codes:")
        for line in explain_gone(plans, gone_codes):
            print(line)
    stop = sanity(decisions)
    if stop:
        print(f"\n🛑 {stop}")
        return 2

    picked = [d for d in decisions if d.pick]
    repriced = sum(1 for d in picked if d.row.eur is not None and abs(d.pick.wholesale_eur - d.row.eur) > 0.001)
    redayed = sum(1 for d in picked if d.row.days != d.pick.days)
    refused = {k: sum(1 for d in decisions if d.reason == k) for k in ("gone", "short", "regional")}
    print(f"\n📊 priced {len(picked)} | price moved on {repriced} | days corrected on {redayed} | "
          f"gone {refused['gone']} | too short {refused['short']} | regional {refused['regional']} | "
          f"{len(updates)} cells")
    longer = [d for d in picked if d.won_days > 0]
    if longer:
        print(f"⏳ held the longer plan on {len(longer)} rows: "
              f"+{sum(d.won_days for d in longer)} days for +{sum(d.paid_up for d in longer) * 100:.0f} cents total")
        for d in longer:
            print(f"     row {d.row.row:<4}{d.row.sku:<9}{d.row.country:<12}{d.row.code:<11}"
                  f"{d.pick.days}d instead of {d.pick.days - d.won_days}d, +{d.paid_up * 100:.0f}c")

    if not a.apply:
        print("\n(dry run — nothing written; add --apply to write)")
        return 0
    n = write_updates(svc, col, updates)
    fixed = stamp_price_direction(svc, col)
    print(f"\n✅ wrote {n} cells"
          + (f"; corrected the text direction of {fixed} money cells" if fixed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
