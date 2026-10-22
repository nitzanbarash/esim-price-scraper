"""bot_common: the jobs the update network ships (usage_bot, receipts_colors)
reach no buying code, and fulfillment_bot still offers every moved name."""
import ast
import subprocess
import sys
import unittest

import bot_common
import fulfillment_bot

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


if __name__ == "__main__":
    unittest.main()
