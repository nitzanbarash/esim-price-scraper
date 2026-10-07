"""receipts_colors: the rules it wants, and that a second run writes nothing."""
import unittest

import receipts_colors as rc

# The tab's shape up to 2026-10-07: usage in E, status in S, source in V,
# buy/sale/sell in X/Y/Z. (The owner then moved everything from S one to the
# right; see test_rules_stranded_by_a_column_move_are_rewritten.)
HEADER = (["מייל - Mail", "", "", "", rc.COL_USAGE] + [""] * 13
          + [rc.COL_STATUS, "", ""]
          + [rc.COL_SOURCE, "", rc.COL_BUY, rc.COL_SALE, rc.COL_SELL])
USAGE_ORDER = [rc.USAGE_GREY, rc.USAGE_BLUE, rc.USAGE_RED, rc.USAGE_YELLOW, rc.USAGE_GREEN]


def sheet(rules=()):
    return {"properties": {"sheetId": 0, "gridProperties": {"rowCount": 993}},
            "conditionalFormats": list(rules)}


# The owner's hand-made Status rule as it stood until 2026-10-07.
STATUS_RULE = {"ranges": [{"startRowIndex": 1, "endRowIndex": 993,
                           "startColumnIndex": 18, "endColumnIndex": 19}],
               "booleanRule": {"condition": {"type": "TEXT_EQ",
                                             "values": [{"userEnteredValue": "פעיל"}]},
                               "format": {"backgroundColor": {"green": 1}}}}
# Somebody else's rule: not on our columns, not one of our formulas.
FOREIGN_RULE = {"ranges": [{"startRowIndex": 1, "endRowIndex": 993,
                            "startColumnIndex": 1, "endColumnIndex": 2}],
                "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                                              "values": [{"userEnteredValue": '=$B2<TODAY()-30'}]},
                                "format": {"backgroundColor": {"red": 1}}}}


def cols_of(reqs, key="addConditionalFormatRule"):
    return sorted({q[key]["rule"]["ranges"][0]["startColumnIndex"] for q in reqs if key in q})


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

    def test_second_run_writes_nothing_and_foreign_rules_survive(self):
        first = rc.plan(sheet([FOREIGN_RULE]), HEADER, ["esim.dog", "Stellar"])
        self.assertTrue(all("deleteConditionalFormatRule" not in q for q in first))
        applied = [FOREIGN_RULE] + [q["addConditionalFormatRule"]["rule"] for q in first]
        self.assertEqual(rc.plan(sheet(applied), HEADER, ["esim.dog", "Stellar"]), [])
        # A new dropdown entry rewrites only our rules, never the foreign one.
        again = rc.plan(sheet(applied), HEADER, ["esim.dog", "Stellar", "Airalo"])
        dels = [q["deleteConditionalFormatRule"]["index"] for q in again
                if "deleteConditionalFormatRule" in q]
        self.assertNotIn(0, dels)
        self.assertEqual(dels, sorted(dels, reverse=True))

    def test_status_colours_follow_the_status_header(self):
        rules = [r for r in rc.desired_rules(HEADER, [], 0, 993)
                 if r["ranges"][0]["startColumnIndex"] == 18]
        self.assertEqual([rc._hex(r["booleanRule"]["format"]["backgroundColor"]) for r in rules],
                         [rc.STATUS_ACTIVE, rc.STATUS_DONE, rc.STATUS_FAULT])
        f = [r["booleanRule"]["condition"]["values"][0]["userEnteredValue"] for r in rules]
        self.assertEqual(f[0], '=TRIM($S2)="פעיל"')
        self.assertIn('"הסתיים"', f[1])
        # The owner's old TEXT_EQ rule is taken over, not left to paint a neighbour.
        reqs = rc.plan(sheet([STATUS_RULE]), HEADER, [])
        self.assertEqual([q["deleteConditionalFormatRule"]["index"] for q in reqs
                          if "deleteConditionalFormatRule" in q], [0])

    def test_rules_stranded_by_a_column_move_are_rewritten(self):
        # 2026-10-07: the owner cut/pasted Status, source, buy/sale/sell one
        # column to the right (S->T, V->W, X/Y/Z->Y/Z/AA). Sheets left every
        # rule where it was, so Status colours painted the new Device column.
        before = [FOREIGN_RULE] + rc.desired_rules(HEADER, ["esim.dog", "Stellar"], 0, 993)
        moved = HEADER[:18] + ["דגם טלפון - Device"] + HEADER[18:]
        reqs = rc.plan(sheet(before), moved, ["esim.dog", "Stellar"])
        dels = [q["deleteConditionalFormatRule"]["index"] for q in reqs
                if "deleteConditionalFormatRule" in q]
        self.assertEqual(sorted(dels), list(range(1, len(before))))      # all ours, not the foreign one
        self.assertEqual(cols_of(reqs), [4, 19, 22, 26])                 # E stays; S,V,Z -> T,W,AA
        applied = [FOREIGN_RULE] + [q["addConditionalFormatRule"]["rule"] for q in reqs
                                    if "addConditionalFormatRule" in q]
        self.assertEqual(rc.plan(sheet(applied), moved, ["esim.dog", "Stellar"]), [])

    def test_missing_column_changes_nothing(self):
        with self.assertRaises(SystemExit):
            rc.desired_rules(HEADER[:-1], [], 0, 993)
        with self.assertRaises(SystemExit):
            rc.desired_rules([h for h in HEADER if h != rc.COL_USAGE], [], 0, 993)


