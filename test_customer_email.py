#!/usr/bin/env python3
"""What the customer's "your eSIM is ready" mail SAYS about money.

Two kinds of order arrived on 2026-10-20 and the mail had a wrong word for
both of them:

  * a GIFT — a package the owner gives away (the site stores paid_usd 0 and
    free: true). Its 'Total paid' row read "$0.00", which looks like a charge
    that failed rather than a present.
  * a REPLACEMENT — new_order.py --replaces WR-OLD. The customer's first order
    page now forwards to the new one, and a mail with a new order number and
    a price reads like a second purchase. One line says it replaces their
    order and that there is nothing more to pay.

Offline: SMTP is faked and the message is read back, nothing is sent.

Run: python test_customer_email.py
"""
import base64
import json
import os
import smtplib
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("GMAIL_APP_PASSWORD", "test-password")

# Never a real mail server (2026-11-16: an Inbox() in a test once logged in
# to the owner's real Gmail with the fake password). A test that needs SMTP
# patches in its own fake; anything else that tries fails here, offline.
import imaplib  # noqa: E402
import smtplib  # noqa: E402


class _NoMailServer:
    def __init__(self, *a, **k):
        raise OSError("test: no real SMTP/IMAP connection")


smtplib.SMTP = smtplib.SMTP_SSL = _NoMailServer
imaplib.IMAP4_SSL = _NoMailServer

import bot_common  # noqa: E402
import fulfillment_bot as fb  # noqa: E402

# The alert throttle keeps a file in the temp folder; the tests keep their own.
bot_common.THROTTLE_FILE = os.path.join(tempfile.mkdtemp(prefix="wr-test-"), "throttle.json")

FREE_HE = "\u05d7\u05d9\u05e0\u05dd"                         # "Free"
PAID_HE = "\u05e1\u05d4\u05f4\u05db \u05e9\u05d5\u05dc\u05dd"  # "Total paid"
REPLACES_HE = "\u05de\u05d7\u05dc\u05d9\u05e4\u05d4"           # "replaces"


def link(**token):
    body = {"id": "WR-NEW001", "sku": "2.0.10", "ts": 1, "l": "en", **token}
    raw = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    return "https://www.waverole.com/?order=" + raw


class FakeSMTP:
    sent = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def mail(order_url, **kw):
    FakeSMTP.sent.clear()
    with mock.patch.object(fb.smtplib, "SMTP", FakeSMTP):
        fb.send_customer_email("buyer@example.com", "WR-NEW001", order_url,
                               {"gb": 10.0, "days": 30, "location": "Europe", "network": ""},
                               **kw)
    [msg] = FakeSMTP.sent
    html = msg.get_payload()[0].get_payload(decode=True).decode("utf-8")
    return msg, html


def paid_cell(html, label):
    """The value cell beside `label` in the details table."""
    after = html.split(label, 1)[1]
    return after.split("text-align:right\">", 1)[1].split("</td>", 1)[0]


class TotalPaid(unittest.TestCase):
    def test_an_ordinary_order_still_shows_its_price(self):
        _, html = mail(link(t=6.99), total=6.99)
        self.assertEqual(paid_cell(html, "Total paid"), "$6.99")
        self.assertNotIn("replaces your order", html)

    def test_a_gift_says_free(self):
        _, html = mail(link(t=0, fr=1), total=0, free=True)
        self.assertEqual(paid_cell(html, "Total paid"), "Free")

    def test_a_gift_in_hebrew_says_chinam(self):
        _, html = mail(link(t=0, fr=1, l="he"), total=0, free=True, lang="he")
        self.assertEqual(paid_cell(html, PAID_HE), FREE_HE)

    def test_the_link_alone_can_say_it_is_a_gift(self):
        # process() may not reach the site's record; the token's fr:1 decides.
        _, html = mail(link(t=0, fr=1))
        self.assertEqual(paid_cell(html, "Total paid"), "Free")

    def test_an_explicit_no_beats_the_link(self):
        _, html = mail(link(t=0, fr=1), total=0, free=False)
        self.assertEqual(paid_cell(html, "Total paid"), "$0.00")

    def test_free_next_to_a_real_price_shows_the_price(self):
        # A contradiction about money is read as the paid order, never the gift.
        _, html = mail(link(t=5), total=5, free=True)
        self.assertEqual(paid_cell(html, "Total paid"), "$5.00")

    def test_a_zero_without_the_marker_is_not_called_free(self):
        _, html = mail(link(t=0), total=0)
        self.assertEqual(paid_cell(html, "Total paid"), "$0.00")


