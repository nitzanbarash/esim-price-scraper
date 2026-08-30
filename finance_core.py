"""Pure money logic for the finance system — no network, no I/O.

Everything here is a plain function over plain data so it can be tested without
a mailbox, a spreadsheet or a clock. `finance_bot.py` does the talking to the
outside world and calls into this module for every decision that involves
money.

Two rules this module exists to enforce:

1. A number that was never measured is not zero. The price sheet leaves the
   processing fee blank on some packages; treating blank as 0 overstates the
   profit on those orders. `processing_fee` falls back to a modelled fee and
   says which of the two it used.
2. Times are compared in one timezone. The receipts sheet is written in Israel
   local time, the mail server speaks UTC, and the supplier's own receipts are
   stamped in a third zone. Every timestamp entering this module is converted
   at the door.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

try:                                     # py>=3.9 everywhere we run
    from zoneinfo import ZoneInfo
    IL = ZoneInfo("Asia/Jerusalem")
except Exception:                        # pragma: no cover - fallback only
    IL = timezone(timedelta(hours=3))

UTC = timezone.utc

# ---------------------------------------------------------------- categories

COGS = "עלות מכר"
PROCESSING = "סליקה"
INFRA = "תשתית"
MARKETING = "שיווק"
FEES = "עמלות"
OTHER = "אחר"

ALL_CATEGORIES = [COGS, PROCESSING, INFRA, MARKETING, FEES, OTHER]

# Vendor fingerprint -> (display name, category). Matched against the sender
# address and the subject, lowercased. First hit wins, so put the specific
# entries first.
VENDOR_RULES: list[tuple[str, str, str]] = [
    ("esim.dog", "ESIM.DOG", COGS),
    ("vercel", "Vercel", INFRA),
    ("upstash", "Upstash", INFRA),
    ("resend", "Resend", INFRA),
    ("cloudflare", "Cloudflare", INFRA),
    ("namecheap", "Namecheap", INFRA),
    ("godaddy", "GoDaddy", INFRA),
    ("google cloud", "Google Cloud", INFRA),
    ("google workspace", "Google Workspace", INFRA),
    ("googleadwords", "Google Ads", MARKETING),
    ("google ads", "Google Ads", MARKETING),
    ("facebookmail", "Meta Ads", MARKETING),
    ("meta platforms", "Meta Ads", MARKETING),
    ("tiktok", "TikTok Ads", MARKETING),
    ("icount", "iCount", PROCESSING),
    ("stripe", "Stripe", PROCESSING),
    ("paypal", "PayPal", PROCESSING),
    ("openai", "OpenAI", INFRA),
    ("anthropic", "Anthropic", INFRA),
    ("github", "GitHub", INFRA),
]

# A receipt we cannot confidently price or attribute lands here instead of
# being silently counted. The owner clears it with one tap in the web app.
NEEDS_REVIEW = "לבדיקה"
CONFIRMED = "מאושר"

# ------------------------------------------------------------------- parsing

_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?"

_CURRENCY_SIGNS = {
    "$": "USD", "usd": "USD", "us$": "USD",
    "₪": "ILS", "ils": "ILS", "nis": "ILS", "shekel": "ILS", 'ש"ח': "ILS",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
}


def parse_money(raw) -> tuple[float | None, str | None]:
    """'4.17$' / '$4.17' / '₪12.50' / 'USD 4.17' -> (4.17, 'USD').

    Returns (None, None) when there is no number at all. A bare number keeps
    its value and reports no currency, so the caller decides the default
    rather than this function guessing one.
    """
    if raw is None:
        return None, None
    if isinstance(raw, (int, float)):
        return float(raw), None

    s = str(raw).strip()
    if not s:
        return None, None

    m = re.search(_NUM, s)
    if not m:
        return None, None
    value = float(m.group().replace(",", ""))

    low = s.lower()
    currency = None
    for token, code in _CURRENCY_SIGNS.items():
        if token in low:
            currency = code
            break
    return value, currency


def parse_sheet_datetime(raw: str) -> datetime | None:
    """Receipts sheet stamps 'DD/MM/YYYY HH:MM:SS' in Israel local time."""
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=IL)
        except ValueError:
            continue
    return None


def to_israel(dt: datetime | None) -> datetime | None:
    """Every comparison in this system happens in Israel local time."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(IL)


