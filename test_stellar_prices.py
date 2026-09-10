#!/usr/bin/env python3
"""Tests for the Stellar price refresh.

Every case here is a way the refresh could sell the customer something other
than what the page promised, or write over a cell that was not its to write.
The feed's two lies (a rounded-up size field, a coverage field that echoes the
product page) and the package-code family are reproduced from real listings.

Run:  python test_stellar_prices.py
"""

import sys

from esim_price_scraper import HEADER_KEYS
from stellar_prices import (
    stamp_price_direction,
    MARK_GONE, MARK_REGIONAL, MARK_SHORT, RETAIL_OVER_WHOLESALE, FX_FALLBACK,
    Catalogue, Variant, StellarRow, decide, fetch_fx, is_regional_sku,
    plan_updates, price_cell, read_rows,
)

_fails = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


# ── a feed the way Stellar really shapes it ─────────────────────────────────

def variant(sku, cents, gb, days, dtype="Data in Total", top_gb=None, active=True):
    return {"sku": sku, "name": sku, "active": active, "unit_price_cents": cents,
            "duration_days": days,
            # top-level data_gb is ceil'd by Stellar; meta holds the truth
            "data_gb": top_gb if top_gb is not None else gb,
            "meta": {"data_type": dtype, "data_gb": gb, "days": days}}


def product(slug, codes, variants):
    return {"slug": slug, "meta": {"coverage_codes": codes}, "variants": variants}


EU35 = [f"C{i}" for i in range(35)]
FEED = {"snapshot_generated_at": "2026-09-09T07:14:21Z", "data": [
    product("germany-esim", ["DE"], [
        variant("ESIM-GERMANY-10GB-20D-CKH995", 269, 10, 20),   # the strictly-worse twin
        variant("ESIM-GERMANY-10GB-30D-CKH995", 269, 10, 30),   # same price, longer
        variant("ESIM-GERMANY-9GB-30D-CKH995", 260, 9, 30),     # same family, wrong size
        variant("ESIM-GERMANY-750MB-7D-CKH993", 40, 0.75, 7, top_gb=1),   # the ceil trap
        variant("ESIM-GERMANY-1GB-7D-CKH993", 52, 1, 7),
        variant("ESIM-GERMANY-20GB-20D-CKH1013", 512, 20, 20),  # only 20 days exists
        variant("ESIM-GERMANY-5GB-20D-CKH130", 140, 5, 20),
        variant("ESIM-GERMANY-5GB-30D-CKH130", 150, 5, 30),     # longer but dearer
        variant("ESIM-GERMANY-1GBD-1D-PDAILY01", 100, 1, 1, dtype="Daily Unlimited"),
        variant("ESIM-GERMANY-3GB-30D-CKH777", 90, 3, 30, active=False),
    ]),
    product("taiwan-esim", ["TW"], [
        variant("ESIM-TAIWAN-10GB-30D-P32JCTKR0", 1440, 10, 30),
    ]),
    product("asia-12-areas-esim", ["TW", "JP", "KR", "TH"], [
        variant("ESIM-ASIA-12-AREAS-10GB-30D-P32JCTKR0", 1440, 10, 30),
    ]),
    product("europe-35-areas-esim", EU35, [
        variant("ESIM-EUROPE-35-AREAS-10GB-30D-P29FDU5TL", 345, 10, 30),
    ]),
]}

cat = Catalogue.from_feed(FEED)

print("-- reading the feed --")
check("per-day unlimited plans are not fixed-data listings",
      any(v.code == "PDAILY01" for v in cat.variants), False)
check("inactive listings are dropped", "CKH777" in cat.by_code, False)
check("the 750MB plan is read as 0.75GB, not the rounded-up 1",
      sorted(v.gb for v in cat.by_code["CKH993"]), [0.75, 1.0])
check("a code is one family across its sizes and days",
      sorted((v.gb, v.days) for v in cat.by_code["CKH995"]), [(9.0, 30), (10.0, 20), (10.0, 30)])
check("wholesale is retail over the measured ratio, to the cent",
      cat.by_code["CKH995"][0].wholesale_eur, round(2.69 / RETAIL_OVER_WHOLESALE, 2))
check("Taiwan's code is regional because the 12-area product sells it too",
      "P32JCTKR0" in cat.regional_codes, True)
