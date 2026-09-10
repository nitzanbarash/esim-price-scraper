#!/usr/bin/env python3
"""
Pins the Stellar buyer: which order is placed, under which idempotency key,
what the site is handed back, and -- above all -- when NOTHING is bought.
Offline: the site, Stellar and both sheets are faked with their real payload
shapes (the wholesale OpenAPI 1.6.0, /api/orders, the 25-column receipts tab).

Run: python test_stellar_buyer.py
"""
from __future__ import annotations

import io
import logging
import os
import re
import sys
from datetime import datetime

os.environ.setdefault("ORDERS_TOKEN", "test-token")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON", "{}")

import fulfillment_bot as fb          # noqa: E402
import stellar_prices as sp           # noqa: E402
import stellar_buyer as sb            # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    print(("✅ " if ok else "❌ ") + name + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


# ── fakes ────────────────────────────────────────────────────────────────────

class Resp:
    def __init__(self, code: int, body):
        self.status_code, self._body = code, body
        self.response = self

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class Site:
    """waverole.com /api/orders: the queue, the claim, the reports."""
    def __init__(self, orders, claim_by: str = "", fail_report: bool = False):
        self.orders, self.claim_by, self.posts, self.gets = orders, claim_by, [], []
        self.fail_report = fail_report      # the site is unreachable when reporting

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append(dict(params or {}))
        return Resp(200, {"orders": self.orders})

    def post(self, url, json=None, headers=None, timeout=None):
        if self.fail_report and "status" in json:
            import requests
            raise requests.ConnectionError("site unreachable")
        self.posts.append(dict(json))
        if "claim" in json:
            if self.claim_by:
                return Resp(409, {"ok": False, "claimed": False, "by": self.claim_by})
            return Resp(200, {"ok": True, "claimed": True, "by": json["claim"]})
        return Resp(200, {"ok": True})

    def reports(self, status):
        return [p for p in self.posts if p.get("status") == status]


LPA = "LPA:1$smdp.example.net$ABCDEF123456"
QR = "https://p.esimlink.org/e/abc123"


def plan(pid, code, gb, days, cents, dest="Indonesia", cc="ID", slug="indonesia-esim"):
    return {"id": pid, "sku": f"ESIM-{dest.upper()}-{gb:g}GB-{days}D-{code}",
            "name": f"{dest} {gb:g}GB {days}Days", "slug": f"{slug}-{gb:g}gb-{days}d",
            "product_slug": slug, "destination": dest, "country_code": cc,
            "data": {"megabytes": int(gb * 1024), "label": f"{gb:g}GB", "type": "fixed"},
            "validity_days": days, "duration": {"configurable": False, "default_days": days,
                                                 "minimum_days": days, "maximum_days": days},
            "coverage": {"codes": [cc], "countries": [dest],
                         "networks": [{"country": dest, "country_code": cc, "operator": "Telkomsel", "network": "5G"},
                                      {"country": dest, "country_code": cc, "operator": "XL", "network": "4G"}],
                         "breakout_ip_country_code": "HK"},
            "price": {"currency": "EUR", "amount": f"{cents/100:.2f}", "amount_cents": cents, "billing_unit": "plan"},
            "available": True, "catalogue_synced_at": "2026-09-10T00:00:00Z"}


PLANS = [plan("p-20d", "JC059", 10, 20, 212), plan("p-30d", "JC059", 10, 30, 213),
         plan("p-3gb", "P0BS87WNL", 3, 15, 70)]