def normalise_sku(sku: str, known: set[str]) -> str | None:
    """The site and the price sheet disagree about one zero: 1.A.3 vs 1.0A.3."""
    if not sku:
        return None
    sku = sku.strip()
    candidates = [sku]
    m = re.match(r"^(\d+)\.([A-Za-z])\.(.+)$", sku)
    if m:
        candidates.append(f"{m.group(1)}.0{m.group(2)}.{m.group(3)}")
    m = re.match(r"^(\d+)\.0([A-Za-z])\.(.+)$", sku)
    if m:
        candidates.append(f"{m.group(1)}.{m.group(2)}.{m.group(3)}")
    for c in candidates:
        if c in known:
            return c
    return None


def vendor_of(sender: str, subject: str) -> tuple[str, str]:
    """Map an email to (vendor, category). Unknown senders are not guessed."""
    hay = f"{sender} {subject}".lower()
    for token, name, category in VENDOR_RULES:
        if token in hay:
            return name, category
    domain = ""
    m = re.search(r"@([\w.-]+)", sender or "")
    if m:
        domain = m.group(1).split(".")[0]
    return (domain or "לא ידוע"), OTHER


# ----------------------------------------------------------------- fee model

@dataclass(frozen=True)
class Provider:
    """A way customers pay, and what it costs to receive money that way.

    `confirmed` marks the difference between a number we know and a number
    we assumed. Bit is confirmed at zero because that is simply how Bit
    works. The card rates are placeholders until a real settlement receipt
    proves them, and every row they touch says so.
    """
    name: str
    fixed: float = 0.0
    rate: float = 0.0
    confirmed: bool = False

    def fee(self, price: float) -> float:
        return round(self.fixed + self.rate * float(price or 0.0), 4)


# Every order so far was paid through Bit, which costs nothing to receive.
# PayMe and PayPal are planned for the site; their rates below are only
# stand-ins so a forecast is possible — replace them with the contracted
# numbers, or let a real provider receipt override them per order.
DEFAULT_PROVIDERS: dict[str, Provider] = {
    "bit":    Provider("Bit", 0.0, 0.0, confirmed=True),
    "payme":  Provider("PayMe", 0.0, 0.029),
    "paypal": Provider("PayPal", 0.0, 0.034),
    "card":   Provider("סליקת אשראי", 0.072, 0.0227),
}

NO_FEE = "ללא עמלה (Bit)"


@dataclass(frozen=True)
class FeeModel:
    """How to price the cost of collecting, per order.

    Order of preference, most trustworthy first:
      1. a fee the provider actually reported for that order
      2. the provider's contracted fixed+rate
      3. the price sheet's סליקה cell, which is a *planned* cost used to set
         prices rather than a charge that was ever incurred
    """
    default_provider: str = "bit"
    providers: dict = field(default_factory=lambda: dict(DEFAULT_PROVIDERS))
    use_sheet_when_unknown: bool = False

    def provider(self, name: str | None) -> Provider:
        key = (name or self.default_provider or "bit").strip().lower()
        return self.providers.get(key) or self.providers.get(
            self.default_provider, DEFAULT_PROVIDERS["bit"])

    def estimate(self, price: float, name: str | None = None) -> float:
        return self.provider(name).fee(price)


def processing_fee(sell_price: float, sheet_fee, model: FeeModel,
                   provider: str | None = None,
                   actual_fee=None) -> tuple[float, str]:
    """Return (fee, source) — the source names where the number came from."""
    known, _ = parse_money(actual_fee)
    if known is not None:
        return round(known, 4), "בפועל"

    p = model.provider(provider)
    if p.fixed or p.rate:
        return p.fee(sell_price), p.name if p.confirmed else f"{p.name} (הערכה)"

    if model.use_sheet_when_unknown:
        value, _ = parse_money(sheet_fee)
        if value is not None and value > 0:
            return round(value, 4), "מחירון"
    return 0.0, NO_FEE if p.confirmed else f"{p.name} (0)"


