#!/usr/bin/env python3
"""The buyer must get their eSIM exactly once.

Replays the 2026-09-02 incident: order WR-SCKMCC was mailed at 14:01 and again
at 14:13. The send worked both times — what failed in between was the POST that
tells the site "this one is done", so the ledger still called the order owed and
the next sweep sent a second copy of the same eSIM.

Two rules, and they pull against each other on purpose:
  * never mail an order that is already in Sent  (no duplicates)
  * when we cannot READ Sent, mail it anyway     (no silent no-shows)
"""
import os
import sys
import types
import unittest
from unittest import mock

# Offline by construction: with no token, every site call fb makes raises
# before a socket is opened -- so a test that forgot a patch fails loudly
# instead of POSTing to the live site from a developer's shell.
os.environ.pop("ORDERS_TOKEN", None)

import fulfillment_bot as fb


class FakeInbox:
    """Stands in for the mailbox. `sent` is what is already in Sent."""

    def __init__(self, sent=(), broken=False):
        self.sent = {(o, a.lower()) for o, a in sent}
        self.broken = broken
        self.closed = False
        self.asked = []

    def already_mailed_to(self, order_id, to):
        self.asked.append((order_id, to))
        if self.broken:
            # What the real one does when IMAP is unreachable: answers "no"
            # rather than withholding the eSIM.
            return False
        return (order_id, to.lower()) in self.sent

    def close(self):
        self.closed = True


class Harness(unittest.TestCase):
    def setUp(self):
        self.sends = []          # (to, order_id)
        self.confirms = []       # (order_id, ok)

        def fake_send(to, order_id, *a, **k):
            self.sends.append((to, order_id))

        def fake_confirm(order_id, ok, error="", address=""):
            self.confirms.append((order_id, ok))
            return self.confirm_works

        def fake_confirm_lease(order_id, ok, error="", address="", lease=""):
            self.confirms.append((order_id, ok))
            if lease:
                self.leases.append((order_id, ok, lease))
            return self.confirm_works

        self.confirm_works = True
        self.leases = []
        # The site's mail lease (2026-10-21). Default: a site from before it,
        # which answers 400 -- every guard below it must hold on its own.
        self.claim_answer = {"claim": None, "reason": "old_site"}
        self.claims = []

        def fake_claim(order_id, by, ttl_s=900):
            self.claims.append((order_id, by))
            return dict(self.claim_answer)
        self.p = [
            mock.patch.object(fb, "send_customer_email", fake_send),
            mock.patch.object(fb, "report_email_sent", fake_confirm_lease),
            mock.patch.object(fb, "claim_mail", fake_claim),
        ]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def sweep(self, owed, inbox, sheet_rows=None):
        ws = types.SimpleNamespace(
            get_all_values=lambda: sheet_rows or [["מייל - Mail", "מס׳ הזמנה"]])
        with mock.patch.object(fb, "awaiting_email_orders", lambda: owed), \
             mock.patch.object(fb, "_open_inbox",
                               lambda attempts=1: inbox or (_ for _ in ()).throw(
                                   OSError("imap down"))):
            fb.deliver_pending_emails(ws)


OWED = [{"order_id": "WR-SCKMCC", "customer_email": "chensharony321@gmail.com",
         "order_url": "", "lang": "he", "esim": {"plan": "10GB - 25 days"}}]


class TestNoDuplicate(Harness):
    def test_the_incident_does_not_repeat(self):
        """Mailed once, confirmation lost: the sweep must close, not resend."""
        inbox = FakeInbox(sent=[("WR-SCKMCC", "chensharony321@gmail.com")])
        self.sweep(OWED, inbox)
        self.assertEqual(self.sends, [], "buyer was mailed a second eSIM")
        self.assertEqual(self.confirms, [("WR-SCKMCC", True)])

    def test_case_differs_between_site_and_sent(self):
        """The site says Chensharony321@, Sent holds chensharony321@."""
        inbox = FakeInbox(sent=[("WR-SCKMCC", "chensharony321@gmail.com")])
        owed = [dict(OWED[0], customer_email="Chensharony321@gmail.com")]
        self.sweep(owed, inbox)
        self.assertEqual(self.sends, [])

    def test_still_not_resent_when_the_ledger_will_not_close(self):
        """A stale ledger row is cheaper than a second eSIM."""
        self.confirm_works = False
        inbox = FakeInbox(sent=[("WR-SCKMCC", "chensharony321@gmail.com")])
        self.sweep(OWED, inbox)
        self.assertEqual(self.sends, [])

    def test_a_second_real_order_is_not_confused_with_the_first(self):
        """WR-VJ8K9P is a different order to the same buyer — it must go out."""
        inbox = FakeInbox(sent=[("WR-SCKMCC", "chensharony321@gmail.com")])
        owed = [dict(OWED[0], order_id="WR-VJ8K9P")]
        self.sweep(owed, inbox)
        self.assertEqual([o for _, o in self.sends], ["WR-VJ8K9P"])


class TestStillDelivers(Harness):
    def test_never_mailed_still_gets_mailed(self):
        inbox = FakeInbox(sent=[])
        self.sweep(OWED, inbox)
        self.assertEqual(self.sends,
                         [("chensharony321@gmail.com", "WR-SCKMCC")])

    def test_unreadable_sent_folder_delivers_rather_than_withholds(self):
        """A duplicate beats a no-show: the eSIM is the buyer's only copy."""
        inbox = FakeInbox(broken=True)
        self.sweep(OWED, inbox)
        self.assertEqual(len(self.sends), 1)

    def test_no_mailbox_at_all_still_delivers(self):
        self.sweep(OWED, None)
        self.assertEqual(len(self.sends), 1)

    def test_address_comes_from_the_sheet_when_the_site_has_none(self):
        rows = [["מייל - Mail", "מס׳ הזמנה"],
                ["chensharony321@gmail.com", "WR-SCKMCC"]]
        owed = [dict(OWED[0], customer_email="")]
        inbox = FakeInbox(sent=[])
        self.sweep(owed, inbox, sheet_rows=rows)
        self.assertEqual(self.sends,
                         [("chensharony321@gmail.com", "WR-SCKMCC")])

    def test_mailbox_is_closed_afterwards(self):
        inbox = FakeInbox(sent=[])
        self.sweep(OWED, inbox)
        self.assertTrue(inbox.closed, "IMAP connection leaked")


