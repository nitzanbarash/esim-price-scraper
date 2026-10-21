#!/usr/bin/env python3
"""The stock shelf: every second package exactly once on the stock tab, and
exactly one alert per package (stock_shelf.py, 2026-10-21).

The owner's rule since 2026-10-21: a package bought twice for one order is
acceptable -- it goes to the 'Stock' tab as a free spare, the next order for
the same SKU is served from it, and he is told once. These pin the four ways
that could go wrong:
  * a package shelved twice (two runs, a lost answer from the site) is served
    to two customers;
  * the customer's OWN eSIM shelved is sold to the next customer;
  * an alert per run instead of per package trains the owner to ignore them;
  * a Stellar package, which has no ICCID, written as if it had one.

Offline: the site and the spreadsheet are fakes with the real shapes.
Run: python test_stock_shelf.py
"""
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.pop("ORDERS_TOKEN", None)       # no fake may ever fall through to the live site

import fulfillment_bot as fb               # noqa: E402
import stock_shelf as ss                   # noqa: E402

SID_X = "cs_live_x9Y8w7V6u5T4s3R2q1P0oNmLkJiHgFeDcBa9876543210zyxwvuZYXWVUabcd"
SID_S = "cs_live_s1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz0123456789abcdefABCDEFgh"
URL = "https://esim.dog/success?session_id="
NOW = datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc)


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


class Site:
    """GET ?status=extras and POST extra_shelved, as api/orders.js answers."""
    def __init__(self, orders, status=200, fail_marks=False, locked_by="", lock_status=200):
        self.orders, self.status, self.fail_marks = orders, status, fail_marks
        self.marks, self.gets = [], []
        # The shelf lock (shelf_lock / shelf_unlock): held by another run when
        # locked_by is set; lock_status 400 = a site from before the lock.
        self.locked_by, self.lock_status, self.locks = locked_by, lock_status, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append(dict(params or {}))
        if self.status != 200:
            return Resp(self.status, {"error": "status must be ..."})
        # The site drops an extra from the list once it is marked.
        out = []
        for o in self.orders:
            left = [x for x in o["extras"] if not any(
                (m.get("iccid") and m.get("iccid") == x.get("iccid")) or
                (m.get("supplier_ref") and m.get("supplier_ref") == x.get("supplier_ref")) for m in self.marks)]
            if left:
                out.append({**o, "extras": left})
        return Resp(200, {"orders": out})

    def post(self, url, json=None, headers=None, timeout=None):
        act = (json or {}).get("action")
        if act in ("shelf_lock", "shelf_unlock"):
            self.locks.append(dict(json))
            if self.lock_status != 200:
                return Resp(self.lock_status, {"error": "status must be ..."})
            if act == "shelf_unlock":
                ok = json.get("lease") == "L1" and self.locked_by == json.get("lease")
                if ok:
                    self.locked_by = ""
                return Resp(200, {"ok": True, "released": ok})
            if self.locked_by:
                return Resp(200, {"ok": True, "lock": False, "by": self.locked_by.split("#")[0]})
            self.locked_by = "L1"
            return Resp(200, {"ok": True, "lock": True, "lease": "L1"})
        if self.fail_marks:
            import requests
            raise requests.ConnectionError("site unreachable")
        self.marks.append(dict(json))
        return Resp(200, {"ok": True, "shelved": True})


class Tab:
    def __init__(self, rows=None, header=None):
        self.rows = [list(header or ss.HEADER)] + [list(r) for r in (rows or [])]

    def row_values(self, n):
        return list(self.rows[n - 1]) if n <= len(self.rows) else []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def update(self, range_name=None, values=None, value_input_option=None):
        n = int(range_name[1:])
        while len(self.rows) < n:
            self.rows.append([""] * len(self.rows[0]))
        self.rows[n - 1] = list(values[0])

    def col(self, n, h):
        return self.rows[n - 1][self.rows[0].index(h)]


RHDR = ["\u05de\u05d9\u05d9\u05dc - Mail", fb.H_ORDER, ss.R_SKU, ss.R_GB, ss.R_PLAN, ss.R_REGION,
        fb.H_LINK_SUP, fb.H_QR, fb.H_STATUS, fb.H_BUY, fb.H_SELL]


def receipts(*rows):
    return Tab(rows, header=RHDR)


def rrow(order, link, status, buy="3.86$", sku="2.0.10"):
    r = [""] * len(RHDR)
    for h, v in ((fb.H_ORDER, order), (ss.R_SKU, sku), (ss.R_GB, "10GB"), (ss.R_PLAN, "10GB - 30 days"),
                 (ss.R_REGION, "Europe"), (fb.H_LINK_SUP, link), (fb.H_STATUS, status), (fb.H_BUY, buy),
                 (fb.H_SELL, "0.00$")):
        r[RHDR.index(h)] = v
    return r


