"""Bot #6 — the books.

Builds a complete picture of the business's money from sources that already
exist, and keeps it in one spreadsheet the owner can read on a phone:

  receipts sheet   -> what was sold, and for how much
  price sheet      -> the list price and the processing fee per package
  supply mailbox   -> the supplier's Stripe receipts: what actually left the card
  main mailbox     -> everything else the business pays for
  frankfurter.app  -> the USD/ILS rate on the day of each transaction

Runs on a schedule; nothing here needs to be running when a customer buys.

Two things it is careful about:

* **It never writes to a mailbox.** The supply inbox is shared with the
  fulfillment bot, which uses the \\Flagged marker as its own memory of what
  it has processed. This bot opens every mailbox read-only so it cannot
  disturb that, and remembers what it has seen in the spreadsheet instead.

* **It never overwrites the owner's own work.** Expenses typed by hand or
  photographed live in the same tab as the ones read from mail. A rebuild
  refreshes the machine-read fields and leaves every human decision —
  category, confirmation, note — exactly where it was.

Usage:
    python finance_bot.py                 # build everything
    python finance_bot.py --no-mail       # sheets only, no IMAP
    python finance_bot.py --dry-run       # compute and print, write nothing
    python finance_bot.py --init          # create the spreadsheet, print its id
"""

from __future__ import annotations

import argparse
import email
import email.utils
import imaplib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from email.header import decode_header, make_header

import gspread
import requests
from google.oauth2.service_account import Credentials

import finance_core as fc
from finance_core import (COGS, CONFIRMED, NEEDS_REVIEW, Expense, FeeModel,
                          Sale, build_sale, normalise_sku, parse_money,
                          parse_stripe_receipt, parse_generic_receipt,
                          reconcile, summarise, to_israel)

# --------------------------------------------------------------------- config

RECEIPTS_SHEET_ID = "1bWH_Zef0aNwZjLOR07hjJRZRXkrY73mX0aMLGPH6uao"
PRICE_SHEET_ID = "108D3BUV-MNcIuRZuKUgb-E-b1Ra8moxWZZyI5JxnyRo"

FINANCE_TITLE = "Waverole — כספים"
FINANCE_SHEET_ID = os.getenv("FINANCE_SHEET_ID", "").strip()

OWNER_EMAILS = [e.strip() for e in os.getenv(
    "FINANCE_SHARE_WITH",
    "uper.request@gmail.com,waverolesupply@gmail.com").split(",") if e.strip()]

SUPPLY_USER = os.getenv("GMAIL_USER", "waverolesupply@gmail.com")
SUPPLY_PASS_ENV = "GMAIL_APP_PASSWORD"
MAIN_USER = os.getenv("MAIN_GMAIL_USER", "uper.request@gmail.com")
MAIN_PASS_ENV = "MAIN_GMAIL_APP_PASSWORD"

IMAP_HOST = "imap.gmail.com"
IMAP_TIMEOUT = int(os.getenv("IMAP_TIMEOUT", "60"))
LOOKBACK_DAYS = int(os.getenv("FINANCE_LOOKBACK_DAYS", "400"))

# Tab names. Hebrew, because that is the language the owner reads the
# business in.
T_SUMMARY = "סקירה"
T_SALES = "מכירות"
T_EXPENSES = "הוצאות"
T_RECON = "התאמת ספק"
T_MONTHLY = "חודשי"
T_PACKAGES = "רווח לפי חבילה"
T_RATES = "שערי מטבע"
T_SETTINGS = "הגדרות"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------- sheets

# Filled in by sheet_client(); the finance sheet has to be shared with this
# address, and the setup guide prints it rather than hard-coding it.
SERVICE_ACCOUNT_EMAIL = ""