class ReplacesLine(unittest.TestCase):
    def test_english(self):
        _, html = mail(link(t=6.99), total=6.99, replaces="WR-4YWATY")
        self.assertIn("This order replaces your order", html)
        self.assertIn('<strong dir="ltr">WR-4YWATY</strong>', html)
        self.assertIn("nothing more to pay", html)
        # The price stays: the customer DID pay — once, for the old order.
        self.assertEqual(paid_cell(html, "Total paid"), "$6.99")

    def test_hebrew(self):
        msg, html = mail(link(t=6.99, l="he"), total=6.99, lang="he", replaces="wr-4ywaty")
        self.assertIn(REPLACES_HE, html)
        self.assertIn('<strong dir="ltr">WR-4YWATY</strong>', html)
        self.assertIn('dir="rtl"', html)

    def test_the_subject_still_names_the_new_order(self):
        # already_mailed_to finds the customer's copy by the order id in the
        # subject; a replacement must not change what it searches for.
        msg, _ = mail(link(t=6.99), total=6.99, replaces="WR-4YWATY")
        self.assertIn("WR-NEW001", str(msg["Subject"]))

    def test_nothing_odd_reaches_the_mail(self):
        for bad in ("WR-NEW001", "<script>", "WR-4YWATY\" onclick=x", "12345"):
            _, html = mail(link(t=6.99), total=6.99, replaces=bad)
            self.assertNotIn("replaces your order", html, bad)

    def test_a_gift_that_replaces_says_both(self):
        _, html = mail(link(t=0, fr=1), total=0, free=True, replaces="WR-4YWATY")
        self.assertEqual(paid_cell(html, "Total paid"), "Free")
        self.assertIn("This order replaces your order", html)


class IsFree(unittest.TestCase):
    def test_needs_all_three_facts(self):
        self.assertTrue(fb.is_free({"free": True, "paid_usd": 0, "source": "manual"}))
        self.assertTrue(fb.is_free({"free": True, "paid_usd": 0}))          # awaiting rows
        self.assertFalse(fb.is_free({"paid_usd": 0, "source": "manual"}))  # no marker
        self.assertFalse(fb.is_free({"free": True, "paid_usd": 5}))        # forged
        self.assertFalse(fb.is_free({"free": True, "paid_usd": None}))
        self.assertFalse(fb.is_free({"free": "true", "paid_usd": 0}))      # only a real true
        self.assertFalse(fb.is_free({"free": True, "paid_usd": 0, "source": "paypal"}))


class StaleAlert(unittest.TestCase):
    def test_a_waiting_gift_is_named_a_gift(self):
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        orders = [{"order_id": "WR-GIFT01", "ts": ts, "paid_usd": 0, "free": True,
                   "source": "manual", "sku": "2.0.10", "supplier": "esim.dog",
                   "stale_alert_rung": 0}]
        sent = []
        resp = mock.Mock()
        resp.json.return_value = {"orders": orders}
        with mock.patch.dict(os.environ, {"ORDERS_TOKEN": "t"}), \
             mock.patch.object(fb.requests, "get", lambda *a, **k: resp), \
             mock.patch.object(fb, "_mark_alerted", lambda marks: None), \
             mock.patch.object(fb, "alert", lambda s, b, **k: sent.append(b)):
            fb.check_stale_pending()
        self.assertIn("FREE (gift)", sent[0])
        self.assertNotIn("$0", sent[0])

    def test_a_rung_the_site_could_not_store_is_mailed_once_per_computer(self):
        # 2026-11-16: the PCs run this every 45 s at their quickest. The site
        # did not store the rung (its mark failed) - each run sees it "fresh".
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(minutes=17)).isoformat()
        orders = [{"order_id": "WR-STUCK1", "ts": ts, "paid_usd": 5, "sku": "2.0.10",
                   "supplier": "esim.dog", "stale_alert_rung": 0}]
        sent, marked = [], []
        resp = mock.Mock()
        resp.json.return_value = {"orders": orders}
        with mock.patch.dict(os.environ, {"ORDERS_TOKEN": "t"}), \
             mock.patch.object(fb.requests, "get", lambda *a, **k: resp), \
             mock.patch.object(fb, "_mark_alerted", marked.append), \
             mock.patch.object(fb, "alert", lambda s, b, **k: sent.append((s, k))):
            for _ in range(5):
                fb.check_stale_pending()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][1].get("every_s"), 0, "the ladder is its own throttle")
        self.assertEqual(len(marked), 5, "the site is still told every run")