check("a plain country code is not regional", "CKH995" in cat.regional_codes, False)
check("the 35-area Europe code is regional", "P29FDU5TL" in cat.regional_codes, True)
check("snapshot age is computed from the stamp",
      round(cat.age_hours(__import__("datetime").datetime(2026, 9, 9, 9, 14, 21,
            tzinfo=__import__("datetime").timezone.utc)), 2), 2.0)

print("-- the sheet's legend --")
check("2.0B.10 is a regional SKU", is_regional_sku("2.0B.10"), True)
check("1.0A.3 is a regional SKU", is_regional_sku("1.0A.3"), True)
check("2.49.10 is a country SKU", is_regional_sku("2.49.10"), False)
check("1.88.10 is a country SKU", is_regional_sku("1.88.10"), False)

# ── a sheet the way the owner keeps it ──────────────────────────────────────

HDR = list(HEADER_KEYS.values()) + ["<---- לא לגעת", "מחיר סופי", "נבחר"]
IX = {k: HDR.index(h) for k, h in HEADER_KEYS.items()}


def row(sku, country, gb, source, days="", price="", route="", changed="", stock="", prev=""):
    r = [""] * len(HDR)
    r[IX["code"]], r[IX["countries"]], r[IX["gb"]], r[IX["source"]] = sku, country, gb, source
    r[IX["validity"]], r[IX["price"]], r[IX["route"]] = days, price, route
    r[IX["changed"]], r[IX["stock"]], r[IX["prev"]] = changed, stock, prev
    return r


SHEET = [HDR,
    # 2: dog / 3: stellar — the 20-day twin at the price of the 30-day one
    row("2.49.10", "גרמניה", "10gb", "esim.dog", "30d", "$3.12", "Blue"),
    row("2.49.10", "גרמניה", "10gb", "Stellar", "20d", "$2.57 (€2.21)", "CKH995"),
    # 4/5: Stellar only sells 20 days here; we promise 30
    row("2.49.20", "גרמניה", "20gb", "esim.dog", "30d", "$5.94"),
    row("2.49.20", "גרמניה", "20gb", "Stellar", "20d", "$4.88 (€4.20)", "CKH1013"),
    # 6/7: 1GB — the family also holds a 750MB plan that is cheaper
    row("2.49.1", "גרמניה", "1gb", "esim.dog", "7d", "$0.60"),
    row("2.49.1", "גרמניה", "1gb", "Stellar", "7d", "$0.50 (€0.43)", "CKH993"),
    # 8: a country SKU pinned to a code that is really the Asia regional
    row("1.88.10", "טאיוואן", "10gb", "Stellar", "30d", "$13.73 (€11.81)", "P32JCTKR0"),
    # 9: Stellar-only regional SKU — its own days are the promise
    row("2.0B.10", "35 מדינות", "10gb", "Stellar", "30d", "(€2.83) $3.29", "P29FDU5TL"),
    # 10: a code that vanished
    row("2.49.5", "גרמניה", "5gb", "Stellar", "20d", "$1.34 (€1.15)", "ZZZ999", stock=MARK_SHORT),
    # 11: the owner's note row — no code, not ours
    row("2.49.40", "גרמניה", "40gb", "Stellar", "", "—", "", changed="Stellar לא מוכר 40GB"),
    # 12/13: a price that moved, and a stock word the OWNER wrote
    row("2.49.3", "גרמניה", "5gb", "esim.dog", "20d", "$1.60"),
    row("2.49.3", "גרמניה", "5gb", "Stellar", "20d", "$1.40 (€1.20)", "CKH130", stock="לא במלאי"),
    # 14: a stale error note on an otherwise unchanged row
    row("2.49.9", "גרמניה", "9gb", "Stellar", "30d", "$2.48 (€2.13)", "CKH995", changed="Check failed"),
    # 15: a real price note that must persist while nothing changes
    row("2.49.11", "גרמניה", "9gb", "Stellar", "30d", "$2.48 (€2.13)", "CKH995", changed="↓ -€0.10 (-4.5%)"),
    # 16: a short row, padded on read
    ["2.49.12", "גרמניה", "10gb", "Stellar"],
]

rows, col = read_rows(SHEET)
by_sku = {r.sku: r for r in rows}

