import unittest
import usage_bot as ub


class RefundMarks(unittest.TestCase):
    HDR = [ub.COL_ORDER, ub.COL_BUY, ub.COL_SELL]

    def marks(self, body, refunds):
        rows = [self.HDR] + body
        idx = {c: i for i, c in enumerate(self.HDR)}
        return ub.refund_marks(rows, idx, refunds)

    def test_full_customer_refund_marks_sell(self):
        m = self.marks([["WR-AAA111", "3.84$", "6.99$"]],
                       [{"order_id": "WR-AAA111", "paid_usd": 6.99, "refund_usd": 6.99}])
        self.assertEqual(m, [(2, 3, "refund")])

    def test_partial_refund_names_the_amount(self):
        m = self.marks([["WR-AAA111", "3.84$", "6.99$"]],
                       [{"order_id": "wr-aaa111", "paid_usd": 6.99, "refund_usd": 3}])
        self.assertEqual(m, [(2, 3, "refund 3.00$")])

    def test_supplier_refund_marks_buy(self):
        m = self.marks([["WR-AAA111", "3.84$", "6.99$"]],
                       [{"order_id": "WR-AAA111", "cost_usd": 3.84, "supplier_refund_usd": 3.84}])
        self.assertEqual(m, [(2, 2, "refund")])

    def test_already_marked_or_unknown_left_alone(self):
        m = self.marks([["WR-AAA111", "refund", "Refund"]],
                       [{"order_id": "WR-AAA111", "paid_usd": 6.99, "refund_usd": 6.99, "supplier_refund_usd": 3.84},
                        {"order_id": "WR-NOPE00", "refund_usd": 5}])
        self.assertEqual(m, [])


if __name__ == "__main__":
    unittest.main()