class TestAlertIsNotMistakenForTheCustomerCopy(unittest.TestCase):
    """An alert naming the order is also from us and also names it in the
    subject. Counting one as the buyer's copy would withhold the eSIM."""

    def _inbox(self, subjects):
        inbox = fb.Inbox.__new__(fb.Inbox)
        inbox._mailed = {}
        inbox.box = types.SimpleNamespace(
            uid=lambda *a, **k: ("OK", [b"1"]))
        inbox._headers = lambda uids: {
            u: {"Subject": s} for u, s in zip(uids, subjects)}
        return inbox

    def test_only_an_alert_means_not_mailed(self):
        inbox = self._inbox([fb.ALERT_PREFIX + "Order WR-SCKMCC is STUCK"])
        self.assertFalse(inbox.already_mailed_to("WR-SCKMCC", "owner@x.com"))

    def test_a_real_customer_copy_counts(self):
        inbox = self._inbox(["ה-eSIM שלך מוכן לשימוש! ✈ הזמנה WR-SCKMCC"])
        self.assertTrue(inbox.already_mailed_to("WR-SCKMCC", "owner@x.com"))

    def test_an_order_id_that_could_break_the_search_is_not_searched(self):
        inbox = self._inbox(["anything"])
        self.assertFalse(inbox.already_mailed_to('WR" OR "1', "owner@x.com"))


# ── 2026-10-20: superseded rows, exact sessions, late packages ──────────────
# WR-4YWATY / WR-T3QD3Q (receipts rows 188-189, 2026-10-11): the owner closed
# a checkout mid-payment, the order was bought again, and the abandoned
# attempt's row stayed behind — order number, no eSIM, so "pending" to the
# matcher, and a candidate for the next Europe 10GB delivery for twelve hours.
# The PC bot now marks such a row (Status 'replaced...', Sell 0, its session
# neutralised); these pin that the delivery side never writes onto one.

from datetime import datetime, timedelta, timezone  # noqa: E402

REPLACED = fb.REPLACED_STATUS
REPLACED_CHECK = fb.REPLACED_CHECK_STATUS
HDR = ["\u05de\u05d9\u05d9\u05dc - Mail", "\u05ea\u05d0\u05e8\u05d9\u05da - Date", "\u05d0\u05d9\u05d7\u05e1\u05d5\u05df - GB", "\u05de\u05e1\u05f3 \u05d4\u05d6\u05de\u05e0\u05d4", "QR", "Activation Code",
       "Link - esim.dog", "Link - waverole", "\u05de\u05e1 \u05e1\u05d9\u05d3\u05d5\u05e8\u05d9 -ICCID", "\u05e1\u05d8\u05d8\u05d5\u05e1 - Status",
       "\u05e7\u05e0\u05d9\u05d9\u05d4 - Buy", "\u05d4\u05e0\u05d7\u05d4 - Sale", "\u05de\u05db\u05d9\u05e8\u05d4 - Sell", "\u05e8\u05db\u05d9\u05e9\u05d4 - Purchase"]
H = {h: i for i, h in enumerate(HDR)}
SID_A = "cs_live_a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuVwXyZ0123456789abcdefABCDEF"
SID_B = "cs_live_b9Z8y7X6w5V4u3T2s1R0qPoNmLkJiHgFeDcBa9876543210zyxwvuZYXWVU"
URL = "https://esim.dog/success?session_id="
WHEN = datetime(2026, 10, 11, 17, 10, tzinfo=timezone.utc)      # 20:10 Israel


def rrow(order, link="", status="\u05e4\u05e2\u05d9\u05dc", buy="3.86$", sell="6.99$",
         date="11/10/2026 19:55:43", gb="10GB", act="", iccid="", mail="c@x.com"):
    r = [""] * len(HDR)
    r[H["\u05de\u05d9\u05d9\u05dc - Mail"]], r[H["\u05ea\u05d0\u05e8\u05d9\u05da - Date"]], r[H["\u05d0\u05d9\u05d7\u05e1\u05d5\u05df - GB"]] = mail, date, gb
    r[H["\u05de\u05e1\u05f3 \u05d4\u05d6\u05de\u05e0\u05d4"]], r[H["Link - esim.dog"]] = order, link
    r[H["Link - waverole"]] = "https://www.waverole.com/?order=e30"
    r[H["\u05e1\u05d8\u05d8\u05d5\u05e1 - Status"]], r[H["\u05e7\u05e0\u05d9\u05d9\u05d4 - Buy"]], r[H["\u05de\u05db\u05d9\u05e8\u05d4 - Sell"]] = status, buy, sell
    r[H["Activation Code"]], r[H["\u05de\u05e1 \u05e1\u05d9\u05d3\u05d5\u05e8\u05d9 -ICCID"]] = act, iccid
    r[H["\u05d4\u05e0\u05d7\u05d4 - Sale"]], r[H["\u05e8\u05db\u05d9\u05e9\u05d4 - Purchase"]] = "-", "paypal"
    return r


class Sheet:
    def __init__(self, rows):
        self.rows = [list(HDR)] + [list(r) for r in rows]
        self.cells = []
        self.formats = []

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def row_values(self, n):
        return list(self.rows[n - 1])

    def update_cells(self, cells, value_input_option=None):
        for c in cells:
            self.cells.append((c.row, HDR[c.col - 1], c.value))
            self.rows[c.row - 1][c.col - 1] = c.value

    def format(self, a1, fmt):
        self.formats.append((a1, fmt))


def delivery(sid=SID_B, gb=10.0, when=WHEN):
    return {"uid": "7", "success_url": URL + sid, "success_urls": [URL + sid], "gb": gb,
            "days": 30, "location": "Europe", "network": "", "received_at": when}