class StellarFake:
    def __init__(self, wallet=10000, statuses=("fulfilled",), create=202, with_install=True,
                 esims_read=True):
        # create may be one answer or a sequence of them, so a replay under the
        # same idempotency key can be answered differently from the first try.
        self.wallet, self.statuses = wallet, list(statuses)
        self.create_codes = list(create) if isinstance(create, (list, tuple)) else [create]
        self.with_install, self.esims_read = with_install, esims_read
        self.creates, self.gets, self.headers = [], [], {"Authorization": "Bearer k"}
        self.last_status = "processing"     # what /esims/{id} reflects: nothing before fulfilment

    def _esim(self, status):
        e = {"id": "e-1", "sim_id": "1234567890123456", "plan_id": "p-30d", "unit": 1,
             "duration_days": 30, "status": status, "order_id": "so-1", "usage_available": False}
        if status == "fulfilled" and self.with_install and self.esims_read:
            e["installation"] = {"qr_code_url": QR, "activation_code": LPA, "apn": "internet"}
        return e

    def get(self, url, timeout=None, **kw):
        self.gets.append(url)
        if url.endswith("/wallet"):
            return Resp(200, {"data": {"currency": "EUR", "available_balance_cents": self.wallet}})
        if "/esims/" in url:
            if not self.esims_read:
                return Resp(403, {"error": {"code": "forbidden", "message": "esims:read"}})
            # Credentials exist only once the order is fulfilled -- a unit of
            # a processing, failed or reviewed order has no installation block.
            return Resp(200, {"data": self._esim(self.last_status)})
        if "/plans/" in url:
            pid = url.rsplit("/", 1)[1]
            for p in PLANS:
                if p["id"] == pid:
                    return Resp(200, {"data": p})
            return Resp(404, {})
        if "/orders/" in url:
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            self.last_status = status
            return Resp(200, {"data": {"id": "so-1", "order_number": "SW-1", "status": status,
                                       "ready": status == "fulfilled", "esims": [self._esim(status)],
                                       "sensitive_delivery_included": self.esims_read}})
        return Resp(404, {})

    def post(self, url, json=None, headers=None, timeout=None):
        self.creates.append((headers.get("Idempotency-Key"), json))
        code = self.create_codes.pop(0) if len(self.create_codes) > 1 else self.create_codes[0]
        if code == "timeout":                      # sent, never answered
            import requests
            raise requests.exceptions.ReadTimeout("no answer")
        if code == "noid":                         # accepted, but names no order
            return Resp(202, {"data": {"status": "processing"}})
        if code == 202:
            return Resp(202, {"data": {"id": "so-1", "status": "processing", "ready": False, "esims": []}})
        return Resp(code, {"error": {"code": "refused", "message": f"code {code}"}})


HDR = ["מייל - Mail", "תאריך - Date", 'מק"ט - SUK', "איחסון - GB", "GB (0/X) - ניצול", "מס׳ הזמנה",
       "QR", "Activation Code", "SM-DP+ Address", "", "Link - esim.dog", "Link - waverole",
       "מס סידורי -ICCID", "גישה - APN", "אזור - Region", "חבילה - Plan", "Route", "הופעל - Activated",
       "סטטוס - Status", "", "מקור - source", "", "קנייה - Buy", "הנחה - Sale", "מכירה - Sell"]


class Ws:
    def __init__(self, rows=None):
        self.rows = [list(HDR)] + [list(r) for r in (rows or [])]

    def row_values(self, i):
        return list(self.rows[i - 1])

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def update(self, a1, values, value_input_option=None):
        n = int(a1[1:])
        while len(self.rows) < n:
            self.rows.append([""] * len(HDR))
        self.rows[n - 1] = list(values[0]) + [""] * (len(HDR) - len(values[0]))

    def update_cells(self, cells, value_input_option=None):
        for c in cells:
            self.rows[c.row - 1][c.col - 1] = c.value

    def col(self, n, header):
        return self.rows[n - 1][HDR.index(header)]


def receipt_row(order_id, status, stellar_id="so-1", route="Stellar JC059"):
    r = [""] * len(HDR)
    r[HDR.index("מס׳ הזמנה")] = order_id
    r[HDR.index("Route")] = route
    r[HDR.index("סטטוס - Status")] = status
    r[HDR.index("Link - esim.dog")] = (f"https://wholesale.stellarsecurity.com/orders/{stellar_id}"
                                      if stellar_id else "https://wholesale.stellarsecurity.com/orders")
    return r


def sheet_row(sku="1.62.10", code="JC059", gb=10.0, days=20, eur=2.12, floor=14, country="אינדונזיה"):
    return sp.StellarRow(row=83, sku=sku, country=country, code=code, gb=gb, days=days, eur=eur,
                         price_cell=f"${eur*1.165:.2f} (€{eur:.2f})", changed="", stock="", floor_days=floor)


def token(d=None, gb=10):
    import base64, json
    payload = {"id": "WR-TEST01", "sku": "1.62.10", "ts": 1, "t": 6.99, "l": "he", "lp": 6.99, "gb": gb}
    if d:
        payload["d"] = d
    return "https://www.waverole.com/?order=" + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def order(oid="WR-TEST01", sku="1.62.10", paid=6.99, list_usd=6.99, d=None, source="paypal"):
    return {"order_id": oid, "sku": sku, "customer_email": "buyer@example.com", "paid_usd": paid,
            "ts": "2026-09-10T01:00:00Z", "status": "pending", "order_url": token(d), "source": source,
            "supplier": "stellar", "list_usd": list_usd, "code": "JC059", "stale_alert_rung": 0}


alerts: list[tuple[str, str]] = []
NOW_TOP = datetime(2026, 9, 10, 4, 1, tzinfo=fb.TZ)     # alerts pass
NOW_MID = datetime(2026, 9, 10, 4, 30, tzinfo=fb.TZ)    # hourly alerts held