class Book:
    def __init__(self, tab=None, sheet1=None):
        self.tab, self.sheet1, self.added = tab, sheet1 or receipts(), []

    def worksheet(self, name):
        if name != ss.STOCK_TAB or self.tab is None:
            raise LookupError(name)
        return self.tab

    def add_worksheet(self, title, rows, cols):
        self.added.append(title)
        self.tab = Tab(rows=[], header=[""])
        self.tab.rows = [[]]
        return self.tab


def extra_order(iccid="8972000000000005678", sid=SID_X, distinct=True, **over):
    o = {"order_id": "WR-TWICE1", "sku": "2.0.10", "supplier": "esim.dog", "status": "fulfilled",
         "serving_iccid_tail": "001234", "serving_sid_tail": SID_S[-10:],
         "sessions": [{"sid_tail": SID_X[-10:], "ts": "2026-10-21T08:01:00Z", "attempt": "first", "state": "extra"},
                      {"sid_tail": SID_S[-10:], "ts": "2026-10-21T08:12:00Z", "attempt": "auto_rebuy",
                       "state": "serving"}],
         "extras": [{"sid": sid, "iccid": iccid, "ts": "2026-10-21T09:05:00Z", "via": "sweep",
                     "supplier": "esim.dog", "supplier_ref": "", "buy_price": "3.86", "plan": "10GB - 30d",
                     "distinct": distinct}]}
    o.update(over)
    return o


class Harness(unittest.TestCase):
    def run_shelf(self, site, book, receipts_ws=None):
        self.alerts = []
        with mock.patch.object(ss.requests, "get", site.get), \
             mock.patch.object(ss.requests, "post", site.post), \
             mock.patch.object(fb, "env", lambda name: "test-token"), \
             mock.patch.object(fb, "alert", lambda s, b: self.alerts.append((s, b))):
            return ss.shelve_extras(book, receipts_ws=receipts_ws, now=NOW)


class TestOneRowPerExtra(Harness):
    def test_the_second_package_is_shelved_with_its_receipts_cost(self):
        site = Site([extra_order()])
        book = Book(Tab(), receipts(rrow("WR-TWICE1", fb.SUPERSEDED_SESSION + SID_X, fb.REPLACED_CHECK_STATUS),
                                    rrow("WR-TWICE1", URL + SID_S, "\u05e4\u05e2\u05d9\u05dc", buy="3.91$")))
        got = self.run_shelf(site, book)
        tab = book.tab
        self.assertEqual(got, [{"order_id": "WR-TWICE1", "row": 2, "iccid_tail": "5678"}])
        self.assertEqual(tab.col(2, ss.H_ICCID), "8972000000000005678")
        self.assertEqual(tab.col(2, ss.H_SESSION), SID_X, "the bare session: the PC builds the link")
        self.assertEqual(tab.col(2, ss.H_STATUS), ss.FREE)
        self.assertEqual(tab.col(2, ss.H_COST), "3.86$", "the receipts row of THAT purchase, not the sale's")
        self.assertEqual(tab.col(2, ss.H_SKU), "2.0.10")
        self.assertIn("auto: second package of WR-TWICE1 (receipts row 2)", tab.col(2, ss.H_ORIGIN))
        self.assertIn("10GB", tab.col(2, ss.H_PLAN))
        self.assertEqual(tab.col(2, ss.H_ADDED), "21/10/2026 12:30:00", "Israel time, the PC's format")
        self.assertEqual(site.marks, [{"action": "extra_shelved", "order_id": "WR-TWICE1", "sid": SID_X,
                                       "iccid": "8972000000000005678", "shelf_row": 2}])

    def test_exactly_one_alert_with_tails_only(self):
        site = Site([extra_order()])
        book = Book(Tab(), receipts(rrow("WR-TWICE1", fb.SUPERSEDED_SESSION + SID_X, fb.REPLACED_CHECK_STATUS)))
        self.run_shelf(site, book)
        self.assertEqual(len(self.alerts), 1)
        subject, body = self.alerts[0]
        self.assertIn("WR-TWICE1: a second package was bought - it is on the stock shelf", subject)
        self.assertIn("ONE eSIM", body)
        self.assertIn("...1234", body)
        self.assertIn("...5678", body)
        self.assertIn("row 2, free", body)
        self.assertIn("$3.86 (receipts row 2", body)
        self.assertIn("python -m bot.spares remove 5678 refund requested", body)
        self.assertIn("automatic re-buy", body)
        self.assertNotIn("8972000000000005678", body, "never a full ICCID in a mail")
        self.assertNotIn(SID_X, body, "never a full session in a mail")

    def test_a_rerun_appends_nothing_and_alerts_nothing(self):
        tab = Tab()
        book = Book(tab, receipts())
        self.run_shelf(Site([extra_order()]), book)
        self.assertEqual(len(tab.rows), 2)
        # The site lost the mark (or a second run read the list first).
        again = Site([extra_order()])
        self.run_shelf(again, book)
        self.assertEqual(len(tab.rows), 2, "the same ICCID is never shelved twice")
        self.assertEqual(self.alerts, [], "and the owner is not told twice")
        self.assertEqual(again.marks[0]["shelf_row"], 2, "the site is told the row it already has")

    def test_a_lost_mark_heals_on_the_next_run(self):
        tab = Tab()
        book = Book(tab, receipts())
        self.run_shelf(Site([extra_order()], fail_marks=True), book)
        self.assertEqual((len(tab.rows), len(self.alerts)), (2, 1))
        healed = Site([extra_order()])
        self.run_shelf(healed, book)
        self.assertEqual((len(tab.rows), len(self.alerts), len(healed.marks)), (2, 0, 1))

    def test_a_package_the_owner_shelved_by_hand_is_not_added_again(self):
        row = [""] * len(ss.HEADER)
        row[0] = "8972 0000 0000 000 5678"
        tab = Tab([row])
        site = Site([extra_order()])
        self.run_shelf(site, Book(tab, receipts()))
        self.assertEqual((len(tab.rows), self.alerts, site.marks[0]["shelf_row"]), (2, [], 2))

    def test_no_receipts_row_uses_the_site_s_buy_price(self):
        tab = Tab()
        self.run_shelf(Site([extra_order()]), Book(tab, receipts()))
        self.assertEqual(tab.col(2, ss.H_COST), "3.86$")
        self.assertIn("(no receipts row)", tab.col(2, ss.H_ORIGIN))
        self.assertEqual(tab.col(2, ss.H_PLAN), "10GB - 30d")