# --------------------------------------------------------------------- sales

@dataclass
class Sale:
    when: datetime | None
    order_id: str
    sku: str
    region: str
    gb: str
    customer: str
    status: str
    sell: float
    buy: float
    fee: float
    fee_source: str
    list_price: float | None
    provider: str = ""

    @property
    def internal(self) -> bool:
        """Bought, but never sold: a test order or one given away.

        The owner zeroes the sell price on these so they stop looking like
        revenue. Counting them as sales that lost money would drag the margin
        down and misdescribe them — the money was spent on testing, not lost
        on a customer. They are reported as their own cost line instead.
        """
        return self.sell <= 0 and self.buy > 0

    @property
    def profit(self) -> float:
        return round(self.sell - self.buy - self.fee, 4)

    @property
    def margin_pct(self) -> float | None:
        return round(self.profit / self.sell * 100, 2) if self.sell else None

    @property
    def discount_pct(self) -> float | None:
        """How far below the list price this one actually went out."""
        if not self.list_price or self.list_price <= 0 or not self.sell:
            return None
        return round((self.list_price - self.sell) / self.list_price * 100, 2)

    @property
    def discounted(self) -> bool:
        d = self.discount_pct
        # Half a percent of slack absorbs rounding in the price sheet.
        return d is not None and d > 0.5


def build_sale(row: dict, price_row: dict | None, model: FeeModel) -> Sale:
    """One receipts-sheet row -> one Sale, priced honestly."""
    sell, _ = parse_money(row.get("sell"))
    buy, _ = parse_money(row.get("buy"))
    sell = sell or 0.0
    buy = buy or 0.0

    provider = (row.get("provider") or "").strip()
    fee, fee_source = processing_fee(
        sell, (price_row or {}).get("fee"), model,
        provider=provider, actual_fee=row.get("fee_actual"))
    if not sell:
        # Nothing was charged, so nothing was processed.
        fee, fee_source = 0.0, "אין מכירה"

    list_price, _ = parse_money((price_row or {}).get("list"))

    return Sale(
        when=parse_sheet_datetime(row.get("date", "")),
        order_id=(row.get("order_id") or "").strip(),
        sku=(row.get("sku") or "").strip(),
        region=(row.get("region") or "").strip(),
        gb=(row.get("gb") or "").strip(),
        customer=(row.get("customer") or "").strip(),
        status=(row.get("status") or "").strip(),
        sell=round(sell, 4),
        buy=round(buy, 4),
        fee=fee,
        fee_source=fee_source,
        list_price=list_price,
        provider=model.provider(provider).name,
    )


# ------------------------------------------------------------------ expenses

@dataclass
class Expense:
    key: str                 # stable id — same input always yields the same row
    when: datetime | None
    vendor: str
    description: str
    category: str
    amount: float
    currency: str
    source: str              # מייל | ידני | צילום
    receipt_url: str = ""
    message_id: str = ""
    order_id: str = ""
    status: str = CONFIRMED
    note: str = ""
    amount_ils: float | None = None
    extra: dict = field(default_factory=dict)


def expense_key(source: str, message_id: str, when: datetime | None,
                amount: float, vendor: str) -> str:
    """Deterministic id so a re-run updates a row instead of adding one.

    Prefers the mail server's Message-ID, which is unique and stable. Falls
    back to the content itself for hand-entered rows.
    """
    if message_id:
        seed = f"{source}|{message_id}"
    else:
        stamp = when.isoformat() if when else ""
        seed = f"{source}|{stamp}|{amount:.4f}|{vendor}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


# The supplier's receipts arrive through Stripe and are the only record of
# what actually left the card. Body shape (verified against live mail):
#
#   Receipt from ESIM.DOG Receipt #1958-0718
#   Amount paid
#   $4.17
#   Date paid
#   Aug 11, 2026, 9:12:02 AM
#   Summary
#   eSIM Cyprus - 10GB (30 days validity)
_STRIPE_AMOUNT = re.compile(
    r"Amount\s+paid\s*[\r\n]+\s*([^\r\n]+)", re.I)