def scenario(orders, stellar: StellarFake, ws: Ws | None = None, rows=None, claim_by="", now=NOW_TOP,
             plans=PLANS, settle_wait=5, fail_report=False):
    """Wire the fakes in, run once, hand everything back."""
    site = Site(orders, claim_by, fail_report)
    ws = ws or Ws()
    alerts.clear()
    sb.requests.get, sb.requests.post = site.get, site.post
    sb.sp.fetch_plans = lambda key: list(plans)
    sb.stellar_rows = lambda: {r.sku: r for r in (rows if rows is not None else [sheet_row()])}
    sb.fb.alert = lambda s, b: alerts.append((s, b))
    sb.time.sleep = lambda s: None
    sb.SETTLE_WAIT_S = settle_wait
    st = sb.Stellar("k", session=stellar)
    log = io.StringIO()
    h = logging.StreamHandler(log)
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    try:
        bought = sb.run(st, ws, now)
    finally:
        logging.getLogger().removeHandler(h)
    return site, ws, bought, log.getvalue()


# ── 1. the happy path ────────────────────────────────────────────────────────
print("\n1. a pending Stellar order is bought, settled and handed to the site")
site, ws, bought, logtxt = scenario([order()], (st_fake := StellarFake()))
check("buyer poll names its supplier", bool(site.gets) and site.gets[0].get("supplier") == "stellar", str(site.gets))
check("claimed as gha:stellar first", bool(site.posts) and site.posts[0].get("claim") == "gha:stellar", str(site.posts[:1]))
check("exactly one Stellar order placed", len(st_fake.creates) == 1, str(st_fake.creates))
key, body = st_fake.creates[0]
check("idempotency key is waverole-<order>-a0", key == "waverole-WR-TEST01-a0", key)
check("the 30-day twin is bought (validity beats a cent)", body == {"plans": [{"plan_id": "p-30d", "quantity": 1}]}, str(body))
ful = site.reports("fulfilled")
check("site told fulfilled once", len(ful) == 1, str(len(ful)))
e = ful[0]["esim"] if ful else {}
check("activation code, QR url and SM-DP+ handed over",
      e.get("activation_code") == LPA and e.get("qr_code") == QR and e.get("smdp") == "smdp.example.net", str(e))
check("plan string parses for the customer email (PLAN_RE)",
      bool(fb.PLAN_RE.search(e.get("plan", ""))) and e.get("plan") == "10GB - 30 days", e.get("plan"))
check("networks named from the listing", e.get("networks") == "Telkomsel/XL • 4G + 5G", e.get("networks"))
check("no ICCID invented", e.get("iccid") == "")
n = 2
check("receipts row appended at A2 (explicit range, not append_row)", ws.col(n, "מס׳ הזמנה") == "WR-TEST01")
check("Route says Stellar + package code", ws.col(n, "Route") == "Stellar JC059", ws.col(n, "Route"))
check("supplier link names the Stellar order", ws.col(n, "Link - esim.dog").endswith("/orders/so-1"))
check("status ends פעיל, credentials in the row",
      ws.col(n, "סטטוס - Status") == "פעיל" and ws.col(n, "Activation Code") == LPA and ws.col(n, "QR") == QR)
check("Buy is EUR x the row's own FX, Sell is what the customer paid",
      ws.col(n, "קנייה - Buy") == "2.48$" and ws.col(n, "מכירה - Sell") == "6.99$",
      f"{ws.col(n, 'קנייה - Buy')} / {ws.col(n, 'מכירה - Sell')}")
check("source is the rail, spelled as the PC bot spells it", ws.col(n, "מקור - source") == "paypal", ws.col(n, "מקור - source"))
check("no rail on the order -> the PC bot's old constant", sb.source_text("") == "bot - manually")
check("no discount given -> '-'", ws.col(n, "הנחה - Sale") == "-", ws.col(n, "הנחה - Sale"))
check("nothing secret in the log", LPA not in logtxt and QR not in logtxt and "buyer@example.com" not in logtxt)
check("no alert on a clean purchase", not alerts, str(alerts))
check("run() reports one purchase", bought == 1)

# ── 2. second run: the row exists -> settle, never re-buy ────────────────────
print("\n2. the next run finds the row and settles without buying again")
ws2 = Ws([receipt_row("WR-TEST01", "מעבד")])
site, ws2, bought, _ = scenario([order()], (f2 := StellarFake()), ws=ws2)
check("no second Stellar order", len(f2.creates) == 0, str(f2.creates))
check("delivered from the existing Stellar order", len(site.reports("fulfilled")) == 1)
check("row 2 updated in place, no new row", len(ws2.rows) == 2 and ws2.col(2, "סטטוס - Status") == "פעיל")
check("region and networks fetched with ONE plan call", any(u.endswith("/plans/p-30d") for u in f2.gets)
      and site.reports("fulfilled")[0]["esim"]["region"] == "Indonesia")