class Usage(unittest.TestCase):
    """The 'GB (0/X) - ניצול' bands. The sheet evaluates the formulas; here the
    Python twin (usage_band) stands in for them, and the rules themselves are
    checked for range, colour, order and the shape that keeps them from erroring."""

    def band(self, text):
        return rc.usage_band(text)

    def test_each_band_on_the_owner_s_own_cells(self):
        self.assertEqual(self.band("0 / 10"), rc.USAGE_GREY)
        self.assertEqual(self.band("3.407 / 10"), rc.USAGE_GREEN)
        self.assertEqual(self.band("5.562 / 10"), rc.USAGE_YELLOW)
        self.assertEqual(self.band("9.626 / 10"), rc.USAGE_RED)
        self.assertEqual(self.band("10 / 10"), rc.USAGE_BLUE)

    def test_band_edges(self):
        self.assertEqual(self.band("0.0 / 5"), rc.USAGE_GREY)          # a decimal zero is zero
        self.assertEqual(self.band("0.029 / 10"), rc.USAGE_GREEN)      # anything used is green
        self.assertEqual(self.band("5 / 10"), rc.USAGE_GREEN)          # exactly 50% is still green
        self.assertEqual(self.band("5.001 / 10"), rc.USAGE_YELLOW)
        self.assertEqual(self.band("8 / 10"), rc.USAGE_YELLOW)         # exactly 80% is still yellow
        self.assertEqual(self.band("4 / 5"), rc.USAGE_YELLOW)
        self.assertEqual(self.band("8.001 / 10"), rc.USAGE_RED)
        self.assertEqual(self.band("9.999 / 10"), rc.USAGE_RED)
        self.assertEqual(self.band("10.486 / 10.486"), rc.USAGE_BLUE)  # finished, decimal total
        self.assertEqual(self.band("11 / 10"), rc.USAGE_BLUE)          # over the top is finished too

    def test_spacing_is_tolerated(self):
        for text in ("3.4/10", " 3.4 /10", "3.4/ 10 ", "  3.4  /  10  "):
            self.assertEqual(self.band(text), rc.USAGE_GREEN, text)
        self.assertEqual(self.band("10/10"), rc.USAGE_BLUE)
        self.assertEqual(self.band(" 0 /10"), rc.USAGE_GREY)

    def test_zero_total_is_grey_never_blue_and_never_divides(self):
        self.assertEqual(self.band("0 / 0"), rc.USAGE_GREY)
        self.assertIsNone(self.band("3 / 0"))     # garbage: no band, no error

    def test_blank_and_garbage_get_no_band(self):
        for text in ("", None, "   ", "abc", "10", "10GB", "/ 10", "3 /", "n/a", "1,5 / 10",
                     "3.4 / 10 / 2", "-3 / 10"):
            self.assertIsNone(self.band(text), repr(text))

    def test_rules_range_colour_and_order(self):
        rules = [r for r in rc.desired_rules(HEADER, [], 0, 993)
                 if r["ranges"][0]["startColumnIndex"] == 4]
        self.assertEqual([rc._hex(r["booleanRule"]["format"]["backgroundColor"]) for r in rules],
                         USAGE_ORDER)
        for r in rules:
            self.assertEqual(r["ranges"][0], {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 993,
                                              "startColumnIndex": 4, "endColumnIndex": 5})
            cond = r["booleanRule"]["condition"]
            self.assertEqual(cond["type"], "CUSTOM_FORMULA")
            f = cond["values"][0]["userEnteredValue"]
            self.assertTrue(f.startswith("=IFERROR(AND(") and f.endswith(",FALSE)"), f)
            self.assertIn("$E2", f)
            self.assertIn("REGEXEXTRACT(TO_TEXT($E2)", f)
            self.assertNotIn("/VALUE", f)         # compared by product, never divided
            self.assertNotIn("backgroundColor", str(r["booleanRule"]["format"].get("textFormat")))
        # The formulas read with the same regexes the twin reads with.
        for f in (rules[0]["booleanRule"]["condition"]["values"][0]["userEnteredValue"],):
            self.assertIn(rc.USED_RE, f)
            self.assertIn(rc.TOTAL_RE, f)
        self.assertEqual([c for _, c in rc.usage_formulas("$E2")], USAGE_ORDER)

    def test_rules_follow_the_header(self):
        moved = HEADER[:]
        moved.insert(0, "new")              # the owner inserts a column: E -> F
        rules = [r for r in rc.desired_rules(moved, [], 0, 993)
                 if r["ranges"][0]["startColumnIndex"] == 5]
        self.assertEqual(len(rules), 5)
        for r in rules:
            self.assertIn("$F2", r["booleanRule"]["condition"]["values"][0]["userEnteredValue"])

    def test_usage_rules_are_added_beside_the_existing_ones(self):
        # The sheet as it stood on 2026-10-06: a foreign rule, then this
        # script's two on V and five on Z. The usage rules must ADD to those,
        # and a second run must then write nothing.
        ours = [r for r in rc.desired_rules(HEADER, ["esim.dog", "Stellar"], 0, 993)
                if r["ranges"][0]["startColumnIndex"] in (21, 25)]
        self.assertEqual(len(ours), 7)
        have = [FOREIGN_RULE] + ours
        reqs = rc.plan(sheet(have), HEADER, ["esim.dog", "Stellar"])
        dels = [q["deleteConditionalFormatRule"]["index"] for q in reqs
                if "deleteConditionalFormatRule" in q]
        self.assertTrue(all(i >= 1 for i in dels), dels)          # the foreign rule untouched
        adds = [q["addConditionalFormatRule"]["rule"] for q in reqs if "addConditionalFormatRule" in q]
        self.assertEqual(len(adds), 15)
        self.assertEqual(sum(1 for r in adds if r["ranges"][0]["startColumnIndex"] == 4), 5)
        applied = [FOREIGN_RULE] + adds
        self.assertEqual(len(applied), 16)
        self.assertEqual(rc.plan(sheet(applied), HEADER, ["esim.dog", "Stellar"]), [])


if __name__ == "__main__":
    unittest.main()