_STRIPE_RECEIPT_NO = re.compile(r"Receipt\s*#\s*([\w-]+)", re.I)
_STRIPE_SUMMARY = re.compile(
    r"Summary\s*[\r\n]+\s*([^\r\n]+)", re.I)
_STRIPE_DASHBOARD = re.compile(
    r"(https://dashboard\.stripe\.com/receipts/[^\s)]+)", re.I)


def parse_stripe_receipt(sender: str, subject: str, body: str,
                         received: datetime) -> Expense | None:
    """A Stripe receipt email -> one Expense, or None if it is not one."""
    if "stripe.com" not in (sender or "").lower():
        return None
    if "receipt" not in f"{subject} {body[:200]}".lower():
        return None

    amount_raw = ""
    m = _STRIPE_AMOUNT.search(body or "")
    if m:
        amount_raw = m.group(1)
    amount, currency = parse_money(amount_raw)
    if amount is None:
        return None

    vendor, category = vendor_of(sender, subject)
    # 'Receipt from ESIM.DOG' names the merchant far better than the Stripe
    # sender address does.
    # In the plain-text rendering the merchant name and the receipt number
    # share a line — "Receipt from ESIM.DOG Receipt #1958-0718" — so stop at
    # the second "Receipt" rather than swallowing it into the vendor.
    merchant = re.search(
        r"Receipt\s+from\s+(.+?)(?:\s+Receipt\s*#|\s*[\r\n]|$)",
        body or "", re.I)
    if merchant:
        name = merchant.group(1).strip(" \t-–—")
        if name:
            vendor = name
            _, category = vendor_of(f"{sender} {name}", subject)

    summary = ""
    m = _STRIPE_SUMMARY.search(body or "")
    if m:
        summary = m.group(1).strip()

    receipt_no = ""
    m = _STRIPE_RECEIPT_NO.search(f"{subject}\n{body}")
    if m:
        receipt_no = m.group(1)

    url = ""
    m = _STRIPE_DASHBOARD.search(body or "")
    if m:
        url = m.group(1)

    when = to_israel(received)
    message_id = (sender or "") + "|" + (receipt_no or "")
    return Expense(
        key=expense_key("mail", message_id, when, amount, vendor),
        when=when,
        vendor=vendor,
        description=summary or subject,
        category=category,
        amount=round(amount, 4),
        currency=currency or "USD",
        source="מייל",
        receipt_url=url,
        message_id=message_id,
        note=f"קבלה {receipt_no}" if receipt_no else "",
    )


# Anything that is not a Stripe receipt gets a much more careful reading: the
# main mailbox is a human's inbox, and a marketing email that happens to
# contain "$9.99" must not become an expense.
_RECEIPT_WORDS = re.compile(
    r"receipt|invoice|payment\s+(?:received|confirmation)|billed|"
    r"your\s+bill|חשבונית|קבלה|חיוב|תשלום", re.I)
_TOTAL_LINE = re.compile(
    r"(?:amount\s+paid|total|amount\s+due|grand\s+total|"
    r"sum|סה\"?כ|לתשלום|סכום)\s*[:\-]?\s*"
    r"([$₪€£]?\s*" + _NUM + r"\s*(?:USD|ILS|NIS|EUR|GBP|\$|₪|€|£)?)", re.I)