class TestTheCustomersOwnPackageIsNeverStock(Harness):
    def test_an_iccid_ending_like_the_serving_one_is_refused(self):
        tab = Tab()
        site = Site([extra_order(iccid="8972000000000001234")])
        got = self.run_shelf(site, Book(tab, receipts()))
        self.assertEqual((got, len(tab.rows)), ([], 1))
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("NOT put on the stock shelf", self.alerts[0][0])
        self.assertEqual(site.marks[0].get("refused"), "same_as_serving", "and it is not offered again")

    def test_an_extra_the_site_could_not_prove_different_is_refused(self):
        tab = Tab()
        site = Site([extra_order(distinct=False)])
        self.run_shelf(site, Book(tab, receipts()))
        self.assertEqual(len(tab.rows), 1)
        self.assertEqual(site.marks[0].get("refused"), "not_distinct")


class TestStellar(Harness):
    def test_a_stellar_package_is_named_by_its_order_and_served_by_hand(self):
        o = extra_order(iccid="", sid="", supplier="stellar")
        o["extras"][0].update(supplier="stellar", supplier_ref="so-77aa", buy_price="")
        tab = Tab()
        book = Book(tab, receipts(rrow("WR-TWICE1", "https://wholesale.stellarsecurity.com/orders/so-77aa",
                                       fb.REPLACED_CHECK_STATUS, buy="2.48$")))
        self.run_shelf(Site([o]), book)
        self.assertEqual(tab.col(2, ss.H_ICCID), "", "Stellar never returns an ICCID - none is invented")
        self.assertEqual(tab.col(2, ss.H_SESSION), "stellar:so-77aa")
        self.assertEqual(tab.col(2, ss.H_NOTE), ss.STELLAR_NOTE)
        self.assertEqual(tab.col(2, ss.H_COST), "2.48$")
        self.assertIn("Stellar", self.alerts[0][1])
        # ...and a second run finds it by its Session.
        self.run_shelf(Site([o]), book)
        self.assertEqual((len(tab.rows), self.alerts), (2, []))