# ── 2026-11-16: which side of the handover a mail failure fell ──────────────

class FakeLine:
    """smtplib.SMTP enough for _smtp_send: send_message calls self.data, the
    way smtplib's sendmail does, so the handover watch is exercised."""
    fail_at = ""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.fail_at == "quit":
            raise smtplib.SMTPServerDisconnected("gone at QUIT")
        return False

    def starttls(self):
        if self.fail_at == "tls":
            raise ConnectionResetError("reset")

    def login(self, *a):
        if self.fail_at == "login":
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 bad credentials")

    def send_message(self, msg):
        if self.fail_at == "rcpt":
            raise smtplib.SMTPRecipientsRefused({"buyer@example.com": (550, b"5.1.1 no such user")})
        self.data(msg)

    def data(self, msg):
        if self.fail_at == "data":
            raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed: timed out")
        if self.fail_at == "refused":
            raise smtplib.SMTPDataError(554, b"5.7.0 message rejected")
        return 250, b"OK"


def line(fail_at):
    return type("Line", (FakeLine,), {"fail_at": fail_at})


class Handover(unittest.TestCase):
    def send(self, fail_at):
        with mock.patch.object(fb.smtplib, "SMTP", line(fail_at)):
            fb.send_customer_email("buyer@example.com", "WR-NEW001", link(t=6.99),
                                   {"gb": 10.0, "days": 30, "location": "Europe", "network": ""})

    def test_before_data_the_error_is_what_it_was(self):
        with self.assertRaises(smtplib.SMTPAuthenticationError):
            self.send("login")
        with self.assertRaises(ConnectionResetError):
            self.send("tls")
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send("rcpt")

    def test_a_line_lost_during_data_may_have_delivered(self):
        with self.assertRaises(fb.MailMaybeSent):
            self.send("data")

    def test_the_server_s_own_no_is_a_clear_failure(self):
        with self.assertRaises(smtplib.SMTPDataError):
            self.send("refused")

    def test_a_failure_after_the_250_is_a_sent_mail(self):
        self.send("quit")                         # no exception: it went

    def test_the_public_log_never_sees_the_address(self):
        e = smtplib.SMTPRecipientsRefused({"buyer@example.com": (550, b"5.1.1 buyer@example.com no")})
        self.assertNotIn("buyer", fb._mail_error_text(e))
        self.assertIn("550", fb._mail_error_text(e))
        self.assertEqual(fb._mail_error_text(smtplib.SMTPAuthenticationError(535, b"x")),
                         "SMTPAuthenticationError (535)")

    def test_hard_is_the_mail_path_not_one_recipient(self):
        hard = fb._hard_mail_failure
        self.assertTrue(hard(smtplib.SMTPAuthenticationError(535, b"x")))
        self.assertTrue(hard(TimeoutError("timed out")))
        self.assertTrue(hard(smtplib.SMTPServerDisconnected("x")))
        self.assertTrue(hard(fb.MailMaybeSent("x")))
        self.assertTrue(hard(RuntimeError("Missing env/secret: GMAIL_APP_PASSWORD")))
        self.assertTrue(hard(smtplib.SMTPDataError(421, b"4.7.0 try again later")))
        self.assertTrue(hard(smtplib.SMTPDataError(550, b"5.4.5 Daily user sending limit exceeded")))
        self.assertFalse(hard(smtplib.SMTPRecipientsRefused({"a@b.c": (550, b"5.1.1 no such user")})))
        self.assertFalse(hard(smtplib.SMTPDataError(552, b"5.3.4 message too big")))
        self.assertFalse(hard(ValueError("missing customer email address")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