# ── 3. still provisioning: hand over to the next run ─────────────────────────
print("\n3. Stellar still provisioning -> nothing reported, the row waits")
site, ws3, _, _ = scenario([order()], (f3 := StellarFake(statuses=("processing",))), settle_wait=0)
check("order placed, row says מעבד", len(f3.creates) == 1 and ws3.col(2, "סטטוס - Status") == "מעבד")
check("site not told anything yet", not site.reports("fulfilled") and not site.reports("failed"))
check("no alert for a normal wait", not alerts)

# ── 4. another buyer holds the claim ─────────────────────────────────────────
print("\n4. the claim is held by another worker -> hands off")
site, ws4, _, _ = scenario([order()], (f4 := StellarFake()), claim_by="pc:DESKTOP")
check("nothing bought, nothing written", len(f4.creates) == 0 and len(ws4.rows) == 1)

# ── 5. the money rules ───────────────────────────────────────────────────────
print("\n5. price rose past the tolerance -> refused, parked, owner told")
# Both twins rose: were the 20-day still 2.12, buying IT would be right.
dear = [plan("p-20d", "JC059", 10, 20, 240), plan("p-30d", "JC059", 10, 30, 241)]
site, ws5, _, _ = scenario([order()], (f5 := StellarFake()), plans=dear)
check("no Stellar order", len(f5.creates) == 0)
fl = site.reports("failed")
check("site told failed with the reason", len(fl) == 1 and "price rose" in fl[0].get("reason", ""), str(fl))
check("owner alerted at once", alerts and "refused" in alerts[0][0], str(alerts))

print("\n   the 5% validity tolerance is allowed (sheet 2.12, listing 2.13)")
site, _, _, _ = scenario([order()], (f5b := StellarFake()))
check("2.13 against a 2.12 cell is bought", len(f5b.creates) == 1)

print("\n   the customer's own token outranks the sheet floor")
only20 = [plan("p-20d", "JC059", 10, 20, 212)]
site, _, _, _ = scenario([order(d=30)], (f5c := StellarFake()), plans=only20)
check("promised 30 days, only 20 on sale -> refused as short",
      len(f5c.creates) == 0 and site.reports("failed") and "short" in site.reports("failed")[0]["reason"],
      str(site.reports("failed")))

print("\n   the per-order cap")
sb.MAX_EUR = 2.0
site, _, _, _ = scenario([order()], (f5d := StellarFake()))
check("over the cap -> refused", len(f5d.creates) == 0 and "cap" in site.reports("failed")[0]["reason"])
sb.MAX_EUR = 25.0

print("\n   no sheet row for the SKU")
site, _, _, _ = scenario([order(sku="9.99.99")], (f5e := StellarFake()))
check("refused, site told", len(f5e.creates) == 0 and "no Stellar row" in site.reports("failed")[0]["reason"])

# ── 6. the wallet ────────────────────────────────────────────────────────────
print("\n6. wallet too low -> nothing bought, order stays queued, one alert an hour")
site, ws6, _, _ = scenario([order()], (f6 := StellarFake(wallet=100)))
check("no Stellar order, no row", len(f6.creates) == 0 and len(ws6.rows) == 1)
check("order NOT reported failed (it is not the order's fault)", not site.reports("failed"))
check("top-up alert sent at the top of the hour", alerts and "wallet" in alerts[0][0], str(alerts))
site, _, _, _ = scenario([order()], StellarFake(wallet=100), now=NOW_MID)
check("...and held mid-hour", not alerts)

print("\n   Stellar answers 402 anyway")
site, ws6b, _, _ = scenario([order()], (f6b := StellarFake(create=402)))
check("no row, no failed report, wallet alert", len(ws6b.rows) == 1 and not site.reports("failed")
      and alerts and "wallet" in alerts[0][0])

# ── 7. Stellar fails the order ───────────────────────────────────────────────
print("\n7. Stellar ends the order 'failed' -> parked for retry under a NEW key")
site, ws7, _, _ = scenario([order()], (f7 := StellarFake(statuses=("failed",))))
check("site told failed", len(site.reports("failed")) == 1)
check("row marked נכשל", ws7.col(2, "סטטוס - Status").startswith("נכשל"), ws7.col(2, "סטטוס - Status"))
site, ws7, _, _ = scenario([order()], (f7b := StellarFake()), ws=ws7)
check("retry buys again under -a1", len(f7b.creates) == 1 and f7b.creates[0][0] == "waverole-WR-TEST01-a1",
      str(f7b.creates))