print("-- reading the sheet --")
check("only Stellar rows come back", sorted(r.row for r in rows), [3, 5, 7, 8, 9, 10, 11, 13, 14, 15, 16])
check("columns are found by header, wherever they sit", col["route"], IX["route"])
check("the promise is the esim.dog row's days", by_sku["2.49.10"].floor_days, 30)
check("a Stellar-only SKU is promised its own days", by_sku["2.0B.10"].floor_days, 30)
check("euros are parsed whichever side of the dollars they sit", by_sku["2.0B.10"].eur, 2.83)
check("a short row is padded rather than crashing", by_sku["2.49.12"].code, "")

print("-- choosing the listing --")
d = {r.sku: decide(cat, r) for r in rows}
check("same price, longer product wins: 20d twin → 30d",
      (d["2.49.10"].pick.days, d["2.49.10"].pick.wholesale_eur), (30, 2.21))
check("fewer days than promised is refused, and says what exists",
      (d["2.49.20"].reason, d["2.49.20"].have_days), ("short", (20,)))
check("the cheaper 750MB plan never stands in for 1GB",
      (d["2.49.1"].pick.gb, d["2.49.1"].pick.wholesale_eur), (1.0, 0.43))
check("a regional code under a country SKU is refused", d["1.88.10"].reason, "regional")
check("the same regional code under a regional SKU is priced",
      (d["2.0B.10"].reason, d["2.0B.10"].pick.wholesale_eur), ("", 2.83))
check("a vanished code is reported gone", d["2.49.5"].reason, "gone")
check("a '—' row is not a decision at all", d["2.49.40"], None)
check("cheapest among what qualifies, even when a longer one exists",
      (d["2.49.3"].pick.days, d["2.49.3"].pick.wholesale_eur), (20, 1.15))
check("a row with no code is left alone", d["2.49.12"], None)

print("-- what gets written --")
FX, TS, TODAY = 1.16, "2026-09-09 12:00", "2026-09-09"
ups = plan_updates([x for x in d.values() if x], FX, TS, TODAY)
W = {}
for r, k, v in ups:
    W.setdefault(r, {})[k] = v

check("the 20d twin's days are corrected to 30d", W[3].get("validity"), "30d")
check("...and the correction is noted without inventing a price change",
      (W[3].get("changed"), "prev" in W[3]), ("↔ 20d → 30d", False))
check("price cell is dollars then euros", W[3]["price"], price_cell(2.21, FX))
check("every decided row gets a timestamp", all("updated" in W[r] for r in (3, 5, 7, 8, 9, 10, 13, 14, 15)), True)

check("too short: our marker goes in the stock column", W[5].get("stock"), MARK_SHORT)
check("too short: the note names the code, the size and what exists",
      W[5]["changed"], "אין 20GB ל-30+ ימים בקוד CKH1013 (יש: 20d)")
check("too short: the price is NOT rewritten", "price" in W[5], False)

check("regional: marker + note", (W[8].get("stock"), W[8]["changed"]),
      (MARK_REGIONAL, "קוד אזורי תחת שם מדינה — לא הושווה"))
check("gone: marker replaces our older marker", W[10].get("stock"), MARK_GONE)
check("the owner's note row is untouched", 11 in W, False)

check("a price move writes prev, an arrow note and last_change",
      (W[13].get("prev"), W[13]["changed"], W[13].get("last_change")),
      ("$1.40 (€1.20)", "↓ -€0.05 (-4.2%)", TODAY))
check("...but never clears a stock word the owner wrote", "stock" in W[13], False)
check("a stale error note is cleared when the row is fine", W[14].get("changed"), "")
check("a real price note persists while nothing changes", "changed" in W[15], False)
check("nothing else is written on an unchanged row", sorted(W[15]), ["price", "updated"])
check("the reversed euro-first cell is rewritten in canonical order",
      W[9]["price"], price_cell(2.83, FX))

# the recovery path: our own marker is cleared once the row prices again
rec_row = by_sku["2.49.10"]
rec_row.stock = MARK_GONE
ups2 = plan_updates([decide(cat, rec_row)], FX, TS, TODAY)
check("our own marker is cleared when the code is back",
      any(k == "stock" and v == "" for _, k, v in ups2), True)

print("-- the exchange rate --")
check("live rate is used when it answers", fetch_fx([], fetch=lambda: 1.2), (1.2, "frankfurter"))
check("a failing fetch falls back to the sheet's own dollars/euros",
      fetch_fx(["$2.57 (€2.21)", "(€4.20) $4.88", "—"], fetch=lambda: None)[0], round((2.57 / 2.21 + 4.88 / 4.20) / 2, 4))
