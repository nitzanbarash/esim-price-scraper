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
import unittest
from unittest import mock

os.environ.setdefault("GMAIL_APP_PASSWORD", "test-password")

import fulfillment_bot as fb  # noqa: E402

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
             mock.patch.object(fb, "alert", lambda s, b: sent.append(b)):
            fb.check_stale_pending()
        self.assertIn("FREE (gift)", sent[0])
        self.assertNotIn("$0", sent[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