check("...as a NEW row, the failed one kept", len(ws7.rows) == 3 and ws7.col(3, "סטטוס - Status") == "פעיל")

print("\n   a failed row is not settled again on every run")
ws7c = Ws([receipt_row("WR-TEST01", "נכשל (failed)")])
site, ws7c, _, _ = scenario([order()], (f7c := StellarFake()), ws=ws7c)
check("the failed row is ignored and a fresh attempt is made", len(f7c.creates) == 1 and len(ws7c.rows) == 3)

# ── 8. manual review: hands off, one alert ───────────────────────────────────
print("\n8. manual_review -> row marked, ONE alert, no re-buy on later runs")
site, ws8, _, _ = scenario([order()], (f8 := StellarFake(statuses=("manual_review",))))
check("row says בבדיקה ידנית", ws8.col(2, "סטטוס - Status") == "בבדיקה ידנית")
check("one alert", len(alerts) == 1 and "manual review" in alerts[0][0], str(alerts))
check("order left pending on the site", not site.reports("failed") and not site.reports("fulfilled"))
site, ws8, _, _ = scenario([order()], (f8b := StellarFake(statuses=("manual_review",))), ws=ws8)
check("second run: no purchase, no second alert, no Stellar call at all",
      len(f8b.creates) == 0 and not alerts and not f8b.gets)

# ── 9. idempotency conflict ──────────────────────────────────────────────────
print("\n9. 409 idempotency conflict -> review row blocks the next run")
site, ws9, _, _ = scenario([order()], (f9 := StellarFake(create=409)))
check("review row written without an order id", ws9.col(2, "סטטוס - Status") == "בבדיקה ידנית"
      and ws9.col(2, "Link - esim.dog").endswith("/orders"))
check("owner alerted", alerts and "idempotency" in alerts[0][0])
site, ws9, _, _ = scenario([order()], (f9b := StellarFake()), ws=ws9)
check("next run buys nothing", len(f9b.creates) == 0)

# ── 10. site-SKU spelling ────────────────────────────────────────────────────
print("\n10. the site's 1.A.x is the sheet's 1.0A.x")
site, ws10, _, _ = scenario([order(sku="1.A.10")], (f10 := StellarFake()),
                            rows=[sheet_row(sku="1.0A.10", code="JC059")])
check("row found under the sheet spelling and bought", len(f10.creates) == 1
      and ws10.col(2, 'מק"ט - SUK') == "1.0A.10")

# ── 11. the per-run cap ──────────────────────────────────────────────────────
print("\n11. more orders than the per-run cap -> the rest wait")
sb.MAX_ORDERS_PER_RUN = 2
many = [order(oid=f"WR-{i}") for i in range(4)]
site, ws11, bought, _ = scenario(many, (f11 := StellarFake()))
check("two bought, two left for the next run", len(f11.creates) == 2 and bought == 2)
sb.MAX_ORDERS_PER_RUN = 5

# ── 12. credentials the key cannot see ───────────────────────────────────────
print("\n12. fulfilled at Stellar but no credentials visible -> wait, never deliver blanks")
site, ws12, _, _ = scenario([order()], StellarFake(esims_read=False), settle_wait=0)
check("site not told fulfilled", not site.reports("fulfilled"))
check("row stays מעבד", ws12.col(2, "סטטוס - Status") == "מעבד")

# ── 13. an esim.dog row with the same order number is not ours ───────────────
print("\n13. an esim.dog receipts row for the same order number is not mistaken for ours")
ws13 = Ws([receipt_row("WR-TEST01", "פעיל", stellar_id="", route="Blue")])
site, ws13, _, _ = scenario([order()], (f13 := StellarFake()), ws=ws13)
check("bought as a first attempt (-a0)", len(f13.creates) == 1 and f13.creates[0][0] == "waverole-WR-TEST01-a0")

# ── 14. the empty queue costs nothing ────────────────────────────────────────
print("\n14. an empty queue touches neither Stellar nor the sheets")
site, ws14, bought, _ = scenario([], (f14 := StellarFake()))
check("no calls", not f14.gets and not f14.creates and bought == 0)

# ── 15. discount wording ─────────────────────────────────────────────────────
print("\n15. discount wording matches the PC bot's")
check("100% coupon", sb.discount_text(15.98, 0.01) == "100% ($15.98 הנחה)" or sb.discount_text(15.98, 0) == "100% ($15.98 הנחה)")
check("10% coupon", sb.discount_text(15.99, 14.51) == "9% ($1.48 הנחה)", sb.discount_text(15.99, 14.51))
check("no list price -> blank", sb.discount_text(None, 6.99) == "")