class TestTheMatcherNeverWritesOntoADeadRow(unittest.TestCase):
    def test_row_188_as_it_will_be_marked_is_never_a_candidate(self):
        # Same size, same destination, inside the window: before, this row
        # took the delivery. Marked replaced (Buy and Sell zeroed), never.
        ws = Sheet([rrow("WR-4YWATY", status=REPLACED, buy="0.00$", sell="0.00$")])
        self.assertIsNone(fb.find_pending_row(ws, delivery()))

    def test_replaced_check_the_charge_is_not_a_candidate_either(self):
        ws = Sheet([rrow("WR-4YWATY", status=REPLACED_CHECK, sell="0.00$")])
        self.assertIsNone(fb.find_pending_row(ws, delivery()))

    def test_a_placeholder_zeroed_by_hand_is_dead_but_a_blank_one_is_not(self):
        ws = Sheet([rrow("WR-4YWATY", buy="0$", sell="0$")])
        self.assertIsNone(fb.find_pending_row(ws, delivery()))
        ws = Sheet([rrow("WR-4YWATY", buy="", sell="")])
        self.assertEqual(fb.find_pending_row(ws, delivery())["order_id"], "WR-4YWATY")

    def test_the_live_row_still_matches_beside_a_dead_one(self):
        ws = Sheet([rrow("WR-4YWATY", status=REPLACED, buy="0.00$", sell="0.00$"),
                    rrow("WR-T3QD3Q", date="11/10/2026 20:00:49")])
        self.assertEqual(fb.find_pending_row(ws, delivery())["order_id"], "WR-T3QD3Q")

    def test_the_uncertain_placeholder_is_matched_by_its_session(self):
        # The PC bot marks an uncertain-payment placeholder's link with a
        # fragment; the session id stops at '#', so the identity still holds.
        ws = Sheet([rrow("WR-UNSURE", link=URL + SID_B + "#payment-uncertain")])
        m = fb.find_pending_row(ws, delivery())
        self.assertEqual((m["row"], m["order_id"]), (2, "WR-UNSURE"))

    def test_the_exact_session_beats_the_twelve_hour_guess(self):
        # An older look-alike with no session sits first in the sheet and
        # would win the time-ordered guess; the row naming the mail's own
        # session is the answer.
        ws = Sheet([rrow("WR-OLDER1", date="11/10/2026 12:00:00"),
                    rrow("WR-EXACT1", link=URL + SID_B, date="11/10/2026 19:59:00")])
        self.assertEqual(fb.find_pending_row(ws, delivery())["order_id"], "WR-EXACT1")

    def test_a_row_waiting_for_its_own_session_is_not_handed_another(self):
        # Row names session A; the mail is about B. The size-and-time guess
        # used to give it B's package.
        ws = Sheet([rrow("WR-OWN001", link=URL + SID_A)])
        self.assertIsNone(fb.find_pending_row(ws, delivery(SID_B)))

    def test_only_a_clear_contradiction_counts(self):
        # A mail-wrapped link reads as a prefix of the real id: same purchase.
        ws = Sheet([rrow("WR-WRAP01", link=URL + SID_B)])
        self.assertEqual(fb.find_pending_row(ws, delivery(SID_B[:40]))["order_id"], "WR-WRAP01")
        # The two id formats seen in real mail cannot be compared: no veto.
        ws = Sheet([rrow("WR-PI0001", link="https://esim.dog/success?payment_intent=pi_3Abc")])
        self.assertEqual(fb.find_pending_row(ws, delivery(SID_B))["order_id"], "WR-PI0001")

    def test_a_superseded_session_is_never_read_as_a_session(self):
        ws = Sheet([rrow("WR-4YWATY", link=fb.SUPERSEDED_SESSION + SID_B, status=REPLACED_CHECK,
                         sell="0.00$")])
        self.assertIsNone(fb.find_pending_row(ws, delivery(SID_B)))


class TestALatePackageForAReplacedOrder(unittest.TestCase):
    def test_found_by_its_neutralised_session(self):
        ws = Sheet([rrow("WR-4YWATY", link=fb.SUPERSEDED_SESSION + SID_A, status=REPLACED_CHECK,
                         sell="0.00$"),
                    rrow("WR-T3QD3Q", link=URL + SID_B, act="LPA:1$x$y", iccid="'8900")])
        got = fb.find_replaced_row(ws, delivery(SID_A))
        self.assertEqual((got["row"], got["order_id"]), (2, "WR-4YWATY"))
        self.assertIsNone(fb.find_replaced_row(ws, delivery(SID_B)))

    def test_a_row_marked_by_hand_keeps_its_session_and_is_still_found(self):
        ws = Sheet([rrow("WR-4YWATY", link=URL + SID_A, status=REPLACED)])
        self.assertEqual(fb.find_replaced_row(ws, delivery(SID_A))["row"], 2)

    def test_the_live_row_of_the_same_number_is_named(self):
        ws = Sheet([rrow("WR-4YWATY", link=fb.SUPERSEDED_SESSION + SID_A, status=REPLACED_CHECK,
                         sell="0.00$"),
                    rrow("WR-4YWATY", link=URL + SID_B, act="LPA:1$x$B", iccid="'8900B"),
                    rrow("WR-OTHER1", link=URL + SID_B)])
        self.assertEqual(fb.find_replaced_row(ws, delivery(SID_A))["live_rows"], [3])


# The same-number re-buy of the review (2026-10-20): row 2 is the first,
# uncertain checkout (session A), marked replaced by the PC; row 3 is the
# re-buy (session B), filled in and live. A's delivery mail arrives. Whose
# eSIM is it? Only the site's record can say.
GOT_A = {"activation_code": "LPA:1$smdp$CODEA", "iccid": "8972000000000000AAA", "used_url": URL + SID_A}


