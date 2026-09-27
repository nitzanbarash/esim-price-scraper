"""receipts_colors: the rules it wants, and that a second run writes nothing."""
import unittest

import receipts_colors as rc

HEADER = ["מייל - Mail"] + [""] * 20 + [rc.COL_SOURCE, "", rc.COL_BUY, rc.COL_SALE, rc.COL_SELL]


def sheet(rules=()):
    return {"properties": {"sheetId": 0, "gridProperties": {"rowCount": 993}},
            "conditionalFormats": list(rules)}


STATUS_RULE = {"ranges": [{"startRowIndex": 1, "endRowIndex": 993,
                           "startColumnIndex": 18, "endColumnIndex": 19}],
               "booleanRule": {"condition": {"type": "TEXT_EQ",
                                             "values": [{"userEnteredValue": "פעיל"}]},
                               "format": {"backgroundColor": {"green": 1}}}}


class Colors(unittest.TestCase):
    def test_owner_colours(self):
        got = dict(rc.supplier_colors(["esim.dog", "Stellar"]))
        self.assertEqual(got, {"esim.dog": "#9900ff", "Stellar": "#4a86e8"})

    def test_new_supplier_gets_next_palette_colour(self):
        got = rc.supplier_colors(["esim.dog", "Stellar", "Airalo", "Holafly"])
        self.assertEqual(got[2:], [("Airalo", rc.PALETTE[0]), ("Holafly", rc.PALETTE[1])])
        # A case variant of a known supplier is not a new supplier.
        self.assertEqual(len(rc.supplier_colors(["STELLAR", "esim.dog"])), 2)

    def test_formulas_follow_the_headers(self):
        rules = rc.desired_rules(HEADER, ["esim.dog", "Stellar"], 0, 993)
        f = [r["booleanRule"]["condition"]["values"][0]["userEnteredValue"] for r in rules]
        self.assertEqual(f[0], '=LOWER(TRIM($V2))="esim.dog"')
        self.assertIn("$Z2", f[2])
        self.assertIn("$X2", f[4])          # loss compares Sell with Buy
        self.assertIn("$Y2", f[5])          # discount reads the Sale column
        moved = HEADER[:]
        moved.insert(0, "new")              # the owner inserts a column: V -> W
        f2 = rc.desired_rules(moved, ["esim.dog"], 0, 993)[0]
        self.assertIn("$W2", f2["booleanRule"]["condition"]["values"][0]["userEnteredValue"])

    def test_supplier_is_a_text_colour_not_a_fill(self):
        fmt = rc.desired_rules(HEADER, ["Stellar"], 0, 993)[1]["booleanRule"]["format"]
        self.assertNotIn("backgroundColor", fmt)
        self.assertEqual(rc._hex(fmt["textFormat"]["foregroundColor"]), "#4a86e8")

    def test_red_before_discount_before_green(self):
        rules = rc.desired_rules(HEADER, [], 0, 993)
        sell = [rc._hex(r["booleanRule"]["format"]["backgroundColor"]) for r in rules
                if r["ranges"][0]["startColumnIndex"] == 25]
        self.assertEqual(sell, [rc.RED_REFUND, rc.RED, rc.RED, rc.DISCOUNT, rc.GREEN])

    def test_second_run_writes_nothing_and_status_rules_survive(self):
        first = rc.plan(sheet([STATUS_RULE]), HEADER, ["esim.dog", "Stellar"])
        self.assertTrue(all("deleteConditionalFormatRule" not in q for q in first))
        applied = [STATUS_RULE] + [q["addConditionalFormatRule"]["rule"] for q in first]
        self.assertEqual(rc.plan(sheet(applied), HEADER, ["esim.dog", "Stellar"]), [])
        # A new dropdown entry rewrites only our rules, never the Status one.
        again = rc.plan(sheet(applied), HEADER, ["esim.dog", "Stellar", "Airalo"])
        dels = [q["deleteConditionalFormatRule"]["index"] for q in again
                if "deleteConditionalFormatRule" in q]
        self.assertNotIn(0, dels)
        self.assertEqual(dels, sorted(dels, reverse=True))

    def test_missing_column_changes_nothing(self):
        with self.assertRaises(SystemExit):
            rc.desired_rules(HEADER[:-1], [], 0, 993)


if __name__ == "__main__":
    unittest.main()
