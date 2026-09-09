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
                   row("2.49.10", "Stellar", "$2.57 (€2.21)", ""))
        self.assertEqual(p["2.49.10"]["list"], "6.99")
        self.assertEqual(p["2.49.10"]["buy"], "$3.12")

    def test_order_within_the_pair_does_not_matter(self):
        under = prices(row("2.49.10", "esim.dog", "$3.12", "6.99"),
                       row("2.49.10", "Stellar", "$2.57 (€2.21)", ""))
        over = prices(row("2.49.10", "Stellar", "$2.57 (€2.21)", ""),
                      row("2.49.10", "esim.dog", "$3.12", "6.99"))
        self.assertEqual(under, over)

    def test_a_supplier_only_sku_stays_priced(self):
        # Dropping it would move real sales into "sold but not in the price
        # sheet" — a louder wrong answer than the one being fixed.
        p = prices(row("2.0B.5", "Stellar", "$1.59 (€1.37)", ""))
        self.assertIn("2.0B.5", p)
        self.assertEqual(p["2.0B.5"]["buy"], "$1.59 (€1.37)")

    def test_case_and_padding_in_the_source_cell_still_match(self):
        p = prices(row("2.49.10", "Stellar", "$2.57 (€2.21)", ""),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