class TestWhoseLatePackageIsIt(unittest.TestCase):
    def _sheet(self):
        r1 = rrow("WR-4YWATY", link=fb.SUPERSEDED_SESSION + SID_A, status=REPLACED_CHECK, sell="0.00$")
        r1[H["\u05d4\u05e0\u05d7\u05d4 - Sale"]] = ""
        r2 = rrow("WR-4YWATY", link=URL + SID_B, act="LPA:1$smdp$CODEB", iccid="'8972000000000000BBB",
                  date="11/10/2026 20:00:49")
        return Sheet([r1, r2])

    def _run(self, facts, answer=None, details=GOT_A, when=WHEN, ws=None, park=None):
        ws = ws or self._sheet()
        inbox, alerts, flagged, posts = mock.Mock(), [], [], []
        inbox.flag = flagged.append
        self.parks = []

        def report(oid, d, det):
            posts.append((oid, det.get("activation_code")))
            if isinstance(answer, Exception):
                raise answer
            return answer if answer is not None else {"ok": True, "order_id": oid, "status": "fulfilled"}

        def park_extra(oid, sid, det, via, supplier="esim.dog", supplier_ref=""):
            self.parks.append((oid, sid, det.get("activation_code"), det.get("iccid"), via))
            if isinstance(park, Exception):
                raise park
            return park if park is not None else {"ok": True, "parked": True, "customer_mail": False}
        with mock.patch.object(fb, "alert", lambda s, b: alerts.append((s, b))), \
             mock.patch.object(fb, "order_facts", lambda oid: dict(facts)), \
             mock.patch.object(fb, "fetch_esim_details", lambda urls: dict(details)), \
             mock.patch.object(fb, "report_fulfilled", report), \
             mock.patch.object(fb, "site_park_extra", park_extra), \
             mock.patch.object(fb, "send_customer_email", side_effect=AssertionError("no mail")), \
             mock.patch.object(fb, "report_email_sent", side_effect=AssertionError("no ledger")):
            fb.process(inbox, ws, delivery(SID_A, when=when))
        return ws, alerts, flagged, posts

    FULFILLED = {"order_id": "WR-4YWATY", "status": "fulfilled", "esim_ready": True, "has_session": True}

    def col(self, ws, n, h):
        return ws.rows[n - 1][H[h]]

    def test_the_site_shows_THIS_package_so_the_rows_swap(self):
        ws, alerts, flagged, posts = self._run(self.FULFILLED)
        self.assertEqual(posts, [("WR-4YWATY", GOT_A["activation_code"])])
        # Row 2 is the sale now: the eSIM, active, the money and the rail of row 3.
        self.assertEqual(self.col(ws, 2, "Activation Code"), GOT_A["activation_code"])
        self.assertEqual(self.col(ws, 2, "\u05e1\u05d8\u05d8\u05d5\u05e1 - Status"), "\u05e4\u05e2\u05d9\u05dc")
        self.assertEqual(self.col(ws, 2, "\u05de\u05db\u05d9\u05e8\u05d4 - Sell"), "6.99$")
        self.assertEqual(self.col(ws, 2, "\u05e8\u05db\u05d9\u05e9\u05d4 - Purchase"), "paypal")
        self.assertEqual(self.col(ws, 2, "Link - esim.dog"), URL + SID_A, "the clean link replaces the defused one")
        self.assertIn(("A2", {"backgroundColor": fb.PAID_BG}), ws.formats)
        # Row 3 is the spare: not a sale, white, its cost kept.
        self.assertEqual(self.col(ws, 3, "\u05e1\u05d8\u05d8\u05d5\u05e1 - Status"), REPLACED_CHECK)
        self.assertEqual(self.col(ws, 3, "\u05de\u05db\u05d9\u05e8\u05d4 - Sell"), "0.00$")
        self.assertEqual(self.col(ws, 3, "\u05e7\u05e0\u05d9\u05d9\u05d4 - Buy"), "3.86$")
        self.assertEqual(self.col(ws, 3, "Link - esim.dog"), fb.SUPERSEDED_SESSION + SID_B)
        self.assertIn(("A3", {"backgroundColor": fb.WHITE_BG}), ws.formats)
        self.assertEqual(flagged, ["7"])
        self.assertEqual(len(alerts), 1)
        self.assertIn("rows swapped", alerts[0][0])
        self.assertIn("row 3", alerts[0][1])

    def test_a_zeroed_buy_is_called_out_when_the_package_turns_out_charged(self):
        ws = self._sheet()
        ws.rows[1][H["\u05e7\u05e0\u05d9\u05d9\u05d4 - Buy"]] = "0.00$"
        ws.rows[1][H["\u05e1\u05d8\u05d8\u05d5\u05e1 - Status"]] = REPLACED
        _, alerts, _, _ = self._run(self.FULFILLED, ws=ws)
        self.assertIn("Buy says 0", alerts[0][1])

    def test_the_site_holds_ANOTHER_esim_so_this_one_is_the_spare(self):
        ws, alerts, flagged, posts = self._run(
            self.FULFILLED, answer={"ok": True, "status": "fulfilled", "late_delivery": True,
                                    "customer_mail": False})
        self.assertEqual(len(posts), 1)
        self.assertEqual(ws.cells, [], "the sheet is left as the PC wrote it")
        self.assertEqual(ws.formats, [])
        self.assertEqual(flagged, ["7"])
        # 2026-10-21: the site parked it as an extra; the stock shelf's alert
        # is the owner's one mail about it, so nothing is said from here.
        self.assertEqual(alerts, [])
        self.assertEqual(self.parks, [], "the site already holds it - nothing more to report")

    def test_replaced_by_another_order_is_a_spare_without_asking_again(self):
        ws, alerts, flagged, posts = self._run({"status": "settled", "replaced_by": "WR-T3QD3Q",
                                                "has_session": False})
        self.assertEqual((posts, ws.cells, flagged), ([], [], ["7"]))
        # Reported to the site as an extra of its order, with its session and
        # package, so the stock shelf takes it - no alert from here.
        self.assertEqual(self.parks, [("WR-4YWATY", SID_A, GOT_A["activation_code"], GOT_A["iccid"],
                                       "cloud:replaced-row")])
        self.assertEqual(alerts, [])

    def test_no_esim_and_not_waiting_is_a_spare(self):
        ws, alerts, flagged, posts = self._run({"status": "failed", "esim_ready": False, "has_session": True})
        self.assertEqual((posts, ws.cells, flagged, alerts), ([], [], ["7"], []))
        self.assertEqual(len(self.parks), 1)

    def test_a_spare_the_site_cannot_be_told_about_is_still_told_to_the_owner(self):
        ws, alerts, flagged, posts = self._run({"status": "settled", "replaced_by": "WR-T3QD3Q",
                                                "has_session": False}, park=OSError("502"))
        self.assertEqual((ws.cells, flagged), ([], ["7"]))
        self.assertEqual(len(alerts), 1)
        self.assertIn("WR-T3QD3Q", alerts[0][1])
        self.assertIn("NOT on the order's extras", alerts[0][1])

    def test_a_spare_the_site_calls_the_customer_s_own_is_never_shelved(self):
        ws, alerts, flagged, posts = self._run({"status": "failed", "esim_ready": False, "has_session": True},
                                               park={"error": "is_serving"})
        self.assertEqual(flagged, ["7"])
        self.assertIn("never be shelved", alerts[0][1])

    def test_an_order_still_waiting_is_asked_again_not_parked(self):
        fresh = datetime.now(timezone.utc) - timedelta(minutes=10)
        ws, alerts, flagged, posts = self._run({"status": "failed", "esim_ready": False, "has_session": True},
                                               park={"error": "order_waiting"}, when=fresh)
        self.assertEqual((flagged, alerts), ([], []))

    def test_a_spare_with_no_details_yet_waits_for_the_supplier(self):
        fresh = datetime.now(timezone.utc) - timedelta(minutes=10)
        ws, alerts, flagged, posts = self._run({"status": "settled", "replaced_by": "WR-T3QD3Q"},
                                               details={}, when=fresh)
        self.assertEqual((self.parks, flagged, alerts), ([], [], []))

    def test_an_esim_on_a_record_that_is_not_fulfilled_is_not_posted_to(self):
        ws, alerts, flagged, posts = self._run({"status": "settled", "esim_ready": True, "has_session": True})
        self.assertEqual((posts, ws.cells, flagged), ([], [], ["7"]))
        self.assertIn("decide by hand", alerts[0][0])

    def test_a_pending_order_is_asked_again_while_the_mail_is_fresh(self):
        fresh = datetime.now(timezone.utc) - timedelta(minutes=10)
        ws, alerts, flagged, posts = self._run({"status": "pending", "esim_ready": False,
                                                "has_session": True}, when=fresh)
        self.assertEqual((posts, ws.cells, flagged, alerts), ([], [], [], []))

    def test_then_handed_to_the_owner_once_the_grace_is_over(self):
        ws, alerts, flagged, posts = self._run({"status": "pending", "esim_ready": False, "has_session": True})
        self.assertEqual((posts, ws.cells, flagged), ([], [], ["7"]))
        self.assertIn("decide by hand", alerts[0][0])

    def test_an_unreadable_site_never_decides_blind(self):
        fresh = datetime.now(timezone.utc) - timedelta(minutes=10)
        ws, alerts, flagged, posts = self._run({}, when=fresh)
        self.assertEqual((posts, ws.cells, flagged, alerts), ([], [], [], []))

    def test_a_site_without_the_second_package_guard_is_never_posted_to(self):
        # Before f10f1d4 a different eSIM reported to a fulfilled order
        # REPLACED the one on the customer's page.
        facts = {k: v for k, v in self.FULFILLED.items() if k != "has_session"}
        ws, alerts, flagged, posts = self._run(facts)
        self.assertEqual((posts, ws.cells, flagged), ([], [], ["7"]))
        self.assertIn("decide by hand", alerts[0][0])

    def test_no_activation_code_means_no_comparison(self):
        ws, alerts, flagged, posts = self._run(self.FULFILLED, details={"iccid": "8972000000000000AAA"})
        self.assertEqual((posts, ws.cells, flagged), ([], [], ["7"]))

    def test_a_failed_report_is_retried_not_guessed(self):
        fresh = datetime.now(timezone.utc) - timedelta(minutes=10)
        ws, alerts, flagged, posts = self._run(self.FULFILLED, answer=OSError("502"), when=fresh)
        self.assertEqual((len(posts), ws.cells, flagged, alerts), (1, [], [], []))