def sheet_client() -> gspread.Client:
    global SERVICE_ACCOUNT_EMAIL
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw:
        info = json.loads(raw)
        SERVICE_ACCOUNT_EMAIL = info.get("client_email", "")
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        # credentials.json first: it is the same scraper account that Actions
        # supplies through GOOGLE_CREDENTIALS_JSON, and the only one of the
        # two with the Drive API turned on, so a local run and a scheduled
        # run see exactly the same world.
        for path in (os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "credentials.json"),
                     "/Users/bhmis/Documents/esim-bot/service_account.json"):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    SERVICE_ACCOUNT_EMAIL = json.load(fh).get("client_email", "")
                creds = Credentials.from_service_account_file(path, scopes=SCOPES)
                break
        else:
            raise RuntimeError("no service-account credentials available")
    return gspread.authorize(creds)


def retry(fn, tries: int = 4, base: float = 1.5):
    """Sheets rate-limits; a couple of patient retries beats a failed run."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:                       # noqa: BLE001
            last = e
            if i == tries - 1:
                break
            time.sleep(base ** i)
    raise last


class NotSetUpYet(Exception):
    """The finance spreadsheet does not exist yet.

    It is deliberately created by the owner's own Apps Script rather than
    here: a service account has no Drive storage of its own any more, so a
    spreadsheet it created would have no owner who could keep it. The script
    creates it in the owner's Drive and hands this bot edit rights.
    """


def open_finance(gc: gspread.Client):
    """Find the books. Never creates them — see NotSetUpYet."""
    if FINANCE_SHEET_ID:
        try:
            return gc.open_by_key(FINANCE_SHEET_ID)
        except Exception as e:                       # noqa: BLE001
            raise NotSetUpYet(
                f"FINANCE_SHEET_ID is set but unreadable: {type(e).__name__}")
    try:
        return gc.open(FINANCE_TITLE)
    except gspread.SpreadsheetNotFound:
        raise NotSetUpYet(
            f"no spreadsheet named {FINANCE_TITLE!r} is shared with this "
            f"service account yet — run setup() in the finance Apps Script")


def read_receipts(gc: gspread.Client) -> list[dict]:
    """The sales ledger, addressed by header name rather than column letter.

    The bot that writes this sheet looks columns up by name for the same
    reason: the owner reorders them.
    """
    values = retry(lambda: gc.open_by_key(RECEIPTS_SHEET_ID).sheet1.get_all_values())
    if not values:
        return []
    head = [h.strip() for h in values[0]]

    def find(*needles) -> int | None:
        for i, h in enumerate(head):
            low = h.lower()
            if any(n.lower() in low for n in needles):
                return i
        return None

    cols = {
        "date": find("תאריך", "date"),
        "sku": find('מק"ט', "suk", "sku"),
        "gb": find("איחסון", "אחסון"),
        "order_id": find("מס׳ הזמנה", "מס' הזמנה", "הזמנה"),
        "region": find("אזור", "region"),
        "customer": find("מייל", "mail"),
        "status": find("סטטוס", "status"),
        "buy": find("קנייה", "buy"),
        "sell": find("מכירה", "sell"),
        # Optional. Add a column named e.g. "אמצעי תשלום" to the receipts
        # sheet and per-order fees start being counted from that instead of
        # the account-wide default.
        "provider": find("אמצעי תשלום", "תשלום דרך", "provider", "payment"),
        "fee_actual": find("עמלה בפועל", "עמלת סליקה"),
    }
    required = [k for k, v in cols.items()
                if v is None and k not in ("provider", "fee_actual")]
    if required:
        log(f"WARNING receipts sheet is missing columns: {required}")

    out = []
    for row in values[1:]:
        def cell(key):
            i = cols[key]
            return row[i] if i is not None and i < len(row) else ""
        rec = {k: cell(k) for k in cols}
        if not any(rec.values()):
            continue
        out.append(rec)
    return out


def read_prices(gc: gspread.Client) -> dict[str, dict]:
    values = retry(lambda: gc.open_by_key(PRICE_SHEET_ID).sheet1.get_all_values())
    if not values:
        return {}
    head = [h.strip() for h in values[0]]

    def find(*needles) -> int | None:
        for i, h in enumerate(head):
            if any(n in h for n in needles):
                return i
        return None

    c_sku = 0
    c_fee = find("סליקה")
    c_list = find("מחיר סופי")
    c_buy = find("מחיר קנייה")
    c_countries = find("מדינות")
    c_gb = find("GB")
    c_days = find("זמן חבילה")

    out: dict[str, dict] = {}
    for row in values[1:]:
        sku = (row[c_sku] if c_sku < len(row) else "").strip()
        if not sku:
            continue

        def cell(i):
            return row[i] if i is not None and i < len(row) else ""
        out[sku] = {
            "fee": cell(c_fee),
            "list": cell(c_list),
            "buy": cell(c_buy),
            "countries": cell(c_countries),
            "gb": cell(c_gb),
            "days": cell(c_days),
        }
    return out


# --------------------------------------------------------------------- mail

def _decode(raw) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:                                # noqa: BLE001
        return str(raw)


def _body(msg) -> str:
    """Plain text if the sender provided it, otherwise HTML stripped down."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            try:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8",
                                      errors="replace")
            except Exception:                        # noqa: BLE001
                continue
            if part.get_content_type() == "text/plain" and not plain:
                plain = text
            elif part.get_content_type() == "text/html" and not html:
                html = text
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8",
                                  errors="replace")
        except Exception:                            # noqa: BLE001
            text = ""
        if msg.get_content_type() == "text/html":
            html = text
        else:
            plain = text

    if plain.strip():
        return plain
    if not html:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&#8203;", ""), ("&zwnj;", "")):
        text = text.replace(a, b)
    text = re.sub(r"[ \t​‌ ]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


class Mailbox:
    """A read-only view of one Gmail account.

    Read-only is load-bearing, not a nicety: the fulfillment bot uses the
    \\Flagged marker on this same account to remember which delivery emails
    it has already handled. Opening the folder with readonly=True makes it
    impossible for the books to touch that.
    """

    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password
        self.box: imaplib.IMAP4_SSL | None = None

    def __enter__(self):
        self.box = imaplib.IMAP4_SSL(IMAP_HOST, timeout=IMAP_TIMEOUT)
        self.box.login(self.user, self.password.replace(" ", ""))
        # All Mail, so archived receipts are still found.
        for folder in ('"[Gmail]/All Mail"', '"[Gmail]/&APc-ll Mail"', "INBOX"):
            try:
                typ, _ = self.box.select(folder, readonly=True)
                if typ == "OK":
                    self.folder = folder
                    break
            except Exception:                        # noqa: BLE001
                continue
        else:
            raise RuntimeError(f"no readable folder on {self.user}")
        return self

    def __exit__(self, *exc):
        try:
            self.box.close()
        except Exception:                            # noqa: BLE001
            pass
        try:
            self.box.logout()
        except Exception:                            # noqa: BLE001
            pass
        return False

    def search(self, criteria: str) -> list[bytes]:
        try:
            typ, data = self.box.uid("SEARCH", None, criteria)
        except Exception as e:                       # noqa: BLE001
            log(f"  search failed ({criteria}): {type(e).__name__}")
            return []
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def fetch(self, uids: list[bytes]) -> list[tuple[str, str, str, datetime]]:
        """(sender, subject, body, received) for each uid, best effort."""
        out = []
        for uid in uids:
            try:
                typ, data = self.box.uid("FETCH", uid, "(RFC822)")
            except Exception as e:                   # noqa: BLE001
                log(f"  fetch {uid!r} failed: {type(e).__name__}")
                continue
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email.message_from_bytes(data[0][1])
            sender = _decode(msg.get("From"))
            subject = _decode(msg.get("Subject"))
            received = None
            try:
                received = email.utils.parsedate_to_datetime(msg.get("Date"))
            except Exception:                        # noqa: BLE001
                pass
            if received is None:
                continue
            out.append((sender, subject, _body(msg), received))
        return out


def _since() -> str:
    return (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")


def collect_supplier_receipts(password: str) -> list[Expense]:
    """Every Stripe receipt in the supply mailbox — the real cost of goods."""
    found: list[Expense] = []
    with Mailbox(SUPPLY_USER, password) as mb:
        uids = mb.search(f'(FROM "stripe.com" SINCE {_since()})')
        log(f"  {len(uids)} candidate supplier receipt(s)")
        for sender, subject, body, received in mb.fetch(uids):
            e = parse_stripe_receipt(sender, subject, body, received)
            if e:
                found.append(e)
    return found


def collect_other_expenses(password: str) -> list[Expense]:
    """Business overheads from the owner's main mailbox.

    A human inbox is mostly not receipts, so the search is narrowed on the
    server and every candidate still has to look like a receipt and yield a
    total before it is written down.
    """
    found: dict[str, Expense] = {}
    since = _since()
    terms = ["receipt", "invoice", "billing", "payment", "subscription"]
    with Mailbox(MAIN_USER, password) as mb:
        uids: set[bytes] = set()
        for term in terms:
            uids.update(mb.search(f'(SINCE {since} SUBJECT "{term}")'))
        log(f"  {len(uids)} candidate expense mail(s)")
        for sender, subject, body, received in mb.fetch(sorted(uids)):
            e = (parse_stripe_receipt(sender, subject, body, received)
                 or parse_generic_receipt(sender, subject, body, received))
            if e:
                found[e.key] = e
    return list(found.values())


# ----------------------------------------------------------------- fx rates

class Rates:
    """USD -> ILS on the day of the transaction, cached in the spreadsheet.

    An accountant wants the rate that applied when the money moved, not
    today's. Weekends and holidays have no published rate, so the nearest
    earlier day is used and the cache remembers the answer either way.
    """

    def __init__(self, cached: dict[str, float] | None = None,
                 fallback: float = 3.0, online: bool = True):
        self.cache = dict(cached or {})
        self.fallback = fallback
        self.online = online
        self.fetched = 0

    def for_date(self, dt) -> float:
        if dt is None:
            return self.fallback
        key = dt.strftime("%Y-%m-%d")
        if key in self.cache:
            return self.cache[key]
        if not self.online:
            return self.fallback
        rate = self._fetch(key)
        if rate:
            self.cache[key] = rate
            self.fetched += 1
            return rate
        # Nearest earlier cached day beats a constant.
        earlier = [k for k in self.cache if k <= key]
        if earlier:
            return self.cache[max(earlier)]
        return self.fallback

    def _fetch(self, key: str) -> float | None:
        try:
            r = requests.get(f"https://api.frankfurter.app/{key}",
                             params={"from": "USD", "to": "ILS"}, timeout=15)
            if r.status_code == 200:
                return float(r.json()["rates"]["ILS"])
        except Exception:                            # noqa: BLE001
            pass
        return None


# ------------------------------------------------------------------ writing

def ensure_tab(sh, title: str, rows: int = 200, cols: int = 20):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def write_tab(sh, title: str, header: list[str], rows: list[list],
              freeze: bool = True) -> None:
    ws = ensure_tab(sh, title, rows=max(len(rows) + 20, 100),
                    cols=max(len(header), 10))
    body = [header] + rows
    retry(lambda: ws.clear())
    if body:
        retry(lambda: ws.update(body, "A1", value_input_option="RAW"))
    if freeze:
        try:
            retry(lambda: ws.freeze(rows=1))
        except Exception:                            # noqa: BLE001
            pass


# Appended, not inserted: finance_app.gs hardcodes column 10 = סטטוס when
# reviewing a pending row, so existing positions must not shift.
EXPENSE_HEADER = ["מזהה", "תאריך", "ספק", "תיאור", "קטגוריה", "סכום",
                  "מטבע", 'סכום ש"ח', "מקור", "סטטוס", "קבלה",
                  "מזהה מייל", "הזמנה", "הערות", "סכום $"]

# Columns the owner owns. A rebuild refreshes everything else and leaves
# these exactly as the owner left them.
HUMAN_FIELDS = ("קטגוריה", "סטטוס", "הערות")


def read_existing_expenses(sh) -> dict[str, dict]:
    try:
        ws = sh.worksheet(T_EXPENSES)
    except gspread.WorksheetNotFound:
        return {}
    values = retry(lambda: ws.get_all_values())
    if len(values) < 2:
        return {}
    head = [h.strip() for h in values[0]]
    out = {}
    for row in values[1:]:
        rec = {head[i]: (row[i] if i < len(row) else "")
               for i in range(len(head))}
        key = rec.get("מזהה", "").strip()
        if key:
            out[key] = rec
    return out


def merge_expenses(fresh: list[Expense], existing: dict[str, dict],
                   rates: Rates) -> list[list]:
    """Machine facts refresh; human decisions survive; manual rows persist."""
    rows: dict[str, list] = {}

    def to_row(e: Expense, keep: dict | None) -> list:
        category = (keep or {}).get("קטגוריה") or e.category
        status = (keep or {}).get("סטטוס") or e.status
        note = (keep or {}).get("הערות") or e.note
        rate = rates.for_date(e.when)
        ils = (e.amount if e.currency == "ILS"
               else round(e.amount * rate, 2))
        # The book runs in USD (see finance-system memory, rule 1); ILS is
        # only an accountant line. rate is ILS per USD, so ILS->USD divides.
        usd = (e.amount if e.currency == "USD"
               else round(e.amount / rate, 2))
        return [
            e.key,
            e.when.strftime("%Y-%m-%d %H:%M") if e.when else "",
            e.vendor, e.description, category,
            round(e.amount, 2), e.currency, ils,
            e.source, status, e.receipt_url, e.message_id, e.order_id, note,
            usd,
        ]

    for e in fresh:
        rows[e.key] = to_row(e, existing.get(e.key))

    # Anything already in the sheet that this run did not produce: keep it.
    # That is every hand-entered and photographed expense, plus mail rows
    # that have aged out of the lookback window.
    for key, rec in existing.items():
        if key in rows:
            continue
        rows[key] = [rec.get(h, "") for h in EXPENSE_HEADER]

    def sort_key(r):
        return (str(r[1]) or "0000")
    return sorted(rows.values(), key=sort_key, reverse=True)


def fmt(v, nd=2):
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        return round(float(v), nd)
    return v


# --------------------------------------------------------------------- main

def build(gc, *, use_mail: bool = True) -> dict:
    log("reading sheets")
    receipts = read_receipts(gc)
    prices = read_prices(gc)
    log(f"  {len(receipts)} order row(s), {len(prices)} package(s) priced")

    # Customers pay by Bit today, which is free to receive. When PayMe or
    # PayPal go live on the site, set PAYMENT_PROVIDER (or fill a payment
    # column per order) and the fees start being counted automatically.
    providers = dict(fc.DEFAULT_PROVIDERS)
    for key in list(providers):
        rate = os.getenv(f"FEE_RATE_{key.upper()}")
        fixed = os.getenv(f"FEE_FIXED_{key.upper()}")
        if rate is None and fixed is None:
            continue
        p = providers[key]
        providers[key] = fc.Provider(
            p.name,
            float(fixed) if fixed is not None else p.fixed,
            float(rate) if rate is not None else p.rate,
            confirmed=True)
    model = FeeModel(
        default_provider=os.getenv("PAYMENT_PROVIDER", "bit"),
        providers=providers,
        use_sheet_when_unknown=os.getenv("FEE_FROM_SHEET", "") == "1")

    known = set(prices)
    sales: list[Sale] = []
    unpriced: set[str] = set()
    for row in receipts:
        sku = normalise_sku(row.get("sku", ""), known)
        if sku is None and row.get("sku"):
            unpriced.add(row["sku"].strip())
        sales.append(build_sale(row, prices.get(sku) if sku else None, model))
    if unpriced:
        log(f"  NOTE sold SKUs missing from the price sheet: {sorted(unpriced)}")

    expenses: list[Expense] = []
    mail_problems: list[str] = []
    if use_mail:
        supply_pw = os.getenv(SUPPLY_PASS_ENV, "").strip()
        if supply_pw:
            log(f"reading {SUPPLY_USER} (read-only)")
            try:
                got = collect_supplier_receipts(supply_pw)
                log(f"  {len(got)} supplier receipt(s)")
                expenses += got
            except Exception as e:                   # noqa: BLE001
                mail_problems.append(f"{SUPPLY_USER}: {type(e).__name__}")
                log(f"  FAILED {type(e).__name__}")
        else:
            mail_problems.append(f"{SUPPLY_PASS_ENV} not set")

        main_pw = os.getenv(MAIN_PASS_ENV, "").strip()
        if main_pw:
            log(f"reading {MAIN_USER} (read-only)")
            try:
                got = collect_other_expenses(main_pw)
                log(f"  {len(got)} expense(s)")
                expenses += got
            except Exception as e:                   # noqa: BLE001
                mail_problems.append(f"{MAIN_USER}: {type(e).__name__}")
                log(f"  FAILED {type(e).__name__}")
        else:
            mail_problems.append(
                f"{MAIN_PASS_ENV} not set — overheads from the main mailbox "
                f"are not being collected")

    return dict(sales=sales, expenses=expenses, prices=prices,
                mail_problems=mail_problems, model=model)


def publish(sh, data: dict, rates: Rates) -> dict:
    sales: list[Sale] = data["sales"]
    fresh: list[Expense] = data["expenses"]

    existing = read_existing_expenses(sh)
    expense_rows = merge_expenses(fresh, existing, rates)

    # Summaries run off what is actually in the sheet, so a hand-typed
    # expense counts the moment it is written, not on the next mail read.
    merged: list[Expense] = []
    for r in expense_rows:
        when = None
        try:
            when = datetime.strptime(str(r[1]), "%Y-%m-%d %H:%M").replace(tzinfo=fc.IL)
        except Exception:                            # noqa: BLE001
            try:
                when = datetime.strptime(str(r[1])[:10], "%Y-%m-%d").replace(tzinfo=fc.IL)
            except Exception:                        # noqa: BLE001
                pass
        amount, _ = parse_money(r[5])
        merged.append(Expense(
            key=r[0], when=when, vendor=r[2], description=r[3],
            category=r[4] or fc.OTHER, amount=amount or 0.0,
            currency=r[6] or "USD", source=r[8], status=r[9] or CONFIRMED,
            receipt_url=r[10], message_id=r[11], order_id=r[12], note=r[13]))

    summary = summarise(sales, merged, rates.for_date)
    cogs_expenses = [e for e in merged if e.category == COGS]
    matches = reconcile(cogs_expenses, sales)

    log("writing tabs")
    write_tab(sh, T_EXPENSES, EXPENSE_HEADER, expense_rows)

    sales_rows = []
    for s in sorted(sales, key=lambda x: x.when or datetime.min.replace(tzinfo=fc.IL),
                    reverse=True):
        sales_rows.append([
            s.when.strftime("%Y-%m-%d %H:%M") if s.when else "",
            s.order_id, s.sku, s.region, s.gb, s.customer, s.status,
            fmt(s.sell), fmt(s.buy), s.provider, fmt(s.fee), s.fee_source,
            fmt(s.profit), fmt(s.margin_pct, 1),
            fmt(s.list_price), fmt(s.discount_pct, 1),
            "כן" if s.discounted else "",
            "כן" if s.internal else "",
        ])
    write_tab(sh, T_SALES,
              ["תאריך", "מס׳ הזמנה", 'מק"ט', "אזור", "GB", "לקוח", "סטטוס",
               "מכירה $", "קנייה $", "אמצעי תשלום", "סליקה $", "מקור סליקה",
               "רווח $", "שולי רווח %", "מחירון $", "הנחה %", "ניתנה הנחה",
               "פנימי"],
              sales_rows)

    recon_rows = [[
        m.when.strftime("%Y-%m-%d %H:%M") if m.when else "",
        fmt(m.amount), m.verdict, m.order_id, m.note,
    ] for m in sorted(matches, key=lambda x: x.when or datetime.min.replace(tzinfo=fc.IL),
                      reverse=True)]
    write_tab(sh, T_RECON,
              ["תאריך", "סכום $", "תוצאה", "מס׳ הזמנה", "הערה"], recon_rows)

    month_rows = [[
        k, m["orders"], fmt(m["revenue"]), fmt(m["cogs"]), fmt(m["fees"]),
        fmt(m["overhead"]), fmt(m["internal"]), fmt(m["net"]),
        fmt(m["margin"], 1),
    ] for k, m in sorted(data_months(summary), reverse=True)]
    write_tab(sh, T_MONTHLY,
              ["חודש", "הזמנות", "הכנסות $", "עלות מכר $", "סליקה $",
               "הוצאות אחרות $", "הזמנות פנימיות $", "רווח נקי $",
               "שולי רווח %"], month_rows)

    pkg_rows = [[
        p["sku"], p["region"], p["gb"], p["orders"], fmt(p["revenue"]),
        fmt(p["cogs"]), fmt(p["fees"]), fmt(p["profit"]), fmt(p["margin"], 1),
        p["discounted"], fmt(p["avg_discount"], 1),
    ] for p in sorted(summary["packages"].values(),
                      key=lambda x: x["profit"], reverse=True)]
    write_tab(sh, T_PACKAGES,
              ['מק"ט', "אזור", "GB", "הזמנות", "הכנסות $", "עלות מכר $",
               "סליקה $", "רווח $", "שולי רווח %", "כמה קיבלו הנחה",
               "הנחה ממוצעת %"], pkg_rows)

    write_tab(sh, T_RATES, ["תאריך", "USD/ILS"],
              [[k, v] for k, v in sorted(rates.cache.items(), reverse=True)])

    write_summary(sh, summary, matches, data, rates)
    return summary


def data_months(summary: dict):
    return [(k, v) for k, v in summary["months"].items() if k]


def write_summary(sh, s: dict, matches, data: dict, rates: Rates) -> None:
    rate = rates.for_date(datetime.now(fc.IL))
    orphans = [m for m in matches if m.verdict == fc.CHARGE_NO_SALE]
    missing = [m for m in matches if m.verdict == fc.SALE_NO_CHARGE]

    rows = [
        ["עודכן", datetime.now(fc.IL).strftime("%Y-%m-%d %H:%M"), ""],
        ["", "", ""],
        ["— שורה תחתונה —", "", ""],
        ["הכנסות", s["revenue"], f'≈ {round(s["revenue"] * rate)} ש"ח'],
        ["עלות מכר (ספק)", -s["cogs"], ""],
        ["עמלות סליקה", -s["fees"],
         f'מתוכן {s["modelled_fees"]}$ מוערכים' if s["modelled_fees"] else ""],
        ["רווח גולמי", s["gross_profit"], ""],
        ["הוצאות אחרות", -s["overhead"], ""],
        ["הזמנות פנימיות / בדיקה", -s["internal_cost"],
         f'{s["internal_orders"]} הזמנות שנקנו ולא נמכרו'
         if s["internal_orders"] else ""],
        ["רווח נקי", s["net_profit"], f'≈ {round(s["net_profit"] * rate)} ש"ח'],
        ["שולי רווח %", s["margin"], ""],
        ["", "", ""],
        ["— תנועה —", "", ""],
        ["מספר הזמנות", s["orders"], "מכירות אמיתיות בלבד"],
        ["הכנסה ממוצעת להזמנה", s["avg_order"], ""],
        ["רווח ממוצע להזמנה", s["avg_profit"], ""],
        ["", "", ""],
        ["— הנחות —", "", ""],
        ["הזמנות שקיבלו הנחה", s["discounted_orders"],
         f'{s["discounted_pct"]}% מההזמנות'],
        ["סך ההנחות שניתנו", s["discount_given"], ""],
        ["", "", ""],
        ["— בקרה —", "", ""],
        ["חיובי ספק ללא מכירה", len(orphans),
         "כסף שיצא בלי הזמנה מולו" if orphans else "תקין"],
        ["מכירות ללא חיוב ספק", len(missing),
         "מחיר קנייה שלא נמצא לו חיוב" if missing else "תקין"],
        ["הוצאות שממתינות לאישור", s["unreviewed"],
         "לא נספרות ברווח עד לאישור" if s["unreviewed"] else "אין"],
        ["שער דולר בשימוש", rate, ""],
    ]
    for problem in data.get("mail_problems", []):
        rows.append(["⚠ מייל", problem, ""])

    write_tab(sh, T_SUMMARY, ["", "ערך", "הערה"],
              [[a, fmt(b), c] for a, b, c in rows], freeze=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-mail", action="store_true",
                    help="skip IMAP; build from the spreadsheets alone")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print, write nothing")
    ap.add_argument("--whoami", action="store_true",
                    help="print the account the finance sheet must be shared with")
    ap.add_argument("--offline-fx", action="store_true",
                    help="do not call the rates API")
    args = ap.parse_args()

    try:
        gc = sheet_client()
    except Exception as e:                           # noqa: BLE001
        log(f"FATAL cannot authenticate to Google: {e}")
        return 1

    if args.whoami:
        print(SERVICE_ACCOUNT_EMAIL or "(unknown)")
        return 0

    try:
        data = build(gc, use_mail=not args.no_mail)
    except Exception as e:                           # noqa: BLE001
        log(f"FATAL build failed: {e}")
        traceback.print_exc()
        return 1

    cached: dict[str, float] = {}
    sh = None
    if not args.dry_run:
        try:
            sh = open_finance(gc)
        except NotSetUpYet as e:
            # Not an error: the books simply have not been created yet. A red
            # run every hour until the owner is back would train them to
            # ignore the alert that matters.
            log(f"nothing to write to — {e}")
            return 0
        try:
            ws = sh.worksheet(T_RATES)
            for row in retry(lambda: ws.get_all_values())[1:]:
                if len(row) >= 2 and row[0]:
                    try:
                        cached[row[0]] = float(row[1])
                    except ValueError:
                        pass
        except gspread.WorksheetNotFound:
            pass

    rates = Rates(cached, fallback=float(os.getenv("USD_ILS", "3.0")),
                  online=not args.offline_fx)

    if args.dry_run:
        summary = summarise(data["sales"], data["expenses"], rates.for_date)
        print(json.dumps({k: v for k, v in summary.items()
                          if k not in ("months", "packages")},
                         ensure_ascii=False, indent=2))
        for k, m in sorted(summary["months"].items()):
            print(f"  {k}: revenue {m['revenue']:>8.2f}  net {m['net']:>8.2f}"
                  f"  orders {m['orders']}")
        return 0

    summary = publish(sh, data, rates)
    log(f"done — revenue ${summary['revenue']} net ${summary['net_profit']} "
        f"({summary['margin']}%) across {summary['orders']} order(s)")
    log(f"https://docs.google.com/spreadsheets/d/{sh.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