check("an exception is a failure too, not a crash",
      fetch_fx(["$1.16 (€1.00)"], fetch=lambda: 1 / 0)[1], "derived from the sheet's own cells")
check("with nothing to derive from, the constant", fetch_fx([], fetch=lambda: None), (FX_FALLBACK, "hard-coded fallback"))
check("price cell format", price_cell(2.21, 1.1614), "$2.57 (€2.21)")

print("-- the price column's text direction --")


class _FakeSvc:
    """Enough of the Sheets service to watch what stamp_price_direction sends."""

    def __init__(self, cells):
        self.cells, self.requests = cells, []

    def spreadsheets(self):
        return self

    def get(self, **kw):
        if "includeGridData" in kw:
            rows = [{"values": [{
                "userEnteredValue": {"stringValue": t},
                "userEnteredFormat": ({"textDirection": d} if d else {})}]}
                for t, d in self.cells]
            return _Exec({"sheets": [{"data": [{"rowData": rows}]}]})
        return _Exec({"sheets": [{"properties": {
            "sheetId": 7, "gridProperties": {"rowCount": 300}}}]})

    def batchUpdate(self, **kw):
        self.requests.extend(kw["body"]["requests"])
        return _Exec({})


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self, **kw):
        return self.v


svc = _FakeSvc([("$2.57 (€2.21)", None), ("$1.00 (€0.86)", "LEFT_TO_RIGHT"),
                ("", None), ("$3.13 (€2.69)", None)])
backwards = stamp_price_direction(svc, {"price": 6, "prev": 7})
# two unstamped cells in each of the two columns
check("counts only non-empty cells that lack the direction", backwards, 4)
reqs = [r["repeatCell"] for r in svc.requests[0:1]] + [
    r["repeatCell"] for r in svc.requests[1:]]
cols = sorted((r["range"]["startColumnIndex"], r["range"]["endColumnIndex"]) for r in reqs)
check("stamps BOTH money columns, not just the buy price", cols, [(6, 7), (7, 8)])
req = reqs[0]
check("stamps the real sheetId, not a hard-coded 0", req["range"]["sheetId"], 7)
check("skips the header row", req["range"]["startRowIndex"], 1)
check("covers the whole grid, not just today's rows", req["range"]["endRowIndex"], 300)
check("sets left-to-right",
      req["cell"]["userEnteredFormat"]["textDirection"], "LEFT_TO_RIGHT")
check("touches only the two format fields it owns", req["fields"],
      "userEnteredFormat.textDirection,userEnteredFormat.horizontalAlignment")
check("a fully stamped column reports nothing to fix",
      stamp_price_direction(_FakeSvc([("$1 (€1)", "LEFT_TO_RIGHT")]), {"price": 6}), 0)
check("a column the sheet does not have is skipped, not crashed on",
      stamp_price_direction(_FakeSvc([("$1 (€1)", None)]), {"price": 6}), 1)

# ── the wholesale API (from_api) ───────────────────────────────────────────
from stellar_prices import Decision, _gb_from_mb, sanity

def plan(sku, cents, mb, days, codes=("DE",), available=True, unit="plan",
         configurable=False, pid="uuid-1", synced="2026-09-10T05:00:00Z"):
    return {"id": pid, "sku": sku, "name": sku, "product_slug": "germany",
            "data": {"megabytes": mb, "label": None, "type": None},
            "validity_days": days, "available": available,
            "duration": {"configurable": configurable, "default_days": days,
                         "minimum_days": days, "maximum_days": days},
            "price": {"currency": "EUR", "amount": f"{cents/100:.2f}",
                      "amount_cents": cents, "billing_unit": unit},
            "coverage": {"codes": list(codes), "countries": [], "networks": [],
                         "breakout_ip_country_code": "GB"},
            "catalogue_synced_at": synced}

api = Catalogue.from_api([
    plan("ESIM-GERMANY-10GB-30D-CKH995", 221, 10240, 30),
    plan("ESIM-GERMANY-10GB-20D-CKH995", 221, 10240, 20, pid="uuid-2", synced="2026-09-09T05:00:00Z"),
    plan("ESIM-GERMANY-750MB-7D-CKH993", 40, 768, 7),
    plan("ESIM-MOROCCO-3GBD-1D-PW8BZYG4P", 223, 3072, 1, unit="day", configurable=True),
    plan("ESIM-EUROPE-5GB-30D-CKH980", 159, 5120, 30, codes=("DE", "FR", "IT")),
    plan("ESIM-GERMANY-20GB-30D-CKH1013", 420, 20480, 30, available=False),
    plan(None, 100, 1024, 7),
])
check("API price is what we pay — NOT divided by the retail factor",
      sorted(v.wholesale_eur for v in api.by_code["CKH995"]), [2.21, 2.21])
