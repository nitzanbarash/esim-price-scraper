"""Tests for the money logic.

The fixtures are real (only the receipt link is a fake): STRIPE_BODY is a verbatim supplier receipt pulled from
waverolesupply@gmail.com, and the reconciliation cases reproduce the 9 August
burst where eight identical $4.17 charges landed within twenty minutes.
"""

import unittest
from datetime import datetime, timedelta, timezone

import finance_core as fc


UTC = timezone.utc


def il(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=fc.IL)


def utc(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


STRIPE_BODY = """Receipt from ESIM.DOG Receipt #1958-0718
Amount paid
$4.17

Date paid
Aug 11, 2026, 9:12:02 AM

Payment method
- 8247

Summary
eSIM Cyprus - 10GB (30 days validity)

- - eSIM Cyprus - 10GB (One-time) x 1 : $4.17

- Subtotal : $4.17
- Amount paid : $4.17

If you have any questions, contact us at hello@esim.dog (hello@esim.dog).

Something wrong with the email? View in browser: (https://dashboard.stripe.com/receipts/payment/FAKE-RECEIPT-TOKEN-FOR-TESTS-0000000000000000000000000000000000000000000000000000000000000000)
"""

STRIPE_SENDER = "receipts+acct_1Rl17YP3rsvFckzp@stripe.com"


class TestMoneyParsing(unittest.TestCase):
    def test_reads_every_shape_the_sheets_use(self):
        cases = [
            ("4.17$", 4.17, "USD"),
            ("$4.17", 4.17, "USD"),
            ("6", 6.0, None),
            ("6.5", 6.5, None),
            ("₪12.50", 12.5, "ILS"),
            ("USD 4.17", 4.17, "USD"),
            ("1,234.50 ILS", 1234.5, "ILS"),
            (7, 7.0, None),
        ]
        for raw, value, currency in cases:
            with self.subTest(raw=raw):
                self.assertEqual(fc.parse_money(raw), (value, currency))

    def test_nothing_is_not_zero(self):
        # A blank cell must be distinguishable from a real zero, or every
        # missing price silently becomes a free package.
        self.assertEqual(fc.parse_money(""), (None, None))
        self.assertEqual(fc.parse_money(None), (None, None))
        self.assertEqual(fc.parse_money("לא ידוע"), (None, None))
        self.assertEqual(fc.parse_money("0"), (0.0, None))

    def test_sheet_dates_are_israel_local(self):
        dt = fc.parse_sheet_datetime("09/08/2026 08:03:25")
        self.assertIsNotNone(dt)
        self.assertEqual((dt.day, dt.month, dt.hour), (9, 8, 8))
        self.assertEqual(dt.tzinfo, fc.IL)

    def test_utc_mail_time_converts_into_the_sheet_s_clock(self):
        # The 9 Aug 05:04 UTC receipt is the 08:04 Israel-time sheet row.
        self.assertEqual(fc.to_israel(utc(2026, 8, 9, 5, 4)).hour, 8)


class TestSkuNormalisation(unittest.TestCase):
    def test_bridges_the_missing_zero(self):
        known = {"1.0A.3", "2.30.10"}
        self.assertEqual(fc.normalise_sku("1.A.3", known), "1.0A.3")
        self.assertEqual(fc.normalise_sku("1.0A.3", known), "1.0A.3")
        self.assertEqual(fc.normalise_sku("2.30.10", known), "2.30.10")
        self.assertIsNone(fc.normalise_sku("9.9.9", known))


class TestFeeModel(unittest.TestCase):
    def test_bit_costs_nothing_to_receive(self):
        # Every order so far was paid by Bit. Charging a modelled processing
        # fee against those would invent an expense that never happened.
        fee, source = fc.processing_fee(6.0, "0.11", fc.FeeModel())
        self.assertEqual(fee, 0.0)
        self.assertEqual(source, fc.NO_FEE)

    def test_the_price_sheet_column_is_a_plan_not_a_charge(self):
        # 0.11 sits in the price sheet for this package, but nothing was
        # ever processed, so it must not reach the books.
        sale = fc.build_sale(
            {"date": "09/08/2026 08:03:25", "sku": "2.34.10", "sell": "4.5$",
             "buy": "3.23$"}, {"fee": "0.11", "list": "4.5"}, fc.FeeModel())
        self.assertEqual(sale.fee, 0.0)
        self.assertAlmostEqual(sale.profit, 4.5 - 3.23, places=4)

    def test_a_card_provider_does_cost_something(self):
        model = fc.FeeModel(default_provider="card")
        for price, expected in ((1.49, 0.11), (3.89, 0.16), (10.49, 0.31)):
            self.assertAlmostEqual(model.estimate(price), expected, places=2)

    def test_paypal_and_payme_are_marked_as_estimates(self):
        model = fc.FeeModel(default_provider="bit")
        for key in ("payme", "paypal"):
            fee, source = fc.processing_fee(100.0, "", model, provider=key)
            self.assertGreater(fee, 0, key)
            self.assertIn("הערכה", source, key)

    def test_a_fee_the_provider_really_reported_beats_every_estimate(self):
        fee, source = fc.processing_fee(
            100.0, "", fc.FeeModel(default_provider="paypal"),
            provider="paypal", actual_fee="3.90$")
        self.assertEqual((fee, source), (3.9, "בפועל"))

    def test_the_provider_is_chosen_per_order(self):
        model = fc.FeeModel(default_provider="bit")
        bit, _ = fc.processing_fee(50.0, "", model, provider="bit")
        pp, _ = fc.processing_fee(50.0, "", model, provider="paypal")
        self.assertEqual(bit, 0.0)
        self.assertGreater(pp, 0.0)

    def test_an_unknown_provider_falls_back_to_the_default(self):
        model = fc.FeeModel(default_provider="bit")
        fee, _ = fc.processing_fee(50.0, "", model, provider="carrier-pigeon")
        self.assertEqual(fee, 0.0)

    def test_a_blank_cell_is_modelled_when_cards_are_in_use(self):
        # Once card processing is switched on, a blank cell means nobody
        # wrote the fee down — not that the sale was free to process.
        fee, source = fc.processing_fee(
            6.0, "", fc.FeeModel(default_provider="card",
                                 use_sheet_when_unknown=True))
        self.assertGreater(fee, 0)

    def test_a_sale_that_never_happened_is_not_charged_a_fee(self):
        sale = fc.build_sale(
            {"date": "16/07/2026 18:25:50", "sku": "GLOBAL-1GB",
             "sell": "", "buy": ""}, None, fc.FeeModel())
        self.assertEqual(sale.fee, 0.0)
        self.assertEqual(sale.fee_source, "אין מכירה")


class TestSale(unittest.TestCase):
    def _sale(self, sell, buy, fee=None, list_price=None, model=None,
              provider=None, discount=""):
        return fc.build_sale(
            {"date": "09/08/2026 08:03:25", "sku": "2.30.10", "sell": sell,
             "buy": buy, "region": "Greece", "order_id": "WR-TEST",
             "provider": provider, "discount": discount},
            {"fee": fee, "list": list_price}, model or fc.FeeModel())

    def test_profit_subtracts_both_the_supplier_and_the_processor(self):
        # The provider's contracted rate is what the money actually costs to
        # collect, so it outranks the price sheet's planning figure.
        model = fc.FeeModel()
        expected_fee = model.provider("card").fee(6.0)
        s = self._sale("6$", "4.17$", fee="0.20", provider="card")
        self.assertAlmostEqual(s.fee, expected_fee, places=4)
        self.assertAlmostEqual(s.profit, 6 - 4.17 - expected_fee, places=4)
        self.assertAlmostEqual(s.margin_pct, s.profit / 6 * 100, places=2)

    def test_profit_on_bit_is_just_price_minus_cost(self):
        s = self._sale("6$", "4.17$")
        self.assertAlmostEqual(s.profit, 6 - 4.17, places=4)

    def test_a_package_priced_a_cent_above_its_cost(self):
        # Sold at 7.00 against a 9.99 list, bought at 6.99. Paid by Bit, so
        # the whole margin is a single cent — the dashboard has to be able
        # to show a package priced this close to its cost. (The real 20GB
        # row was the owner buying on the company's account; that is an
        # internal order, covered in TestSummary.)
        s = self._sale("7$", "6.99$", list_price="9.99",
                       discount="30% ($2.99 הנחה)")
        self.assertAlmostEqual(s.profit, 0.01, places=4)
        self.assertLess(s.margin_pct, 1)
        self.assertTrue(s.discounted)
        self.assertAlmostEqual(s.discount_pct, 30.0, places=1)
        self.assertAlmostEqual(s.discount_usd, 2.99, places=2)

    def test_the_same_order_would_have_lost_money_on_a_card(self):
        s = self._sale("7$", "6.99$", list_price="9.99", provider="card")
        self.assertLess(s.profit, 0)

    def test_full_price_is_not_a_discount(self):
        s = self._sale("6$", "4.17$", list_price="6")
        self.assertFalse(s.discounted)
        self.assertEqual(s.discount_pct, 0.0)

    def test_a_sale_under_today_s_list_is_not_a_discount_by_itself(self):
        # The list price is rewritten daily. A sale from before a price move
        # sits under today's number without anyone having discounted it, and
        # 13 of the 19 orders the books called "discounted" were this. The
        # receipts sheet said "-" on every one of them.
        s = self._sale("6$", "4.17$", list_price="6.49")
        self.assertIsNone(s.discount_pct)
        self.assertFalse(s.discounted)
        self.assertEqual(s.discount_usd, 0.0)

    def test_a_dash_in_the_receipts_sheet_means_a_known_zero(self):
        s = self._sale("4.5$", "2$", list_price="7", discount="-")
        self.assertEqual(s.discount_pct, 0.0)
        self.assertFalse(s.discounted)

    def test_the_recorded_discount_outranks_today_s_list_price(self):
        s = self._sale("6$", "4.17$", list_price="99", discount="9% ($1.48 הנחה)")
        self.assertAlmostEqual(s.discount_pct, 9.0, places=2)
        self.assertAlmostEqual(s.discount_usd, 1.48, places=2)

    def test_a_giveaway_is_a_hundred_percent_off(self):
        s = self._sale("0$", "0.68$", list_price="0.68", discount="100% (0.68%)")
        self.assertAlmostEqual(s.discount_pct, 100.0, places=2)
        self.assertAlmostEqual(s.discount_usd, 0.68, places=2)

    def test_unknown_list_price_reports_no_discount_rather_than_zero(self):
        s = self._sale("6$", "4.17$", list_price="")
        self.assertIsNone(s.discount_pct)
        self.assertFalse(s.discounted)


class TestStripeReceipt(unittest.TestCase):
    def test_reads_the_real_supplier_receipt(self):
        e = fc.parse_stripe_receipt(
            STRIPE_SENDER, "Your ESIM.DOG receipt [#1958-0718]",
            STRIPE_BODY, utc(2026, 8, 11, 8, 13, 22))
        self.assertIsNotNone(e)
        self.assertEqual(e.amount, 4.17)
        self.assertEqual(e.currency, "USD")
        self.assertEqual(e.vendor, "ESIM.DOG")
        self.assertEqual(e.category, fc.COGS)
        self.assertIn("Cyprus", e.description)
        self.assertIn("1958-0718", e.note)
        self.assertTrue(e.receipt_url.startswith("https://dashboard.stripe.com/"))
        # Received 08:13 UTC — the sheet row is stamped 11:12 Israel time.
        self.assertEqual(e.when.hour, 11)

    def test_amount_paid_wins_over_the_other_dollar_signs_in_the_body(self):
        e = fc.parse_stripe_receipt(
            STRIPE_SENDER, "Your ESIM.DOG receipt [#1]",
            STRIPE_BODY.replace("Subtotal : $4.17", "Subtotal : $99.99"),
            utc(2026, 8, 11, 8, 13))
        self.assertEqual(e.amount, 4.17)

    def test_same_receipt_twice_is_the_same_row(self):
        args = (STRIPE_SENDER, "Your ESIM.DOG receipt [#1958-0718]",
                STRIPE_BODY, utc(2026, 8, 11, 8, 13, 22))
        self.assertEqual(fc.parse_stripe_receipt(*args).key,
                         fc.parse_stripe_receipt(*args).key)

    def test_two_different_receipts_are_two_rows(self):
        a = fc.parse_stripe_receipt(
            STRIPE_SENDER, "r", STRIPE_BODY, utc(2026, 8, 11, 8, 13))
        b = fc.parse_stripe_receipt(
            STRIPE_SENDER, "r", STRIPE_BODY.replace("1958-0718", "1822-7151"),
            utc(2026, 8, 11, 8, 12))
        self.assertNotEqual(a.key, b.key)

    def test_ignores_mail_that_is_not_from_stripe(self):
        self.assertIsNone(fc.parse_stripe_receipt(
            "orders@updates.esim.dog", "Your eSIM is ready!",
            STRIPE_BODY, utc(2026, 8, 11, 8, 13)))


class TestGenericReceipt(unittest.TestCase):
    def test_reads_a_recognised_vendor_invoice(self):
        e = fc.parse_generic_receipt(
            "invoice+statements@vercel.com", "Your Vercel invoice",
            "Thanks for your business.\nTotal: $20.00\n",
            utc(2026, 8, 1, 9, 0))
        self.assertIsNotNone(e)
        self.assertEqual(e.amount, 20.0)
        self.assertEqual(e.vendor, "Vercel")
        self.assertEqual(e.category, fc.INFRA)
        self.assertEqual(e.status, fc.CONFIRMED)

    def test_marketing_mail_with_a_price_in_it_is_not_an_expense(self):
        self.assertIsNone(fc.parse_generic_receipt(
            "deals@someshop.com", "Summer sale — everything from $9.99!",
            "Grab it now, only $9.99 today.", utc(2026, 8, 1)))

    def test_a_receipt_with_no_total_is_left_alone(self):
        self.assertIsNone(fc.parse_generic_receipt(
            "invoice@vercel.com", "Your invoice is ready",
            "Log in to view your invoice.", utc(2026, 8, 1)))

    def test_an_unknown_vendor_is_flagged_rather_than_counted(self):
        e = fc.parse_generic_receipt(
            "billing@some-new-tool.io", "Your receipt",
            "Amount paid: $12.00", utc(2026, 8, 1),
            known_vendors_only=False)
        self.assertIsNotNone(e)
        self.assertEqual(e.status, fc.NEEDS_REVIEW)

    def test_hebrew_invoice(self):
        e = fc.parse_generic_receipt(
            "billing@icount.co.il", "חשבונית מס",
            'סה"כ לתשלום: ₪117.00', utc(2026, 8, 1))
        self.assertIsNotNone(e)
        self.assertEqual(e.amount, 117.0)
        self.assertEqual(e.currency, "ILS")


class TestReconciliation(unittest.TestCase):
    def _charge(self, when, amount, tag=""):
        return fc.Expense(
            key=f"k{tag}{when.isoformat()}", when=when, vendor="ESIM.DOG",
            description="", category=fc.COGS, amount=amount,
            currency="USD", source="מייל")

    def _sale(self, when, buy, order_id):
        return fc.Sale(when=when, order_id=order_id, sku="2.30.10",
                       region="Greece", gb="10GB", customer="", status="",
                       sell=6.0, buy=buy, fee=0.21, fee_source="מוערך",
                       list_price=6.0)

    def test_the_nine_august_burst_pairs_one_to_one(self):
        # Eight charges of the same $4.17 minutes apart. Amount alone cannot
        # tell them apart, so the timestamps have to carry the matching.
        minutes = [3, 5, 7, 14, 16, 18, 21, 22]
        sales = [self._sale(il(2026, 8, 9, 8, m), 4.17, f"WR-{m}")
                 for m in minutes]
        charges = [self._charge(utc(2026, 8, 9, 5, m - 1), 4.17, str(m))
                   for m in minutes]

        matches = fc.reconcile(charges, sales)
        self.assertEqual(len(matches), 8)
        self.assertTrue(all(m.verdict == fc.MATCHED for m in matches))
        self.assertEqual(sorted(m.order_id for m in matches),
                         sorted(s.order_id for s in sales))

    def test_a_charge_with_no_sale_is_reported(self):
        # The 9 July $0.99 test purchase: real money, no customer.
        matches = fc.reconcile(
            [self._charge(utc(2026, 7, 9, 17, 40), 0.99)], [])
        self.assertEqual([m.verdict for m in matches], [fc.CHARGE_NO_SALE])

    def test_a_sale_whose_charge_never_arrived_is_reported(self):
        matches = fc.reconcile(
            [], [self._sale(il(2026, 8, 9, 8, 3), 4.17, "WR-1")])
        self.assertEqual([m.verdict for m in matches], [fc.SALE_NO_CHARGE])
        self.assertEqual(matches[0].order_id, "WR-1")

    def test_a_charge_far_from_any_sale_does_not_steal_a_match(self):
        sales = [self._sale(il(2026, 8, 9, 8, 3), 4.17, "WR-1")]
        charges = [self._charge(utc(2026, 8, 9, 5, 2), 4.17, "a"),
                   self._charge(utc(2026, 8, 1, 5, 2), 4.17, "b")]
        verdicts = sorted(m.verdict for m in fc.reconcile(charges, sales))
        self.assertEqual(verdicts, sorted([fc.MATCHED, fc.CHARGE_NO_SALE]))

    def test_amounts_a_cent_apart_still_pair(self):
        # The supplier's price drifts by a cent between the charge and the
        # row (4.17 vs 4.18); that is the same purchase.
        matches = fc.reconcile(
            [self._charge(utc(2026, 8, 6, 6, 38), 4.18)],
            [self._sale(il(2026, 8, 6, 9, 38), 4.17, "WR-1")])
        self.assertEqual(matches[0].verdict, fc.MATCHED)

    def test_a_sale_with_no_purchase_price_is_not_expected_to_have_a_charge(self):
        # The very first row was fulfilled by hand and has no buy price.
        matches = fc.reconcile([], [self._sale(il(2026, 7, 16, 18, 25), 0.0, "WR-W4WXC6")])
        self.assertEqual(matches, [])


class TestSummary(unittest.TestCase):
    def _sales(self):
        rows = []
        for m in (3, 5, 7):
            rows.append(fc.Sale(
                when=il(2026, 8, 9, 8, m), order_id=f"WR-{m}", sku="2.30.10",
                region="Greece", gb="10GB", customer="c@x.com", status="פעיל",
                sell=6.0, buy=4.17, fee=0.21, fee_source="מוערך",
                list_price=6.0))
        rows.append(fc.Sale(
            when=il(2026, 8, 9, 21, 57), order_id="WR-20", sku="2.30.20",
            region="Greece", gb="20GB", customer="c@x.com", status="פעיל",
            sell=7.0, buy=6.99, fee=0.23, fee_source="מוערך",
            list_price=9.99, discount_note="30% ($2.99 הנחה)"))
        return rows

    def test_headline_numbers(self):
        s = fc.summarise(self._sales(), [], lambda d: 3.0)
        self.assertEqual(s["orders"], 4)
        self.assertEqual(s["revenue"], 25.0)
        self.assertEqual(s["cogs"], round(4.17 * 3 + 6.99, 2))
        self.assertAlmostEqual(s["net_profit"],
                               25.0 - (4.17 * 3 + 6.99) - (0.21 * 3 + 0.23),
                               places=2)

    def test_counts_who_got_a_discount(self):
        s = fc.summarise(self._sales(), [], lambda d: 3.0)
        self.assertEqual(s["discounted_orders"], 1)
        self.assertEqual(s["discounted_pct"], 25.0)
        self.assertAlmostEqual(s["discount_given"], 2.99, places=2)

    def test_per_package_breakdown_separates_the_loser(self):
        s = fc.summarise(self._sales(), [], lambda d: 3.0)
        good = s["packages"]["2.30.10"]
        bad = s["packages"]["2.30.20"]
        self.assertEqual(good["orders"], 3)
        self.assertGreater(good["margin"], 25)
        self.assertLess(bad["margin"], 0)
        self.assertEqual(bad["discounted"], 1)

    def test_supplier_cost_is_not_counted_twice(self):
        # The Stripe receipts ARE the buy prices already inside the sales
        # rows. Adding them again as overhead would double the cost.
        cogs_expense = fc.Expense(
            key="k", when=il(2026, 8, 9, 8, 3), vendor="ESIM.DOG",
            description="", category=fc.COGS, amount=4.17, currency="USD",
            source="מייל")
        a = fc.summarise(self._sales(), [], lambda d: 3.0)
        b = fc.summarise(self._sales(), [cogs_expense], lambda d: 3.0)
        self.assertEqual(a["net_profit"], b["net_profit"])

    def test_overhead_reduces_the_net(self):
        infra = fc.Expense(
            key="k", when=il(2026, 8, 1), vendor="Vercel", description="",
            category=fc.INFRA, amount=20.0, currency="USD", source="מייל")
        a = fc.summarise(self._sales(), [], lambda d: 3.0)
        b = fc.summarise(self._sales(), [infra], lambda d: 3.0)
        self.assertAlmostEqual(b["net_profit"], a["net_profit"] - 20.0, places=2)

    def test_unreviewed_expenses_are_held_out_of_the_bottom_line(self):
        maybe = fc.Expense(
            key="k", when=il(2026, 8, 1), vendor="???", description="",
            category=fc.OTHER, amount=99.0, currency="USD", source="מייל",
            status=fc.NEEDS_REVIEW)
        a = fc.summarise(self._sales(), [], lambda d: 3.0)
        b = fc.summarise(self._sales(), [maybe], lambda d: 3.0)
        self.assertEqual(a["net_profit"], b["net_profit"])
        self.assertEqual(b["unreviewed"], 99.0)

    def test_shekel_expenses_are_converted_at_the_day_s_rate(self):
        ils = fc.Expense(
            key="k", when=il(2026, 8, 1), vendor="iCount", description="",
            category=fc.INFRA, amount=300.0, currency="ILS", source="מייל")
        s = fc.summarise(self._sales(), [ils], lambda d: 3.0)
        self.assertAlmostEqual(s["overhead"], 100.0, places=2)

    def test_months_are_split_apart(self):
        sales = self._sales()
        sales.append(fc.Sale(
            when=il(2026, 7, 23, 0, 36), order_id="WR-J", sku="1.972.01",
            region="Israel", gb="1GB", customer="", status="הסתיים",
            sell=1.0, buy=0.68, fee=0.11, fee_source="מחירון", list_price=1.0))
        s = fc.summarise(sales, [], lambda d: 3.0)
        self.assertEqual(sorted(s["months"]), ["2026-07", "2026-08"])
        self.assertEqual(s["months"]["2026-07"]["orders"], 1)
        self.assertEqual(s["months"]["2026-08"]["orders"], 4)

    def _internal(self):
        # A test order: really bought, deliberately zeroed on the sell side.
        return fc.Sale(
            when=il(2026, 8, 9, 21, 57), order_id="WR-TEST", sku="2.30.20",
            region="Greece", gb="20GB", customer="bhmi.9909.2@gmail.com",
            status="פעיל", sell=0.0, buy=6.99, fee=0.0,
            fee_source="אין מכירה", list_price=9.99)

    def test_a_bought_but_unsold_order_is_a_cost_not_a_failed_sale(self):
        s = fc.summarise(self._sales() + [self._internal()], [], lambda d: 3.0)
        base = fc.summarise(self._sales(), [], lambda d: 3.0)
        # It must not pretend to be revenue, an order, or a bad trade...
        self.assertEqual(s["orders"], base["orders"])
        self.assertEqual(s["revenue"], base["revenue"])
        self.assertEqual(s["gross_profit"], base["gross_profit"])
        # ...but the money really did leave, so the bottom line still feels
        # it, and the net margin drops accordingly.
        self.assertLess(s["margin"], base["margin"])
        self.assertEqual(s["internal_orders"], 1)
        self.assertAlmostEqual(s["internal_cost"], 6.99, places=2)
        self.assertAlmostEqual(s["net_profit"], base["net_profit"] - 6.99, places=2)

    def test_an_unsold_order_does_not_poison_its_package_s_margin(self):
        # 2.30.20 sells fine at full price; one zeroed test order must not
        # make the package look like a loss-maker.
        good = fc.Sale(
            when=il(2026, 8, 10, 9, 0), order_id="WR-OK", sku="2.30.20",
            region="Greece", gb="20GB", customer="c@x.com", status="פעיל",
            sell=9.99, buy=6.99, fee=0.30, fee_source="מוערך", list_price=9.99)
        s = fc.summarise([good, self._internal()], [], lambda d: 3.0)
        pkg = s["packages"]["2.30.20"]
        self.assertEqual(pkg["orders"], 1)
        self.assertGreater(pkg["margin"], 0)

    def test_an_unsold_order_still_expects_its_supplier_charge(self):
        # The card was charged for it, so reconciliation must still pair it.
        charge = fc.Expense(
            key="k", when=il(2026, 8, 9, 21, 57), vendor="ESIM.DOG",
            description="", category=fc.COGS, amount=6.99, currency="USD",
            source="מייל")
        matches = fc.reconcile([charge], [self._internal()])
        self.assertEqual([m.verdict for m in matches], [fc.MATCHED])

    def test_a_row_with_no_money_at_all_is_not_an_order(self):
        # The very first row: fulfilled by hand before any of this existed,
        # with neither a cost nor a price recorded.
        blank = fc.Sale(
            when=il(2026, 7, 16, 18, 25), order_id="WR-W4WXC6",
            sku="GLOBAL-1GB", region="Thailand", gb="1GB",
            customer="uper.request@gmail.com", status="הסתיים",
            sell=0.0, buy=0.0, fee=0.0, fee_source="אין מכירה",
            list_price=None)
        base = fc.summarise(self._sales(), [], lambda d: 3.0)
        s = fc.summarise(self._sales() + [blank], [], lambda d: 3.0)
        self.assertEqual(s["orders"], base["orders"])
        self.assertEqual(s["internal_orders"], 0)
        self.assertEqual(s["net_profit"], base["net_profit"])

    def test_internal_cost_lands_in_the_right_month(self):
        s = fc.summarise(self._sales() + [self._internal()], [], lambda d: 3.0)
        self.assertAlmostEqual(s["months"]["2026-08"]["internal"], 6.99, places=2)

    def test_flags_how_much_of_the_fee_total_is_a_guess(self):
        s = fc.summarise(self._sales(), [], lambda d: 3.0)
        self.assertAlmostEqual(s["modelled_fees"], s["fees"], places=2)


# ── 2026-10-20: gifts, superseded rows, shekel charges ──────────────────────
# The receipts rows below are written exactly as the PC bot writes them
# (bot/sheets.py): a gift's Purchase cell says 'free' and its Sell is 0.00$;
# a superseded placeholder says 'replaced' (Buy zeroed, proven unpaid) or
# 'replaced - check the charge' (Buy kept, may have been charged). WR-4YWATY /
# WR-T3QD3Q (receipts rows 188-189, 2026-10-11) is the case that started it:
# one customer, one payment, two rows that both read as a $6.99 sale.

REPLACED_CHECK = fc.REPLACED_STATUS + " - \u05dc\u05d1\u05d3\u05d5\u05e7 \u05d7\u05d9\u05d5\u05d1"


def row(sell, buy, status="\u05e4\u05e2\u05d9\u05dc", provider="paypal", order_id="WR-T3QD3Q",
        date="11/10/2026 20:00:49", discount="-"):
    return {"date": date, "sku": "2.0.10", "sell": sell, "buy": buy, "region": "Europe",
            "order_id": order_id, "status": status, "provider": provider,
            "discount": discount, "customer": "c@x.com", "gb": "10GB"}


def sale(r, model=None):
    return fc.build_sale(r, {"fee": "", "list": "6.99"}, model or fc.FeeModel())


class TestGiftsAndReplacedRows(unittest.TestCase):
    def _real(self):
        return sale(row("6.99$", "3.67$"))

    def test_a_gift_is_an_internal_cost_named_a_gift(self):
        g = sale(row("0.00$", "3.86$", provider=fc.FREE_RAIL, order_id="WR-GIFT01",
                     discount="100% ($6.99 \u05d4\u05e0\u05d7\u05d4)"))
        self.assertTrue(g.internal)
        self.assertTrue(g.gift)
        self.assertEqual(g.fee, 0.0)              # nothing was processed
        self.assertEqual(g.provider, fc.GIFT)     # not "Bit"
        base = fc.summarise([self._real()], [], lambda d: 3.0)
        s = fc.summarise([self._real(), g], [], lambda d: 3.0)
        # Zero revenue, the real cost, no extra order: exactly an internal one.
        self.assertEqual(s["orders"], base["orders"])
        self.assertEqual(s["revenue"], base["revenue"])
        self.assertAlmostEqual(s["internal_cost"], 3.86, places=2)
        self.assertEqual(s["gift_orders"], 1)
        self.assertAlmostEqual(s["gift_cost"], 3.86, places=2)
        self.assertAlmostEqual(s["net_profit"], base["net_profit"] - 3.86, places=2)

    def test_a_free_word_next_to_a_price_is_a_sale(self):
        s = sale(row("5.00$", "3.86$", provider=fc.FREE_RAIL))
        self.assertFalse(s.internal)
        self.assertFalse(s.gift)
        self.assertNotEqual(s.provider, fc.GIFT)

    def test_a_test_order_is_internal_but_not_a_gift(self):
        t = sale(row("0$", "0.68$", provider="bot - manually"))
        self.assertTrue(t.internal)
        self.assertFalse(t.gift)

    def test_a_proven_unpaid_placeholder_leaves_the_books(self):
        dead = sale(row("0.00$", "0.00$", status=fc.REPLACED_STATUS, order_id="WR-4YWATY",
                        date="11/10/2026 19:55:43", discount=""))
        self.assertTrue(dead.replaced)
        base = fc.summarise([self._real()], [], lambda d: 3.0)
        s = fc.summarise([self._real(), dead], [], lambda d: 3.0)
        self.assertEqual(s, base)
        self.assertEqual(fc.reconcile([], [dead]), [])   # no charge is expected

    def test_a_replaced_row_is_never_revenue_even_with_its_price_left_in(self):
        # Marked by hand: the Status typed, the $6.99 left in Sell. Counted,
        # the customer's one payment would be revenue twice.
        hand = sale(row("6.99$", "3.86$", status=REPLACED_CHECK, order_id="WR-4YWATY",
                        date="11/10/2026 19:55:43"))
        self.assertEqual(hand.sell, 0.0)
        self.assertTrue(hand.internal and hand.replaced and not hand.gift)
        s = fc.summarise([self._real(), hand], [], lambda d: 3.0)
        self.assertEqual(s["orders"], 1)
        self.assertAlmostEqual(s["revenue"], 6.99, places=2)
        # The charge may have happened, so its cost stays in the internal line.
        self.assertAlmostEqual(s["replaced_cost"], 3.86, places=2)
        self.assertEqual(s["replaced_orders"], 1)

    def test_reconcile_says_whether_the_replaced_attempt_was_charged(self):
        placeholder = sale(row("0.00$", "3.86$", status=REPLACED_CHECK, order_id="WR-4YWATY",
                               date="11/10/2026 19:56:00"))
        live = sale(row("6.99$", "3.86$", date="11/10/2026 20:01:00"))
        # One charge, CLOSER in time to the dead attempt than to the live one.
        # The live row still takes it: a superseded row is only ever matched
        # when nothing live fits.
        charge = fc.Expense(key="k1", when=il(2026, 10, 11, 19, 58), vendor="ESIM.DOG",
                            description="", category=fc.COGS, amount=3.86,
                            currency="USD", source="\u05de\u05d9\u05d9\u05dc")
        ms = {m.order_id: m for m in fc.reconcile([charge], [placeholder, live])}
        self.assertEqual(ms["WR-T3QD3Q"].verdict, fc.MATCHED)
        self.assertEqual(ms["WR-4YWATY"].verdict, fc.SALE_NO_CHARGE)
        self.assertEqual(ms["WR-4YWATY"].note, fc.REPLACED_NO_CHARGE)
        # A second charge for the same price: now the dead attempt WAS paid.
        charge2 = fc.Expense(key="k2", when=il(2026, 10, 11, 19, 56, 30), vendor="ESIM.DOG",
                             description="", category=fc.COGS, amount=3.86,
                             currency="USD", source="\u05de\u05d9\u05d9\u05dc")
        ms = {m.order_id: m for m in fc.reconcile([charge, charge2], [placeholder, live])}
        self.assertEqual(ms["WR-4YWATY"].verdict, fc.MATCHED)
        self.assertIn(fc.REPLACED_CHARGED, ms["WR-4YWATY"].note)


class TestShekelCharges(unittest.TestCase):
    """esim.dog's Stripe page opens in ₪ for an Israeli buyer; a switch to $
    that did not take left a few purchases charged in shekels. Compared raw,
    ₪11.96 never equalled a $3.86 Buy, and each one showed up twice."""

    def _charge(self, amount, currency="ILS", when=None):
        return fc.Expense(key=f"k{amount}", when=when or il(2026, 8, 30, 0, 52),
                          vendor="ESIM.DOG", description="", category=fc.COGS,
                          amount=amount, currency=currency, source="\u05de\u05d9\u05d9\u05dc")

    def _sale(self, buy=3.86):
        return fc.Sale(when=il(2026, 8, 30, 0, 52, 30), order_id="WR-ILS001",
                       sku="2.0.10", region="Europe", gb="10GB", customer="",
                       status="", sell=6.99, buy=buy, fee=0.0, fee_source="",
                       list_price=6.99)

    def test_the_stripe_receipt_says_shekels(self):
        body = STRIPE_BODY.replace("$4.17", "₪11.96")
        e = fc.parse_stripe_receipt(STRIPE_SENDER, "Your receipt from ESIM.DOG", body,
                                    utc(2026, 8, 30, 0, 52))
        self.assertEqual((e.amount, e.currency), (11.96, "ILS"))

    def test_a_shekel_charge_pairs_with_its_dollar_buy(self):
        # ₪11.96 at Stripe's 3.0986 is the $3.86 list price; at a market 3.0
        # it is $3.99 — Stripe's conversion fee, inside the 4% slack.
        [m] = fc.reconcile([self._charge(11.96)], [self._sale()], rate_for=lambda d: 3.0)
        self.assertEqual(m.verdict, fc.MATCHED)
        self.assertEqual(m.order_id, "WR-ILS001")
        self.assertAlmostEqual(m.amount, 3.99, places=2)        # shown in dollars
        self.assertIn(fc.ILS_CHARGE, m.note)

    def test_without_a_rate_it_is_not_guessed(self):
        ms = fc.reconcile([self._charge(11.96)], [self._sale()])
        self.assertEqual(sorted(m.verdict for m in ms),
                         sorted([fc.CHARGE_NO_SALE, fc.SALE_NO_CHARGE]))
        self.assertIn(fc.NO_RATE, next(m.note for m in ms if m.verdict == fc.CHARGE_NO_SALE))

    def test_a_shekel_charge_for_a_different_price_does_not_pair(self):
        ms = fc.reconcile([self._charge(15.00)], [self._sale()], rate_for=lambda d: 3.0)
        self.assertNotIn(fc.MATCHED, [m.verdict for m in ms])

    def test_dollar_charges_keep_their_two_cent_rule(self):
        # The shekel slack is for a conversion fee; a dollar receipt has none.
        ms = fc.reconcile([self._charge(3.96, currency="USD")], [self._sale()],
                          rate_for=lambda d: 3.0)
        self.assertNotIn(fc.MATCHED, [m.verdict for m in ms])
        [m] = fc.reconcile([self._charge(3.87, currency="USD")], [self._sale()],
                           rate_for=lambda d: 3.0)
        self.assertEqual((m.verdict, m.amount, m.note.startswith("\u05e4\u05e2\u05e8")),
                         (fc.MATCHED, 3.87, True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