class TestSupersedeHelpers(unittest.TestCase):
    def test_a_parked_spare_row_is_no_longer_green(self):
        ws = Sheet([rrow("WR-OLDONE", link=URL + SID_B, act="LPA:1$a$b")])
        ch = fb.mark_row_superseded(ws, 2)
        self.assertEqual(ws.formats, [("A2", {"backgroundColor": fb.WHITE_BG})])
        self.assertIn((fb.H_MAIL + " (fill)", "?", "white"), ch)
        self.assertEqual(ws.rows[1][H["Link - esim.dog"]], URL + SID_B, "a spare keeps its package page")

    def test_placeholders_of_the_order_only(self):
        ws = Sheet([rrow("WR-X00001", link=URL + SID_A + "#payment-uncertain"),     # placeholder -> marked
                    rrow("WR-X00001", link=URL + SID_B, act="LPA:1$a$b"),           # has an eSIM -> kept
                    rrow("WR-X00001", status=REPLACED, buy="0.00$", sell="0.00$"),  # already dead -> kept
                    rrow("WR-Y00002")])                                            # other order -> kept
        before = [list(r) for r in ws.rows]
        out = fb.supersede_placeholders(ws, "wr-x00001", keep={99})
        self.assertEqual(sorted({r for r, *_ in out}), [2])
        self.assertEqual(ws.rows[1][H["Link - esim.dog"]], fb.SUPERSEDED_SESSION + SID_A)
        self.assertEqual(ws.rows[1][H["\u05de\u05db\u05d9\u05e8\u05d4 - Sell"]], "0.00$")
        self.assertEqual(ws.rows[2:], before[2:])

    def test_a_row_the_caller_keeps_is_never_touched(self):
        ws = Sheet([rrow("WR-X00001")])
        self.assertEqual(fb.supersede_placeholders(ws, "WR-X00001", keep={2}), [])
        self.assertEqual(ws.cells, [])