def parse_generic_receipt(sender: str, subject: str, body: str,
                          received: datetime,
                          known_vendors_only: bool = True) -> Expense | None:
    """Best-effort receipt reader for the owner's main mailbox.

    Deliberately conservative. A message only becomes an expense when it both
    looks like a receipt and yields a total. When the vendor is not one we
    recognise the row is still created but flagged for review, because a
    missed expense is worse than one that needs a tap to confirm.
    """
    hay = f"{subject}\n{body[:4000]}"
    if not _RECEIPT_WORDS.search(hay):
        return None

    vendor, category = vendor_of(sender, subject)
    recognised = vendor != "לא ידוע" and category != OTHER
    if known_vendors_only and not recognised:
        return None

    amount = currency = None
    for m in _TOTAL_LINE.finditer(hay):
        amount, currency = parse_money(m.group(1))
        if amount:
            break
    if not amount:
        return None

    when = to_israel(received)
    return Expense(
        key=expense_key("mail", "", when, amount, vendor),
        when=when,
        vendor=vendor,
        description=subject.strip(),
        category=category,
        amount=round(amount, 4),
        currency=currency or "ILS",
        source="מייל",
        status=CONFIRMED if recognised else NEEDS_REVIEW,
        note="" if recognised else "ספק לא מזוהה — לאשר או למחוק",
    )


# ------------------------------------------------------------ reconciliation

@dataclass
class Match:
    expense_key: str
    order_id: str
    verdict: str          # 'התאמה' | 'חיוב ללא מכירה' | 'מכירה ללא חיוב'
    amount: float
    when: datetime | None
    note: str = ""


MATCHED = "התאמה"
CHARGE_NO_SALE = "חיוב ללא מכירה"
SALE_NO_CHARGE = "מכירה ללא חיוב"


def reconcile(cogs: list[Expense], sales: list[Sale],
              window_minutes: int = 25,
              cents: float = 0.02) -> list[Match]:
    """Pair every supplier charge with the sale it paid for.

    The card is the ground truth: each Stripe receipt is money that really
    left. Each sale row claims a purchase price. They should be one-to-one.
    A charge with no sale is money spent on nothing; a sale with no charge
    means a purchase price was recorded that the card never saw.

    Greedy nearest-in-time matching on equal amounts. Purchases run seconds
    apart in bursts and repeat the same price, so amount alone cannot pair
    them — the timestamp breaks the tie.
    """
    unclaimed = [s for s in sales if s.buy > 0]
    used: set[int] = set()
    out: list[Match] = []

    for e in sorted(cogs, key=lambda x: x.when or datetime.min.replace(tzinfo=IL)):
        best_i, best_gap = None, None
        for i, s in enumerate(unclaimed):
            if i in used or not s.when or not e.when:
                continue
            if abs(s.buy - e.amount) > cents:
                continue
            gap = abs((s.when - e.when).total_seconds())
            if gap > window_minutes * 60:
                continue
            if best_gap is None or gap < best_gap:
                best_i, best_gap = i, gap
        if best_i is None:
            out.append(Match(e.key, "", CHARGE_NO_SALE, e.amount, e.when,
                             "חיוב מהספק שאין מולו שורת מכירה"))
        else:
            used.add(best_i)
            s = unclaimed[best_i]
            out.append(Match(e.key, s.order_id, MATCHED, e.amount, e.when,
                             f"פער {int(best_gap)} שניות"))

    for i, s in enumerate(unclaimed):
        if i not in used:
            out.append(Match("", s.order_id, SALE_NO_CHARGE, s.buy, s.when,
                             "שורת מכירה עם מחיר קנייה שלא נמצא לו חיוב"))
    return out


# ------------------------------------------------------------------ roll-ups

