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
import sys
import types
import unittest
from unittest import mock

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

        self.confirm_works = True
        self.p = [
            mock.patch.object(fb, "send_customer_email", fake_send),
            mock.patch.object(fb, "report_email_sent", fake_confirm),
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

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def row_values(self, n):
        return list(self.rows[n - 1])

    def update_cells(self, cells, value_input_option=None):
        for c in cells:
            self.cells.append((c.row, HDR[c.col - 1], c.value))
            self.rows[c.row - 1][c.col - 1] = c.value


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

    def test_process_tells_the_owner_and_touches_nothing(self):
        ws = Sheet([rrow("WR-4YWATY", link=fb.SUPERSEDED_SESSION + SID_A, status=REPLACED_CHECK,
                         sell="0.00$")])
        inbox, alerts, flagged = mock.Mock(), [], []
        inbox.flag = flagged.append
        with mock.patch.object(fb, "alert", lambda s, b: alerts.append((s, b))), \
             mock.patch.object(fb, "fetch_esim_details", side_effect=AssertionError("no lookup")), \
             mock.patch.object(fb, "report_fulfilled", side_effect=AssertionError("no site")), \
             mock.patch.object(fb, "send_customer_email", side_effect=AssertionError("no mail")):
            fb.process(inbox, ws, delivery(SID_A))
        self.assertEqual(flagged, ["7"])
        self.assertEqual(len(alerts), 1)
        self.assertIn("WR-4YWATY", alerts[0][0])
        self.assertEqual(ws.cells, [])


class TestTheSiteCanWithholdTheMail(Harness):
    """The site answers late_delivery / customer_mail:false when the order was
    already replaced, or already holds a different eSIM (api/orders.js)."""

    def _run(self, answer, facts=None):
        ws = Sheet([rrow("WR-OLDONE", link=URL + SID_B)])
        inbox, alerts, flagged = mock.Mock(), [], []
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
        self.assertEqual(len(alerts), 1)
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
        inbox = mock.Mock()
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
             mock.patch.object(fb, "report_email_sent", lambda *a, **k: True):
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
             mock.patch.object(fb, "report_email_sent", lambda *a, **k: True):
            fb.process(mock.Mock(), ws, delivery(SID_B))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