class TestTheSiteCanWithholdTheMail(Harness):
    """The site answers late_delivery / customer_mail:false when the order was
    already replaced, or already holds a different eSIM (api/orders.js)."""

    def _run(self, answer, facts=None, sent=(), ws=None):
        ws = ws or Sheet([rrow("WR-OLDONE", link=URL + SID_B)])
        inbox, alerts, flagged = FakeInbox(sent=sent), [], []
        inbox.flag = flagged.append
        got = {"activation_code": "LPA:1$smdp$CODE", "iccid": "8972000000000000001",
               "used_url": URL + SID_B}
        with mock.patch.object(fb, "alert", lambda s, b: alerts.append((s, b))), \
             mock.patch.object(fb, "find_replaced_row", lambda ws, d: None), \
             mock.patch.object(fb, "fetch_esim_details", lambda urls: dict(got)), \
             mock.patch.object(fb, "report_fulfilled", lambda oid, d, det: answer), \
             mock.patch.object(fb, "order_facts", lambda oid: dict(facts or {})):
            fb.process(inbox, ws, delivery(SID_B))
        return ws, alerts, flagged

    def test_late_delivery_means_no_mail_and_no_ledger_close(self):
        ws, alerts, flagged = self._run({"ok": True, "status": "settled",
                                         "late_delivery": True, "customer_mail": False})
        self.assertEqual(self.sends, [])
        self.assertEqual(self.confirms, [], "closing the ledger would cost the real eSIM's mail")
        self.assertEqual(flagged, ["7"])
        # The stock shelf's alert is the one (2026-10-21): none from here
        # while the row could be marked.
        self.assertEqual(alerts, [])
        # The row keeps the package (the spare's record) but stops being a sale.
        status = ws.rows[1][H["\u05e1\u05d8\u05d8\u05d5\u05e1 - Status"]]
        self.assertEqual(status, REPLACED_CHECK)
        self.assertEqual(ws.rows[1][H["\u05de\u05db\u05d9\u05e8\u05d4 - Sell"]], "0.00$")
        self.assertEqual(ws.rows[1][H["\u05e7\u05e0\u05d9\u05d9\u05d4 - Buy"]], "3.86$", "the charge is real")
        self.assertTrue(ws.rows[1][H["Activation Code"]])

    def test_customer_mail_false_alone_is_enough(self):
        self._run({"ok": True, "status": "fulfilled", "customer_mail": False})
        self.assertEqual(self.sends, [])

    def test_an_older_site_answer_mails_as_before(self):
        ws, alerts, _ = self._run({"ok": True, "order_id": "WR-OLDONE", "status": "fulfilled"})
        self.assertEqual(self.sends, [("c@x.com", "WR-OLDONE")])
        self.assertEqual(self.confirms, [("WR-OLDONE", True)])
        self.assertEqual(alerts, [])
        self.assertEqual(ws.rows[1][H["\u05e1\u05d8\u05d8\u05d5\u05e1 - Status"]], "\u05e4\u05e2\u05d9\u05dc")


class TestTheImmediateMailKnowsGiftsAndReplacements(unittest.TestCase):
    def test_facts_from_the_site_reach_the_mail(self):
        ws = Sheet([rrow("WR-NEW001", link=URL + SID_B)])
        inbox = FakeInbox()
        inbox.flag = lambda uid: None
        seen = {}

        def send(to, oid, url, d, esim=None, lang="", total=None, free=None, replaces=""):
            seen.update(free=free, replaces=replaces)
        with mock.patch.object(fb, "find_replaced_row", lambda ws, d: None), \
             mock.patch.object(fb, "fetch_esim_details", lambda urls: {"activation_code": "LPA:1$a$b"}), \
             mock.patch.object(fb, "report_fulfilled", lambda oid, d, det: {"ok": True}), \
             mock.patch.object(fb, "order_facts", lambda oid: {
                 "order_id": oid, "paid_usd": 0, "free": True, "source": "manual",
                 "replaces": "WR-4YWATY"}), \
             mock.patch.object(fb, "send_customer_email", send), \
             mock.patch.object(fb, "report_email_sent", lambda *a, **k: True), \
             mock.patch.object(fb, "claim_mail", lambda *a, **k: {"claim": None}):
            fb.process(inbox, ws, delivery(SID_B))
        self.assertEqual(seen, {"free": True, "replaces": "WR-4YWATY"})

    def test_an_unreadable_record_leaves_the_link_to_decide(self):
        ws = Sheet([rrow("WR-NEW001", link=URL + SID_B)])
        seen = {}

        def send(to, oid, url, d, esim=None, lang="", total=None, free=None, replaces=""):
            seen.update(free=free, replaces=replaces)
        with mock.patch.object(fb, "find_replaced_row", lambda ws, d: None), \
             mock.patch.object(fb, "fetch_esim_details", lambda urls: {"activation_code": "LPA:1$a$b"}), \
             mock.patch.object(fb, "report_fulfilled", lambda oid, d, det: {"ok": True}), \
             mock.patch.object(fb, "order_facts", lambda oid: {}), \
             mock.patch.object(fb, "send_customer_email", send), \
             mock.patch.object(fb, "report_email_sent", lambda *a, **k: True), \
             mock.patch.object(fb, "claim_mail", lambda *a, **k: {"claim": None}):
            box = FakeInbox()
            box.flag = lambda uid: None
            fb.process(box, ws, delivery(SID_B))
        self.assertEqual(seen, {"free": None, "replaces": ""})


class TestAlreadyCompleted(unittest.TestCase):
    def test_a_completed_row_with_the_uncertain_mark_still_counts(self):
        ws = Sheet([rrow("WR-DONE01", link=URL + SID_B + "#payment-uncertain",
                         act="LPA:1$a$b", iccid="'8972")])
        self.assertTrue(fb.already_completed(ws, [URL + SID_B]))
        self.assertFalse(fb.already_completed(ws, [URL + SID_A]))


class TestTheSweepPassesGiftAndReplacement(Harness):
    def test_the_awaiting_row_s_facts_reach_the_mail(self):
        seen = []

        def send(to, oid, url, d, esim=None, lang="", total=None, free=None, replaces=""):
            seen.append((oid, total, free, replaces))
        owed = [dict(OWED[0], order_id="WR-GIFT01", paid_usd=0, free=True),
                dict(OWED[0], order_id="WR-NEW001", paid_usd=6.99, replaces="WR-4YWATY"),
                dict(OWED[0], order_id="WR-FAKE01", paid_usd=5, free=True)]
        with mock.patch.object(fb, "send_customer_email", send):
            self.sweep(owed, FakeInbox(sent=[]))
        self.assertEqual(seen, [("WR-GIFT01", 0, True, ""),
                                ("WR-NEW001", 6.99, False, "WR-4YWATY"),
                                ("WR-FAKE01", 5, False, "")])