check("plan UUID rides along for the buyer", api.by_code["CKH995"][0].plan_id, "uuid-1")
check("daily-unlimited, unavailable and SKU-less listings are dropped",
      sorted(api.by_code), ["CKH980", "CKH993", "CKH995"])
check("regional is decided by the plan's own coverage codes", api.regional_codes, {"CKH980"})
check("the newest catalogue scan is the snapshot time", api.generated_at, "2026-09-10T05:00:00Z")
check("binary megabytes land on the sheet's decimal sizes",
      [_gb_from_mb(m) for m in (3072, 10240, 768, 750, 512, 500, 100, 1536, 1500)],
      [3.0, 10.0, 0.75, 0.75, 0.5, 0.5, 0.1, 1.5, 1.5])
check("750MB row matches a 768MB listing", api.by_code["CKH993"][0].gb, 0.75)

# ── the broken-read guard ──────────────────────────────────────────────────
def dec(reason): return Decision(None, None, reason)
check("most codes gone at once = a broken read, stop",
      bool(sanity([dec("gone"), dec("gone"), dec("gone"), dec("")])), True)
check("a few gone is a catalogue change, go on",
      sanity([dec("gone"), dec(""), dec(""), dec("")]), "")
check("regional refusals do not count either way",
      sanity([dec("gone"), dec("gone"), dec("regional"), dec("regional"), dec(""), dec("")]), "")
check("too few coded rows to judge — go on", sanity([dec("gone"), dec("gone")]), "")

print()


print("-- validity is not for sale --")

# Variant(code, gb, days, wholesale_eur, slug, name)
IDN20 = ("JC900", 10.0, 20, 2.12, "indonesia", "Indonesia 10GB 20Days")
IDN30 = ("JC900", 10.0, 30, 2.13, "indonesia", "Indonesia 10GB 30Days")
CUT20 = ("JC900", 10.0, 20, 2.00, "s", "20Days")


def _row(floor, days=None):
    return StellarRow(1, "1.60.10", "INDONESIA", "JC900", 10.0, days or floor,
                      None, "", "", "", floor)


def _pick(floor, *vs):
    """(days, EUR, days won back, cents paid for them) for a 10GB Indonesia row."""
    d = decide(Catalogue([Variant(*v) for v in vs], set()), _row(floor))
    return (d.pick.days, d.pick.wholesale_eur, d.won_days, round(d.paid_up * 100))


check("an agora never buys back ten days of validity",
      _pick(20, IDN20, IDN30), (30, 2.13, 10, 1))
check("at exactly 5% over the cheapest the longer plan still wins",
      _pick(20, CUT20, ("JC900", 10.0, 30, 2.10, "s", "30Days")), (30, 2.10, 10, 10))
check("one cent past 5% and thrift takes it back",
      _pick(20, CUT20, ("JC900", 10.0, 30, 2.11, "s", "30Days")), (20, 2.00, 0, 0))
check("a genuinely dearer long plan is still refused",
      _pick(20, CUT20, ("JC900", 10.0, 30, 2.50, "s", "30Days")), (20, 2.00, 0, 0))
check("the promise outranks the bargain: a cheap 15d listing cannot win",
      _pick(20, ("JC900", 10.0, 15, 0.50, "s", "15Days"), IDN20, IDN30), (30, 2.13, 10, 1))
check("a row with nothing longer to buy reports no upgrade",
      _pick(30, IDN30), (30, 2.13, 0, 0))
check("a (nonhkip) twin never wins on the order the API listed it in",
      decide(Catalogue([Variant(*v) for v in (
          ("JC900", 10.0, 30, 2.13, "s", "Indonesia 10GB 30Days (nonhkip)"),
          ("JC900", 10.0, 30, 2.13, "s", "Indonesia 10GB 30Days"))], set()),
          _row(30)).pick.name,
      "Indonesia 10GB 30Days")


if _fails:
    print(f"❌ {len(_fails)} failed: {_fails}")
    sys.exit(1)
print("✅ all checks passed")