# ── 16. an answer that does not say whether the money moved ──────────────────
# The site's 'failed' means "no package was bought": it puts the PAID order
# back on a retry list for exactly that reason (api/orders.js). So the word is
# earned only by an answer that PROVES nothing was created. Everything else
# leaves the order queued and lets the idempotency key settle the question.
print("\n16. Stellar's answer does not say whether the order was created")

check("a 500 is an unknown outcome", sb.unknown_outcome(500, {"error": {}}))
check("so is a 502, a 504 and a 521", all(sb.unknown_outcome(c, {}) for c in (502, 504, 521)))
check("so is a 2xx that names no order", sb.unknown_outcome(202, {"data": {"status": "processing"}}))
check("so is a body that was not JSON", sb.unknown_outcome(200, {}))
check("so is no answer at all (read timeout)", sb.unknown_outcome(0, {}))
check("a 202 WITH an id is not", not sb.unknown_outcome(202, {"data": {"id": "so-1"}}))
check("a refusal is not unknown", not any(sb.unknown_outcome(c, {}) for c in (400, 402, 409, 422)))

print("\n   500 then a clean answer -> ONE package, the same key replayed")
site, ws16, _, _ = scenario([order()], (f16 := StellarFake(create=[500, 202])))
check("asked twice under the SAME key", len(f16.creates) == 2
      and {k for k, _ in f16.creates} == {"waverole-WR-TEST01-a0"}, str(f16.creates))
check("one row, delivered", len(ws16.rows) == 2 and ws16.col(2, sb.H_STATUS) == sb.ST_ACTIVE
      and len(site.reports("fulfilled")) == 1)

print("\n   500 twice -> the site is told NOTHING and a person is asked")
site, ws16b, _, _ = scenario([order()], (f16b := StellarFake(create=500)))
check("never reported failed -- the site would re-queue it to be bought again",
      not site.reports("failed") and not site.reports("fulfilled"), str(site.posts))
check("review row, no Stellar order id", ws16b.col(2, sb.H_STATUS) == sb.ST_REVIEW
      and ws16b.col(2, "Link - esim.dog").endswith("/orders"))
check("the alert names the key and does NOT claim the wallet is untouched",
      alerts and "waverole-WR-TEST01-a0" in alerts[0][1]
      and "debited" not in alerts[0][1].lower(), str(alerts))
print("\n   ...and that row stops the next run cold")
site, ws16b, _, _ = scenario([order()], (f16c := StellarFake()), ws=ws16b)
check("nothing bought, Stellar not even asked", len(f16c.creates) == 0 and not f16c.gets)

print("\n   a read timeout is not a refusal")
site, ws16d, _, _ = scenario([order()], (f16d := StellarFake(create=["timeout", 202])))
check("replayed under the same key and delivered", len(f16d.creates) == 2
      and len(site.reports("fulfilled")) == 1 and not site.reports("failed"))

print("\n   accepted, but naming no order")
site, ws16e, _, _ = scenario([order()], (f16e := StellarFake(create="noid")))
check("twice -> review, not failed", ws16e.col(2, sb.H_STATUS) == sb.ST_REVIEW
      and not site.reports("failed") and len(f16e.creates) == 2)

# ── 17. busy is not no ───────────────────────────────────────────────────────
print("\n17. Stellar answers 503/429 -> the order waits, and the outage heals itself")
site, ws17, _, _ = scenario([order()], (f17 := StellarFake(create=503)))
check("nothing written, nothing reported", len(ws17.rows) == 1
      and not site.reports("failed") and not site.reports("fulfilled"))
check("asked once, not replayed", len(f17.creates) == 1)
check("one alert an hour", len(alerts) == 1 and "busy" in alerts[0][0], str(alerts))
site, _, _, _ = scenario([order()], StellarFake(create=429), now=NOW_MID)
check("...held mid-hour", not alerts)
print("\n   the next run asks again under the SAME key")
site, ws17, _, _ = scenario([order()], (f17b := StellarFake()), ws=ws17)
check("same key, bought once", len(f17b.creates) == 1
      and f17b.creates[0][0] == "waverole-WR-TEST01-a0", str(f17b.creates))

# ── 18. a refusal IS a refusal ───────────────────────────────────────────────
print("\n18. Stellar refuses the request (4xx) -> failed, and the key is spent")
site, ws18, _, _ = scenario([order()], (f18 := StellarFake(create=422)))
check("asked once -- a refusal is not replayed", len(f18.creates) == 1)
check("site told failed, with the reason", len(site.reports("failed")) == 1
      and "422" in site.reports("failed")[0]["reason"], str(site.reports("failed")))
check("a failed row is written", ws18.col(2, sb.H_STATUS).startswith(sb.ST_FAILED),
      ws18.col(2, sb.H_STATUS))
