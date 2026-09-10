#!/usr/bin/env python3
"""The price sheet has two rows per SKU. The books must read the right one.

Since 2026-09-07 each package occupies one row per supplier, stacked under a
shared SKU. read_prices was keyed by SKU alone, so the Stellar row — sitting
underneath, carrying no sell price because we have never bought from Stellar —
overwrote the esim.dog row. Measured on the live sheet before the fix: 57 SKUs
lost their 'מחיר סופי' and 21 sales reported a discount of zero.
"""

import unittest

import finance_bot as fb
import finance_core as fc

HEAD = ["מק\"ט", "מדינות", "GB", "מקור", "קישור", "זמן חבילה",
        "מחיר קנייה", "עמלת סליקה", "מחיר סופי"]


def row(sku, source, buy, sell):
    return [sku, "גרמניה", "10gb", source, "", "30d", buy, "0.5", sell]


class FakeSheet:
    def __init__(self, values):
        self.sheet1 = self
        self._v = values

    def get_all_values(self):
        return self._v


class FakeClient:
    def __init__(self, values):
        self._s = FakeSheet(values)

    def open_by_key(self, key):
        return self._s


def prices(*rows):
    return fb.read_prices(FakeClient([HEAD, *rows]))


class TestSupplierRows(unittest.TestCase):

    def test_the_supplier_row_underneath_does_not_win(self):
        p = prices(row("2.49.10", "esim.dog", "$3.12", "6.99"),
                   row("2.49.10", "Stellar", "(€2.21) $2.57", ""))
        self.assertEqual(p["2.49.10"]["list"], "6.99")
        self.assertEqual(p["2.49.10"]["buy"], "$3.12")

    def test_order_within_the_pair_does_not_matter(self):
        under = prices(row("2.49.10", "esim.dog", "$3.12", "6.99"),
                       row("2.49.10", "Stellar", "(€2.21) $2.57", ""))
        over = prices(row("2.49.10", "Stellar", "(€2.21) $2.57", ""),
                      row("2.49.10", "esim.dog", "$3.12", "6.99"))
        self.assertEqual(under, over)

    def test_a_supplier_only_sku_stays_priced(self):
        # Dropping it would move real sales into "sold but not in the price
        # sheet" — a louder wrong answer than the one being fixed.
        p = prices(row("2.0B.5", "Stellar", "(€1.37) $1.59", ""))
        self.assertIn("2.0B.5", p)
        self.assertEqual(p["2.0B.5"]["buy"], "(€1.37) $1.59")

    def test_case_and_padding_in_the_source_cell_still_match(self):
        p = prices(row("2.49.10", "Stellar", "(€2.21) $2.57", ""),
                   row("2.49.10", "  ESIM.DOG ", "$3.12", "6.99"))
        self.assertEqual(p["2.49.10"]["list"], "6.99")

    def test_a_sheet_with_no_source_column_still_reads(self):
        head = [h for h in HEAD if h != "מקור"]
        v = [head, ["2.49.10", "גרמניה", "10gb", "", "30d", "$3.12", "0.5", "6.99"]]
        p = fb.read_prices(FakeClient(v))
        self.assertEqual(p["2.49.10"]["list"], "6.99")

    def test_blank_sku_rows_are_skipped(self):
        p = prices(row("", "esim.dog", "$1", "2"),
                   row("2.49.10", "esim.dog", "$3.12", "6.99"))
        self.assertEqual(list(p), ["2.49.10"])


# ── the payment rail ─────────────────────────────────────────────────────────
# 'רכישה - Purchase' is the receipts column that names how the money arrived.
# It was never in read_receipts' provider search, so every order came out with
# provider '' — which FeeModel answers with the account default, Bit, whose fee
# is zero. Result: every PayPal sale was booked as costing nothing to collect,
# and PayPal takes 3.4%. These pin the wiring end to end.

RECEIPTS_HEAD = ["תאריך - Date", 'מק"ט - SUK', "איחסון - GB", "מס׳ הזמנה",
                 "אזור - Region", "מייל - Mail", "Route", "סטטוס - Status",
                 "מקור - source", "קנייה - Buy", "הנחה - Sale",
                 "מכירה - Sell", "רכישה - Purchase"]


def receipt(rail):
    return ["2026-09-10", "2.49.10", "10gb", "WR-1", "גרמניה", "a@b.c",
            "Cellcom", "פעיל", "esim.dog", "$3.12", "-", "6.99", rail]


def receipts(*rails):
    return fb.read_receipts(FakeClient([RECEIPTS_HEAD,
                                        *[receipt(r) for r in rails]]))


class TestPaymentRailColumn(unittest.TestCase):

    def test_the_purchase_column_is_found_as_the_provider(self):
        got = receipts("paypal")
        self.assertEqual(got[0]["provider"], "paypal")

    def test_every_rail_the_dropdown_offers_survives_the_read(self):
        got = receipts("paypal", "bot - manually", "icount")
        self.assertEqual([r["provider"] for r in got],
                         ["paypal", "bot - manually", "icount"])

    def test_the_other_columns_did_not_move(self):
        # A substring search is easy to widen too far. 'רכישה' must not have
        # stolen the buy/sell columns on its way in.
        r = receipts("paypal")[0]
        self.assertEqual(r["buy"], "$3.12")
        self.assertEqual(r["sell"], "6.99")
        self.assertEqual(r["order_id"], "WR-1")
        self.assertEqual(r["discount"], "-")

    def test_a_sheet_without_the_column_still_reads(self):
        head = RECEIPTS_HEAD[:-1]
        got = fb.read_receipts(FakeClient([head, receipt("paypal")[:-1]]))
        self.assertEqual(got[0]["provider"], "")


class TestRailToFee(unittest.TestCase):
    """What each rail costs to collect. The rails are not all processors."""

    def setUp(self):
        self.model = fc.FeeModel()

    def test_paypal_resolves_to_the_paypal_fee(self):
        p = self.model.provider("paypal")
        self.assertEqual(p.name, "PayPal")
        self.assertGreater(p.rate, 0)
        self.assertEqual(p.fee(100.0), round(100.0 * p.rate, 4))

    def test_paypal_is_matched_case_and_space_insensitively(self):
        self.assertEqual(self.model.provider(" PayPal ").name, "PayPal")

    def test_a_hand_bought_order_falls_to_the_default(self):
        # 'bot - manually' and 'icount' are how WE bought or invoiced, not a
        # card processor. No processing fee was ever incurred, so the default
        # (Bit, zero) is the true answer, not a stand-in for a missing one.
        for rail in ("bot - manually", "icount", "", None):
            with self.subTest(rail=rail):
                p = self.model.provider(rail)
                self.assertEqual(p.name, "Bit")
                self.assertEqual(p.fee(100.0), 0.0)

    def test_the_fee_actually_reaches_the_order(self):
        paypal, _ = fc.processing_fee(100.0, "0.50", self.model, provider="paypal")
        bit, _ = fc.processing_fee(100.0, "0.50", self.model, provider="bot - manually")
        self.assertGreater(paypal, 0)
        self.assertEqual(bit, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