class TestTheEdges(Harness):
    def test_an_old_site_shelves_nothing(self):
        tab = Tab()
        self.assertEqual(self.run_shelf(Site([extra_order()], status=400), Book(tab, receipts())), [])
        self.assertEqual((len(tab.rows), self.alerts), (1, []))

    def test_an_empty_list_costs_one_get_and_nothing_else(self):
        site = Site([])
        book = Book(None, receipts())
        self.assertEqual(self.run_shelf(site, book), [])
        self.assertEqual((len(site.gets), book.added), (1, []))

    def test_a_renamed_column_writes_nothing_and_says_so(self):
        hdr = list(ss.HEADER)
        hdr[ss.HEADER.index(ss.H_STATUS)] = "Status"
        tab = Tab(header=hdr)
        self.run_shelf(Site([extra_order()]), Book(tab, receipts()))
        self.assertEqual(len(tab.rows), 1)
        self.assertIn("stock tab cannot be written", self.alerts[0][0])

    def test_a_missing_tab_is_created_with_the_pc_bot_s_header(self):
        book = Book(None, receipts())
        self.run_shelf(Site([extra_order()]), book)
        self.assertEqual(book.added, [ss.STOCK_TAB])
        self.assertEqual(book.tab.rows[0], ss.HEADER)
        self.assertEqual(book.tab.col(2, ss.H_ICCID), "8972000000000005678")

    def test_the_header_is_the_pc_bot_s_own(self):
        # bot/spares.py is not in this repo; on the owner's Mac it sits next
        # to it, and the two spellings must be one (the PC reads by name).
        path = os.path.expanduser("~/Documents/esim-bot/bot/spares.py")
        if not os.path.exists(path):
            self.skipTest("the PC bot is not on this machine")
        import ast
        tree = ast.parse(open(path, encoding="utf-8").read())
        got = {}
        for n in tree.body:
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ("HEADER", "TAB"):
                got[n.targets[0].id] = ast.literal_eval(n.value)
        self.assertEqual(got.get("HEADER"), ss.HEADER)
        self.assertEqual(got.get("TAB"), ss.STOCK_TAB)

    def test_no_hebrew_literal_in_this_module(self):
        src = open(ss.__file__, encoding="utf-8").read()
        self.assertFalse(any("\u0590" <= ch <= "\u05ff" for ch in src), "Hebrew only as \\u escapes")


class TestOneWriterAtATime(Harness):
    """Review 2026-10-21: the fulfillment run and the Stellar buyer shelve from
    two workflows that can overlap; both read the tab before either appended,
    and one ICCID landed on two 'free' rows (two customers, one eSIM)."""

    def test_a_run_that_does_not_get_the_lock_shelves_nothing(self):
        site = Site([extra_order()], locked_by="cloud:stellar#abc")
        book = Book(Tab(), receipts())
        self.assertEqual(self.run_shelf(site, book), [])
        self.assertEqual(len(book.tab.rows), 1, "nothing appended while another run holds the shelf")
        self.assertEqual(site.marks, [])
        self.assertEqual(self.alerts, [])

    def test_the_lock_is_taken_and_given_back_around_the_run(self):
        site = Site([extra_order()])
        book = Book(Tab(), receipts())
        self.assertEqual(len(self.run_shelf(site, book)), 1)
        self.assertEqual([x["action"] for x in site.locks], ["shelf_lock", "shelf_unlock"])
        self.assertEqual(site.locked_by, "", "given back")

    def test_a_package_another_writer_added_mid_run_is_not_added_again(self):
        # The tab is re-read per package: a row that appeared after the run's
        # first look (the owner's hand path, or a site with no lock) is seen.
        site = Site([extra_order()], lock_status=400)
        tab = Tab()
        book = Book(tab, receipts())
        real = ss._tab_rows
        calls = {"n": 0}

        def rows_then_someone_appends(ws):
            calls["n"] += 1
            if calls["n"] == 2:     # between the run's first read and its check
                r = [""] * len(ss.HEADER)
                r[0], r[ss.HEADER.index(ss.H_STATUS)] = "8972000000000005678", ss.FREE
                tab.rows.append(r)
            return real(ws)
        with mock.patch.object(ss, "_tab_rows", rows_then_someone_appends):
            got = self.run_shelf(site, book)
        self.assertEqual(got, [])
        self.assertEqual([r[0] for r in tab.rows[1:]].count("8972000000000005678"), 1)
        self.assertEqual(self.alerts, [], "no second alert for a package already on the shelf")

    def test_a_site_that_cannot_be_asked_for_the_lock_shelves_nothing(self):
        site = Site([extra_order()])

        def boom(url, json=None, headers=None, timeout=None):
            import requests
            raise requests.ConnectionError("down")
        book = Book(Tab(), receipts())
        with mock.patch.object(site, "post", boom):
            self.assertEqual(self.run_shelf(site, book), [])
        self.assertEqual(len(book.tab.rows), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