check("the alert may say the wallet is untouched -- this one is certain",
      alerts and "untouched" in alerts[0][1], str(alerts))
print("\n   the retry buys under a NEW key")
site, ws18, _, _ = scenario([order()], (f18b := StellarFake()), ws=ws18)
check("-a1, and delivered", f18b.creates[0][0] == "waverole-WR-TEST01-a1"
      and len(site.reports("fulfilled")) == 1, str(f18b.creates))

# ── 19. an unknown answer poisons everything after it ────────────────────────
# The replay's answer only ever covers the REPLAY. Nothing it says can prove
# the FIRST request created nothing -- so once one answer has been silent about
# the money, 'failed' is off the table for the rest of the call.
print("\n19. unknown, then a refusal -> still unknown, never 'failed'")
site, ws19, _, _ = scenario([order()], (f19 := StellarFake(create=[500, 422])))
check("the 422 covers the replay, not the 500 before it -- so no failed report",
      not site.reports("failed"), str(site.posts))
check("review row instead", ws19.col(2, sb.H_STATUS) == sb.ST_REVIEW)
check("asked twice under one key", len(f19.creates) == 2
      and {k for k, _ in f19.creates} == {"waverole-WR-TEST01-a0"})
print("\n   ...but a 4xx on the FIRST answer still earns the word")
site, ws19b, _, _ = scenario([order()], StellarFake(create=400))
check("reported failed", len(site.reports("failed")) == 1)

print("\n   an answer about our KEY is not an answer about the order")
for code in (401, 403):
    site, ws19c, _, _ = scenario([order()], (f19c := StellarFake(create=code)))
    check(f"{code}: the order is told nothing and keeps its place in the queue",
          not site.reports("failed") and len(ws19c.rows) == 1 and len(f19c.creates) == 1)
    check(f"{code}: the alert points at the key, not the order",
          alerts and "key" in alerts[0][0], str(alerts))

# ── 20. a row that names no Stellar order is never re-bought ─────────────────
# order_rows() reads the link cell as the sheet RENDERS it. A reformatted link,
# or a cell the sheet shows as a hyperlink LABEL (memory: variant-cell-
# rendering), parses to nothing -- and a row that already spent money must not
# become "no attempt was made".
print("\n20. a row that already spent money but names no order is not bought again")
for label, link in (("no id in the link", ""), ("the sheet rendered a label", "label")):
    ws20 = Ws([receipt_row("WR-TEST01", sb.ST_PROCESSING, stellar_id="")])
    if link == "label":
        ws20.rows[1][HDR.index("Link - esim.dog")] = "Stellar order so-1"
    site, ws20, _, _ = scenario([order()], (f20 := StellarFake()), ws=ws20)
    check(f"nothing bought ({label})", len(f20.creates) == 0 and len(ws20.rows) == 2)
    check(f"...and the owner is told how to unstick it ({label})",
          alerts and "cannot be settled" in alerts[0][0], str(alerts))

# ── 21. which write happens first, when a run is killed between them ─────────
print("\n21. the receipts row is written BEFORE the site is told")
site, ws21, _, _ = scenario([order()], StellarFake(), fail_report=True)
check("the site never got the report", not site.reports("fulfilled"))
check("...but the row is already פעיל, so the next run settles from it",
      ws21.col(2, sb.H_STATUS) == sb.ST_ACTIVE and ws21.col(2, "Activation Code") == LPA)

# ── 22. an order an earlier run paid for and never finished ──────────────────
print("\n22. money spent, still not provisioned -> the owner is told")
ws22 = Ws([receipt_row("WR-TEST01", sb.ST_PROCESSING)])
site, ws22, _, _ = scenario([order()], (f22 := StellarFake(statuses=("processing",))), ws=ws22)
check("nothing bought, nothing reported", len(f22.creates) == 0
      and not site.reports("failed") and not site.reports("fulfilled"))
check("one alert, naming the row", alerts and "still processing" in alerts[0][0], str(alerts))
site, _, _, _ = scenario([order()], StellarFake(statuses=("processing",)),
                         ws=Ws([receipt_row("WR-TEST01", sb.ST_PROCESSING)]), now=NOW_MID)
check("...held mid-hour", not alerts)
print("\n   fulfilled at Stellar but the key cannot read the credentials is the same story")
site, _, _, _ = scenario([order()], StellarFake(esims_read=False), settle_wait=0)
check("alerted, not delivered", alerts and "still fulfilled" in alerts[0][0], str(alerts))
print("\n   ...and a normal wait right after the purchase says nothing")
site, _, _, _ = scenario([order()], StellarFake(statuses=("processing",)), settle_wait=0)
check("no alert", not alerts, str(alerts))

