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


if __name__ == "__main__":
    unittest.main(verbosity=2)