# ── 2026-10-21: ONE email per order, whoever sends it ───────────────────────
# The owner's rule: a second PACKAGE for an order is acceptable (stock shelf +
# one alert); a second EMAIL for one order is not. Every automatic sender now
# goes through fb.send_once: the site's sent stamp, its mail lease, the Sent
# folder, then the send — and the report hands the lease back.

class TestSendOnce(Harness):
    def _send(self, inbox=None, facts=None, to="c@x.com", oid="WR-ONE001", raises=None):
        def send():
            if raises:
                raise raises
            self.sends.append((to, oid))
        return fb.send_once(inbox, oid, to, send, by="cloud:test", facts=facts if facts is not None else {})

    def test_a_lease_held_elsewhere_means_no_mail_and_no_report(self):
        self.claim_answer = {"claim": False, "reason": "claimed", "by": "cloud:sweep"}
        self.assertEqual(self._send(FakeInbox()), "claimed")
        self.assertEqual((self.sends, self.confirms), ([], []))

    def test_the_site_says_it_went_no_mail(self):
        self.claim_answer = {"claim": False, "reason": "sent"}
        self.assertEqual(self._send(FakeInbox()), "already")
        self.assertEqual(self.sends, [])
        self.assertEqual(self._send(FakeInbox(), facts={"customer_email_sent_ts": "2026-10-21T08:00:00Z"}), "already")
        self.assertEqual(self.claims[-1:], [("WR-ONE001", "cloud:test")], "the stamp alone stops it, before any lease")

    def test_a_parked_or_closed_order_is_owed_no_mail(self):
        self.claim_answer = {"claim": False, "reason": "not_owed"}
        self.assertEqual(self._send(FakeInbox()), "withheld")
        self.assertEqual(self.sends, [])

    def test_a_granted_lease_is_handed_back_with_the_report(self):
        self.claim_answer = {"claim": True, "lease": "cloud:test#ab12"}
        self.assertEqual(self._send(FakeInbox()), "sent")
        self.assertEqual(self.sends, [("c@x.com", "WR-ONE001")])
        self.assertEqual(self.leases, [("WR-ONE001", True, "cloud:test#ab12")])

    def test_a_failed_send_hands_the_lease_back_and_keeps_the_ledger_open(self):
        self.claim_answer = {"claim": True, "lease": "cloud:test#ab12"}
        self.assertEqual(self._send(FakeInbox(), raises=OSError("smtp 421")), "failed")
        self.assertEqual(self.leases, [("WR-ONE001", False, "cloud:test#ab12")])

    def test_an_old_site_without_a_lease_still_mails_on_the_other_guards(self):
        self.assertEqual(self._send(FakeInbox()), "sent")
        self.assertEqual(len(self.sends), 1)

    def test_sent_folder_still_wins_under_a_lease(self):
        self.claim_answer = {"claim": True, "lease": "cloud:test#ab12"}
        self.assertEqual(self._send(FakeInbox(sent=[("WR-ONE001", "c@x.com")])), "already")
        self.assertEqual(self.sends, [])
        self.assertEqual(self.leases, [("WR-ONE001", True, "cloud:test#ab12")], "closed, lease given back")

    def test_an_inbox_is_opened_only_when_a_mail_is_due(self):
        opened = []

        def lazy():
            opened.append(1)
            return FakeInbox()
        self.claim_answer = {"claim": False, "reason": "claimed"}
        self._send(lazy)
        self.assertEqual(opened, [], "a refused lease never costs a mailbox connection")
        self.claim_answer = {"claim": None}
        self._send(lazy)
        self.assertEqual(opened, [1])

    def test_no_address_reports_and_never_claims(self):
        self.assertEqual(self._send(FakeInbox(), to=""), "failed")
        self.assertEqual((self.claims, self.confirms), ([], [("WR-ONE001", False)]))

    def test_the_real_inbox_remembers_a_send_made_in_this_run(self):
        box = fb.Inbox.__new__(fb.Inbox)
        box._mailed = {("WR-ONE001", "c@x.com"): False}   # asked before the send: "not yet"
        box.note_sent("WR-ONE001", "C@x.com")
        self.assertTrue(box.already_mailed_to("WR-ONE001", "c@x.com"))

    def test_two_real_orders_of_one_person_are_two_mails(self):
        self.assertEqual(self._send(FakeInbox(sent=[("WR-ONE001", "c@x.com")]), oid="WR-TWO002"), "sent")


class TestTheLateDeliveryMailOfAServedOrder(Harness):
    """G2 (2026-10-21). The PC hands the session to the site, the site
    completes the order, the ledger sweep mails the customer — then the
    supplier's delivery mail lands a run later and matches the still-pending
    receipts row. process() used to mail the customer a second time."""

    def _run(self, answer, facts, sent=()):
        ws = Sheet([rrow("WR-SERVED", link=URL + SID_B)])
        inbox, flagged, alerts = FakeInbox(sent=sent), [], []
        inbox.flag = flagged.append
        got = {"activation_code": "LPA:1$smdp$CODE", "iccid": "8972000000000000001", "used_url": URL + SID_B}
        with mock.patch.object(fb, "alert", lambda s, b: alerts.append((s, b))), \
             mock.patch.object(fb, "find_replaced_row", lambda ws, d: None), \
             mock.patch.object(fb, "fetch_esim_details", lambda urls: dict(got)), \
             mock.patch.object(fb, "report_fulfilled", lambda oid, d, det: dict(answer)), \
             mock.patch.object(fb, "order_facts", lambda oid: dict(facts)):
            fb.process(inbox, ws, delivery(SID_B))
        return ws, flagged, alerts

    def test_the_site_says_mail_owed_false(self):
        ws, flagged, alerts = self._run({"ok": True, "status": "fulfilled", "mail_owed": False}, {})
        self.assertEqual((self.sends, self.claims, alerts, flagged), ([], [], [], ["7"]))
        # The row is the sale and is now complete: NOT marked as a spare.
        self.assertEqual(ws.rows[1][H["\u05e1\u05d8\u05d8\u05d5\u05e1 - Status"]], "\u05e4\u05e2\u05d9\u05dc")
        self.assertEqual(ws.rows[1][H["\u05de\u05db\u05d9\u05e8\u05d4 - Sell"]], "6.99$")

    def test_an_old_site_answer_but_the_record_is_stamped(self):
        ws, flagged, _ = self._run({"ok": True, "status": "fulfilled"},
                                   {"customer_email_sent_ts": "2026-10-21T08:00:00Z"})
        self.assertEqual(self.sends, [])
        self.assertEqual(flagged, ["7"])

    def test_no_stamp_anywhere_but_it_is_in_sent(self):
        ws, flagged, _ = self._run({"ok": True, "status": "fulfilled"}, {}, sent=[("WR-SERVED", "c@x.com")])
        self.assertEqual(self.sends, [])
        self.assertEqual(self.confirms, [("WR-SERVED", True)], "the ledger is closed instead")

    def test_a_lease_held_by_the_sweep_at_this_minute(self):
        self.claim_answer = {"claim": False, "reason": "claimed", "by": "cloud:sweep"}
        self._run({"ok": True, "status": "fulfilled", "mail_owed": True}, {})
        self.assertEqual(self.sends, [])

    def test_the_first_mail_of_an_order_still_goes_at_once(self):
        self._run({"ok": True, "status": "fulfilled", "mail_owed": True}, {})
        self.assertEqual(self.sends, [("c@x.com", "WR-SERVED")])
        self.assertEqual(self.claims, [("WR-SERVED", "cloud:delivery")])