# ── 23. the alert window must be wider than the gap between runs ─────────────
# A */5 schedule that Actions delays by four minutes lands on :04, :09, :14 --
# never inside a three-minute window at the top of the hour, so a persistent
# fault would have gone unreported for days.
print("\n23. the hourly window survives a delayed cron")
check("a run at :04 still alerts", not sb._quiet_hour(datetime(2026, 9, 10, 4, 4, tzinfo=fb.TZ)))
check("a run at :09 still alerts", not sb._quiet_hour(datetime(2026, 9, 10, 4, 9, tzinfo=fb.TZ)))
check("a run at :10 is held", sb._quiet_hour(datetime(2026, 9, 10, 4, 10, tzinfo=fb.TZ)))
check("a run at :30 is held", sb._quiet_hour(datetime(2026, 9, 10, 4, 30, tzinfo=fb.TZ)))

# ── 24. a portal link a person pasted by hand still names its order ─────────
# The unknown-outcome alert TELLS the owner to paste a link into the row. A
# copied url carries a trailing slash, a ?tab= or a #fragment, and an anchored
# regex read every one of those as "no order" -- the reading that costs money.
print("\n24. a hand-pasted portal link still settles")
for label, link in (("trailing slash", "https://wholesale.stellarsecurity.com/orders/so-1/"),
                    ("query string", "https://wholesale.stellarsecurity.com/orders/so-1?tab=esims"),
                    ("fragment", "https://wholesale.stellarsecurity.com/orders/so-1#top")):
    ws24 = Ws([receipt_row("WR-TEST01", sb.ST_PROCESSING)])
    ws24.rows[1][HDR.index("Link - esim.dog")] = link
    site, ws24, _, _ = scenario([order()], (f24 := StellarFake()), ws=ws24)
    check(f"settled from the existing order, nothing bought ({label})",
          len(f24.creates) == 0 and len(site.reports("fulfilled")) == 1, str(alerts))

# ── 25. a broken catalogue read must not refuse a single order ──────────────
# An empty page or a renamed id format makes every coded row 'vanish'. Without
# a guard choose() refuses each one and reports it 'failed', spending one of
# the site's retry attempts on every queued order over a fault that is ours.
print("\n25. a catalogue read that loses most rows buys nothing and blames nobody")
many_rows = [sheet_row()] + [sheet_row(sku=f"1.62.{i}", code=f"JC0{i}") for i in range(4)]
site, ws25, bought, _ = scenario([order(), order(oid="WR-TWO")], (f25 := StellarFake()),
                                 rows=many_rows, plans=[])
check("nothing bought, no row, and NO order reported failed",
      bought == 0 and len(ws25.rows) == 1 and not site.reports("failed"), str(site.posts))
check("one alert about the catalogue, not one per order",
      len(alerts) == 1 and "catalogue" in alerts[0][0], str(alerts))

# ── 26. a connection that dropped AFTER the request is not a refusal ─────────
print("\n26. which connection errors can have spent money")
import requests as _rq
check("the server hung up mid-answer -> it was sent",
      sb.reached_stellar(_rq.ConnectionError("RemoteDisconnected('Remote end closed connection')")))
check("could not connect at all -> it never left",
      not sb.reached_stellar(_rq.ConnectionError(
          "HTTPSConnectionPool: Max retries exceeded (Caused by NewConnectionError(...))")))
check("dns did not resolve -> it never left",
      not sb.reached_stellar(_rq.ConnectionError("Name or service not known")))

# ── 27. the row that proves money was spent survives another writer ─────────
# n is READ and then written, and the PC buyer appends to this same sheet.
print("\n27. another bot takes the row number between the read and the write")


class Thief(Ws):
    """Appends a row of its own the instant ours lands on n, once."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.stolen = False

    def update(self, a1, values, value_input_option=None):
        super().update(a1, values, value_input_option)
        if not self.stolen:
            self.stolen = True
            n = int(a1[1:])
            other = [""] * len(HDR)
            other[HDR.index("מס׳ הזמנה")] = "WR-OTHER"
            self.rows[n - 1] = other        # the other writer got there first


ws27 = Thief()
site, ws27, _, _ = scenario([order()], (f27 := StellarFake()), ws=ws27)
check("the other bot's row is not overwritten", ws27.col(2, "מס׳ הזמנה") == "WR-OTHER")
check("...and ours is written again, further down",
      ws27.col(3, "מס׳ הזמנה") == "WR-TEST01" and ws27.col(3, sb.H_STATUS) == sb.ST_ACTIVE,
      str(ws27.rows[2][:8]))

# ── summary (last, so it gates the exit code) ────────────────────────────────
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed:")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ all checks passed")
