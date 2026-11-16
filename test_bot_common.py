"""bot_common: the jobs the update network ships (usage_bot, receipts_colors)
reach no buying code, and fulfillment_bot still offers every moved name."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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

import bot_common
import fulfillment_bot

# The alert throttle keeps a file in the temp folder; the tests keep their own.
bot_common.THROTTLE_FILE = os.path.join(tempfile.mkdtemp(prefix="wr-test-"), "throttle.json")

# The update network's release builder refuses these (waverole-net
# net/build.py NEVER), plus the two modules that reach a buyer.
BUYERS = {"stellar_buyer", "create_order", "finance_bot", "finance_core",
          "stellar_prices", "fulfillment_bot", "stock_shelf"}


def imports_of(path):
    out = set()
    for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(n, ast.Import):
            out |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            out.add(n.module.split(".")[0])
    return out


class BotCommon(unittest.TestCase):
    def test_no_buying_code_anywhere_in_the_shared_module(self):
        # every level, function-level imports too: that is what the builder reads
        self.assertEqual(imports_of("bot_common.py") & BUYERS, set())

    def test_the_jobs_import_no_buyer(self):
        for job in ("usage_bot.py", "receipts_colors.py"):
            self.assertEqual(imports_of(job) & BUYERS, set(), job)

    def test_loading_the_jobs_loads_no_buyer(self):
        code = ("import sys, usage_bot, receipts_colors; "
                f"print(sorted(m for m in {sorted(BUYERS)!r} if m in sys.modules))")
        got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(got.stdout.strip(), "[]")

    def test_fulfillment_bot_still_offers_every_moved_name(self):
        # stellar_buyer and stock_shelf read these as fb.<name>
        for name in ("alert", "env", "sheet_client", "fetch_usage", "fetch_esim_details",
                     "is_replaced_status", "_redact", "_row_time", "TZ", "ORDERS_URL",
                     "RECEIPTS_SHEET_ID", "GMAIL_USER", "ALERTS_EMAIL", "ALERT_PREFIX",
                     "REPLACED_STATUS", "REPLACED_CHECK_STATUS", "SESSION_ID_RE", "GB"):
            self.assertIs(getattr(fulfillment_bot, name), getattr(bot_common, name), name)



class Throttle(unittest.TestCase):
    """2026-11-16: alerts share Gmail's ~500 a day with the customers' eSIM
    mails, and the PCs run the mail bot every 45 s at their quickest."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="wr-test-")
        self.p = mock.patch.object(bot_common, "THROTTLE_FILE", os.path.join(self.dir, "t.json"))
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_once_per_window_then_again(self):
        ok = bot_common.throttle_ok
        self.assertTrue(ok("a", 3600, now=1000))
        self.assertFalse(ok("a", 3600, now=1000 + 3599))
        self.assertTrue(ok("b", 3600, now=1001), "another key is its own")
        self.assertTrue(ok("a", 3600, now=1000 + 3600))

    def test_a_clock_set_back_does_not_hold_it(self):
        self.assertTrue(bot_common.throttle_ok("a", 3600, now=5000))
        self.assertTrue(bot_common.throttle_ok("a", 3600, now=4000))

    def test_the_file_holds_no_subject_text_and_old_keys_go(self):
        bot_common.throttle_ok("WR-ABC123 esim.dog", 60, now=1000)
        bot_common.throttle_ok("new", 60, now=1000 + bot_common.THROTTLE_KEEP_S + 1)
        with open(bot_common.THROTTLE_FILE, encoding="ascii") as f:
            raw = f.read()
        self.assertNotIn("WR-ABC123", raw)
        self.assertNotIn("esim", raw)
        self.assertEqual(len(json.loads(raw)), 1, "the week-old key was pruned")

    def test_never_raises_and_lets_it_through(self):
        with open(bot_common.THROTTLE_FILE, "w") as f:
            f.write("{not json")
        self.assertTrue(bot_common.throttle_ok("a", 3600))
        with mock.patch.object(bot_common, "THROTTLE_FILE",
                               os.path.join(self.dir, "no", "such", "dir", "t.json")):
            self.assertTrue(bot_common.throttle_ok("a", 3600))
            self.assertTrue(bot_common.throttle_ok("a", 3600), "unwritable: always through")
        self.assertTrue(bot_common.throttle_ok("x", 0), "every_s 0 is no throttle")

    def _alerts(self, calls, fail=False):
        sent = []

        class SMTP:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def starttls(self):
                pass

            def login(self, *a):
                pass

            def send_message(self, msg):
                if fail:
                    raise OSError("smtp down")
                sent.append(msg["Subject"])
        with mock.patch.object(bot_common.smtplib, "SMTP", SMTP), \
             mock.patch.dict(os.environ, {"GMAIL_APP_PASSWORD": "x"}), \
             mock.patch.object(bot_common, "log", mock.Mock()):
            for args, kw in calls:
                bot_common.alert(*args, **kw)
        return sent

    def test_alert_same_subject_once_an_hour(self):
        sent = self._alerts([(("Receipts sheet problem", "b1"), {}),
                             (("Receipts sheet problem", "b2"), {}),
                             (("Order WR-AAAAAA: x", "b"), {}),
                             (("Order WR-BBBBBB: x", "b"), {})])
        self.assertEqual(len(sent), 3)

    def test_a_one_off_bypasses_and_a_key_separates(self):
        sent = self._alerts([(("Ambiguous match", "1"), {"every_s": 0}),
                             (("Ambiguous match", "2"), {"every_s": 0}),
                             (("refused", "1"), {"key": "pkg-1"}),
                             (("refused", "2"), {"key": "pkg-2"}),
                             (("refused", "3"), {"key": "pkg-1"})])
        self.assertEqual(len(sent), 4)

    def test_a_failed_alert_is_not_counted_as_sent(self):
        self._alerts([(("S", "b"), {})], fail=True)
        self.assertEqual(len(self._alerts([(("S", "b"), {})])), 1)


class SheetClientTimeout(unittest.TestCase):
    def test_every_sheets_call_has_a_timeout(self):
        got = []
        gc = mock.Mock()
        gc.set_timeout = got.append
        with mock.patch.object(bot_common.gspread, "service_account_from_dict", lambda info: gc), \
             mock.patch.dict(os.environ, {"GOOGLE_CREDENTIALS_JSON": "{}"}):
            self.assertIs(bot_common.sheet_client(), gc)
        self.assertEqual(got, [bot_common.SHEETS_TIMEOUT])
        self.assertTrue(all(t for t in bot_common.SHEETS_TIMEOUT))


if __name__ == "__main__":
    unittest.main()