def month_key(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m") if dt else ""


def summarise(all_sales: list[Sale], expenses: list[Expense],
              rate_for) -> dict:
    """Headline numbers, in USD, plus a per-month and per-package breakdown.

    The business earns dollars and spends dollars, so USD is the unit here
    and no exchange rate touches the core cycle. `rate_for(date) -> float` is
    only used to bring a shekel expense into the same unit, and is passed in
    so this stays free of network calls.
    """
    # A row with no money on either side records something that happened,
    # not something that was traded — an order fulfilled by hand before the
    # books existed. It is neither revenue nor cost.
    priced = [s for s in all_sales if s.sell > 0 or s.buy > 0]

    # Orders that were bought but never sold are costs, not sales. Keeping
    # them in would report a negative margin on a package that sells fine.
    sales = [s for s in priced if not s.internal]
    internal = [s for s in priced if s.internal]
    internal_cost = round(sum(s.buy + s.fee for s in internal), 2)

    revenue = round(sum(s.sell for s in sales), 2)
    cogs = round(sum(s.buy for s in sales), 2)
    fees = round(sum(s.fee for s in sales), 2)
    modelled_fees = round(
        sum(s.fee for s in sales if s.fee_source == "מוערך"), 2)

    # Expenses already counted inside the sales rows must not be added twice.
    overhead = round(sum(
        usd(e, rate_for) for e in expenses
        if e.category not in (COGS,) and e.status != NEEDS_REVIEW), 2)
    unreviewed = round(sum(
        usd(e, rate_for) for e in expenses if e.status == NEEDS_REVIEW), 2)

    gross = round(revenue - cogs - fees, 2)
    net = round(gross - overhead - internal_cost, 2)

    def blank_month():
        return dict(revenue=0.0, cogs=0.0, fees=0.0, overhead=0.0,
                    internal=0.0, orders=0)

    months: dict[str, dict] = {}
    for s in sales:
        m = months.setdefault(month_key(s.when), blank_month())
        m["revenue"] += s.sell
        m["cogs"] += s.buy
        m["fees"] += s.fee
        m["orders"] += 1
    for s in internal:
        months.setdefault(month_key(s.when), blank_month())["internal"] += s.buy + s.fee
    for e in expenses:
        if e.category == COGS or e.status == NEEDS_REVIEW:
            continue
        months.setdefault(month_key(e.when), blank_month())["overhead"] += usd(e, rate_for)
    for k, m in months.items():
        for f in ("revenue", "cogs", "fees", "overhead", "internal"):
            m[f] = round(m[f], 2)
        m["net"] = round(m["revenue"] - m["cogs"] - m["fees"]
                         - m["overhead"] - m["internal"], 2)
        m["margin"] = round(m["net"] / m["revenue"] * 100, 1) if m["revenue"] else None

    packages: dict[str, dict] = {}
    for s in sales:
        p = packages.setdefault(s.sku, dict(
            sku=s.sku, region=s.region, gb=s.gb, orders=0, revenue=0.0,
            cogs=0.0, fees=0.0, discounted=0, discount_sum=0.0))
        p["orders"] += 1
        p["revenue"] += s.sell
        p["cogs"] += s.buy
        p["fees"] += s.fee
        if s.discounted:
            p["discounted"] += 1
            p["discount_sum"] += s.discount_pct or 0.0
        if not p["region"] and s.region:
            p["region"] = s.region
    for p in packages.values():
        for f in ("revenue", "cogs", "fees"):
            p[f] = round(p[f], 2)
        p["profit"] = round(p["revenue"] - p["cogs"] - p["fees"], 2)
        p["margin"] = round(p["profit"] / p["revenue"] * 100, 1) if p["revenue"] else None
        p["avg_discount"] = round(p["discount_sum"] / p["discounted"], 1) if p["discounted"] else None

    discounted = [s for s in sales if s.discounted]
    return dict(
        revenue=revenue, cogs=cogs, fees=fees, overhead=overhead,
        gross_profit=gross, net_profit=net,
        margin=round(net / revenue * 100, 1) if revenue else None,
        orders=len(sales),
        avg_order=round(revenue / len(sales), 2) if sales else 0.0,
        avg_profit=round(net / len(sales), 2) if sales else 0.0,
        modelled_fees=modelled_fees,
        unreviewed=unreviewed,
        internal_orders=len(internal),
        internal_cost=internal_cost,
        discounted_orders=len(discounted),
        discounted_pct=round(len(discounted) / len(sales) * 100, 1) if sales else 0.0,
        discount_given=round(sum(
            (s.list_price or s.sell) - s.sell for s in discounted), 2),
        months=months,
        packages=packages,
    )


def usd(e: Expense, rate_for) -> float:
    """An expense in whatever currency it was paid, expressed in dollars."""
    if e.currency == "USD":
        return e.amount
    rate = rate_for(e.when) or 0.0
    if e.currency == "ILS" and rate:
        return round(e.amount / rate, 4)
    return e.amount