class TestParkedVersusAlreadyMailed(unittest.TestCase):
    def test_only_a_park_marks_the_row(self):
        self.assertTrue(fb.site_parked({"late_delivery": True}))
        self.assertTrue(fb.site_parked({"customer_mail": False}))
        self.assertFalse(fb.site_parked({"mail_owed": False}), "already mailed is not a spare")
        self.assertTrue(fb.site_withholds_mail({"mail_owed": False}))
        self.assertFalse(fb.site_withholds_mail({"ok": True, "status": "fulfilled"}))

    def test_the_report_names_the_delivery_mail_s_session(self):
        seen = {}

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True}

        def post(url, json=None, timeout=None, headers=None):
            seen.update(json)
            return R()
        with mock.patch.object(fb, "env", lambda name: "t"), mock.patch.object(fb.requests, "post", post):
            fb.report_fulfilled("WR-ONE001", delivery(SID_B), {"activation_code": "LPA:1$a$b"})
        self.assertEqual(seen.get("supplier_session"), SID_B)


class TestTheExtraPackagePost(unittest.TestCase):
    def test_the_session_travels_as_sid_never_as_a_handover(self):
        seen = {}

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True, "parked": True}

        def post(url, json=None, timeout=None, headers=None):
            seen.update(json)
            return R()
        with mock.patch.object(fb, "env", lambda name: "t"), mock.patch.object(fb.requests, "post", post):
            out = fb.site_park_extra("WR-4YWATY", SID_A, dict(GOT_A), via="cloud:replaced-row")
        self.assertEqual(out.get("parked"), True)
        self.assertEqual((seen.get("action"), seen.get("sid"), "session_id" in seen, "status" in seen),
                         ("extra_package", SID_A, False, False))
        self.assertEqual(seen["esim"]["iccid"], GOT_A["iccid"])


class TestTheSaleComesBackFromTheSite(unittest.TestCase):
    """G4 (2026-10-21). S2's delivery mail is processed FIRST: the site parks
    it, its row is marked as the spare (Sell 0). Then S1's mail arrives, the
    site says S1 IS the customer's eSIM, and the rows swap — with no live row
    left to copy the Sell, Sale and Purchase from."""

    def test_sell_sale_and_rail_from_the_site_s_record(self):
        r1 = rrow("WR-4YWATY", link=fb.SUPERSEDED_SESSION + SID_A, status=REPLACED_CHECK, sell="0.00$")
        r1[H["\u05d4\u05e0\u05d7\u05d4 - Sale"]] = ""
        r1[H["\u05e8\u05db\u05d9\u05e9\u05d4 - Purchase"]] = ""
        r2 = rrow("WR-4YWATY", link=URL + SID_B, act="LPA:1$smdp$CODEB", iccid="'8972000000000000BBB",
                  status=REPLACED_CHECK, sell="0.00$", date="11/10/2026 20:00:49")   # the parked S2
        ws = Sheet([r1, r2])
        facts = {"order_id": "WR-4YWATY", "status": "fulfilled", "esim_ready": True, "has_session": True,
                 "paid_usd": 6.99, "list_usd": 7.99, "source": "paypal"}
        inbox, alerts, flagged = mock.Mock(), [], []
        inbox.flag = flagged.append
        with mock.patch.object(fb, "alert", lambda s, b: alerts.append((s, b))), \
             mock.patch.object(fb, "order_facts", lambda oid: dict(facts)), \
             mock.patch.object(fb, "fetch_esim_details", lambda urls: dict(GOT_A)), \
             mock.patch.object(fb, "report_fulfilled", lambda oid, d, det: {"ok": True, "status": "fulfilled",
                                                                            "mail_owed": False}), \
             mock.patch.object(fb, "send_customer_email", side_effect=AssertionError("no mail")):
            fb.process(inbox, ws, delivery(SID_A))
        self.assertEqual(ws.rows[1][H["\u05de\u05db\u05d9\u05e8\u05d4 - Sell"]], "6.99$")
        self.assertEqual(ws.rows[1][H["\u05d4\u05e0\u05d7\u05d4 - Sale"]], "13% ($1.00 \u05d4\u05e0\u05d7\u05d4)")
        self.assertEqual(ws.rows[1][H["\u05e8\u05db\u05d9\u05e9\u05d4 - Purchase"]], "paypal")
        self.assertIn(("A2", {"backgroundColor": fb.PAID_BG}), ws.formats)
        self.assertNotIn("NOT restored", alerts[0][1])
        self.assertIn("from the site's record", alerts[0][1])

    def test_a_gift_and_an_unreadable_record(self):
        self.assertEqual(fb.sale_cells_from_facts({"paid_usd": 0, "free": True, "source": "manual"}),
                         {fb.H_SELL: "0.00$", fb.H_PURCHASE: fb.FREE_RAIL})
        self.assertEqual(fb.sale_cells_from_facts({}), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
