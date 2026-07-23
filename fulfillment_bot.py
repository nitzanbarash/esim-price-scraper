#!/usr/bin/env python3
"""
Order-fulfillment bot (bot #4) — runs in GitHub Actions ("on the internet").

What it does, every run (stateless — safe to run as often as you like):
  1. Reads the waverolesupply@gmail.com inbox over IMAP and picks up every
     UNFLAGGED "Your eSIM is ready!" email from orders@updates.esim.dog.
  2. Reads the eSIM for that order's session id from the supplier's JSON
     endpoint — activation code / SM-DP+ / ICCID / APN / QR in one request.
     (This used to drive a headless browser over the supplier's JavaScript
     order page. The page reads the same endpoint we now call directly, so
     the browser cost a minute of CI per order for nothing — and it had
     stopped finding the ICCID and APN after a label change.)
  3. Matches the email to the oldest compatible PENDING row in the receipts
     Google Sheet (has order number, no activation yet, same GB, compatible
     location, purchased before the email within MATCH_WINDOW_HOURS).
     Two look-alike candidates within 3 minutes -> alert, never guess.
  4. Completes the sheet row, then POSTs fulfillment to waverole.com so the
     customer's order page shows the QR and the site emails them.
  5. Flags the email (IMAP \\Flagged) so it is never processed twice.
     A run that could not extract details leaves the email unflagged and
     the next run retries automatically.

Required environment (GitHub Secrets):
  GOOGLE_CREDENTIALS_JSON  service-account JSON (same one the scraper uses;
                           share the receipts sheet with it as Editor!)
  GMAIL_APP_PASSWORD       app password for waverolesupply@gmail.com
  ORDERS_TOKEN             bearer token of waverole.com/api/orders
"""

import base64
import email
import email.header
import email.utils
import imaplib
import json
import logging
import os
import re
import smtplib
import tempfile
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart

import gspread
import requests

from esim_country_data import COUNTRY_DATA

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("fulfillment")

# ── constants ────────────────────────────────────────────────────────────────
GMAIL_USER = os.getenv("GMAIL_USER", "waverolesupply@gmail.com")
ALERTS_EMAIL = os.getenv("ALERTS_EMAIL", "uper.request@gmail.com")
DELIVERY_FROM = "orders@updates.esim.dog"
DELIVERY_SUBJECT = "Your eSIM is ready"
RECEIPTS_SHEET_ID = "1bWH_Zef0aNwZjLOR07hjJRZRXkrY73mX0aMLGPH6uao"
ORDERS_URL = "https://www.waverole.com/api/orders"
MATCH_WINDOW_HOURS = int(os.getenv("MATCH_WINDOW_HOURS", "12"))
UNMATCHED_GRACE_HOURS = 2  # how long an unmatched delivery email keeps retrying
LOOKBACK_DAYS = 7          # only consider emails from the last week
TZ = ZoneInfo("Asia/Jerusalem")

# Both live formats seen in real delivery emails: ?session_id=cs_live_... and
# ?payment_intent=pi_... (older orders).
SUCCESS_URL_RE = re.compile(r"https://esim\.dog/success\?(?:session_id|payment_intent)=[A-Za-z0-9_\-]+")
PLAN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*GB\s*[-–]\s*(\d+)\s*days?", re.I)
LPA_RE = re.compile(r"LPA:1\$[^\s\"'<>]+\$[A-Za-z0-9\-_]+")
# The iPhone/Android install links carry the LPA in ?carddata= (URL-encoded:
# LPA%3A1%24smdp%24code). This is the MOST reliable source — present without
# expanding "eSIM Details" or decoding the QR.
CARDDATA_RE = re.compile(r"carddata=(LPA(?:%3A|:)1(?:%24|\$)[^\"'&\s<>]+)", re.I)
ICCID_RE = re.compile(r"\b(89\d{17,18})\b")
# APN in raw HTML only works for dotted values ("internet.provider.com"); real
# pages also use bare words (seen live: "wbdata"), which only appear cleanly in
# the page TEXT as a label/value pair after expanding "eSIM Details".
APN_RE = re.compile(r"APN[^A-Za-z0-9]{0,20}([a-z0-9.\-]+\.[a-z]{2,})", re.I)
APN_TEXT_RE = re.compile(r"\bAPN\b\s*\n+\s*(?!Copy\b)([A-Za-z][A-Za-z0-9._\-]{1,40})\s*\n")
REGION_TEXT_RE = re.compile(r"\bRegion\b\s*\n+\s*([A-Za-z][A-Za-z ,()&\-]{1,40})\s*\n")


def env(name: str) -> str:
    # RuntimeError, never sys.exit: SystemExit does not inherit from Exception,
    # so an `except Exception` around a step (site report, customer email) would
    # NOT catch it and one missing secret would kill the whole run mid-order —
    # skipping every step after it. Raising normally lets each step fail, alert
    # and carry on; a secret missing at startup still aborts the run loudly.
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env/secret: {name}")
    return v


def _redact(s: str) -> str:
    """This repo is PUBLIC, so Actions logs are public. A success-page URL in a
    log line (e.g. inside a Playwright error message) would hand anyone the
    eSIM activation page AND reveal the supplier. Strip the tokens."""
    return re.sub(r"(session_id|payment_intent)=[A-Za-z0-9_\-]+", r"\1=REDACTED", s)


def order_payload(order_url: str) -> dict:
    """Decode the ?order= base64 JSON of a waverole.com order link
    ({id, sku, ts, t}) — `t` is the total the customer actually paid."""
    try:
        b64 = order_url.split("order=", 1)[1].split("&", 1)[0]
        b64 += "=" * (-len(b64) % 4)
        return json.loads(base64.urlsafe_b64decode(b64))
    except Exception:
        return {}


# ── alerts ───────────────────────────────────────────────────────────────────

def alert(subject: str, body: str):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = GMAIL_USER
        msg["To"] = ALERTS_EMAIL
        msg["Subject"] = f"[fulfillment bot] {subject}"
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(GMAIL_USER, env("GMAIL_APP_PASSWORD").replace(" ", ""))
            s.send_message(msg)
    except Exception:
        log.exception("alert email failed")


# ── inbox ────────────────────────────────────────────────────────────────────

def _decode(s) -> str:
    if not s:
        return ""
    return "".join(
        p.decode(enc or "utf-8", "replace") if isinstance(p, bytes) else p
        for p, enc in email.header.decode_header(s)
    )


def _body_text(msg) -> str:
    chunks = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload:
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    return "\n".join(chunks)


# Mail wraps long lines at ~76 characters, and a session id is long — so the
# link arrives split in two and a plain search returns only the first half.
# That truncated link 404s when opened, which is why a saved supplier link
# sometimes did not work. A newline right around the wrap column is a wrap; one
# after a short or an over-long line is a real break in the text, and stitching
# it would glue the next words onto the URL.
_WRAP_COLS = (66, 82)


def _unwrap(text: str) -> str:
    text = re.sub(r"=\r?\n", "", text)            # quoted-printable soft break
    lines = text.replace("\r\n", "\n").split("\n")
    out = lines[:1]
    for prev, line in zip(lines, lines[1:]):
        wrapped = _WRAP_COLS[0] <= len(prev) <= _WRAP_COLS[1]
        if wrapped and line[:1] and (line[0].isalnum() or line[0] in "_-"):
            out[-1] += line
        else:
            out.append(line)
    return "\n".join(out)


def find_success_urls(text: str) -> list[str]:
    """Every plausible reading of the email's supplier link, longest first.

    Un-wrapping is a guess — we cannot know from the text alone where the id
    ends. So we do not have to: the supplier decides. fetch_esim_details tries
    these in order and the real link is the one that answers with an eSIM.
    """
    plain = {m.group(0) for m in SUCCESS_URL_RE.finditer(text)}
    joined = {m.group(0) for m in SUCCESS_URL_RE.finditer(_unwrap(text))}
    # A stitched candidate is only worth trying if it extends one we saw
    # intact — that way a bad join can never invent an unrelated link.
    repaired = {j for j in joined if any(j.startswith(p) for p in plain)}
    return sorted(plain | repaired, key=len, reverse=True)


def find_success_url(text: str) -> str:
    urls = find_success_urls(text)
    return urls[0] if urls else ""


def parse_delivery(uid: str, msg) -> dict | None:
    text = _body_text(msg)
    success_urls = find_success_urls(text)
    if not success_urls:
        return None
    out = {"uid": uid, "success_url": success_urls[0], "success_urls": success_urls,
           "gb": None, "days": None,
           "location": "", "network": "", "received_at": None}
    if m := PLAN_RE.search(text):
        out["gb"], out["days"] = float(m.group(1)), int(m.group(2))
    if m := re.search(r"📍\s*([A-Za-z ,()&\-]+)", text):
        out["location"] = m.group(1).strip()
    if m := re.search(r"📶\s*([^\n<]+)", text):
        out["network"] = m.group(1).strip()
    try:
        out["received_at"] = email.utils.parsedate_to_datetime(msg.get("Date")).astimezone(timezone.utc)
    except Exception:
        out["received_at"] = datetime.now(timezone.utc)
    return out


def _all_mail_folder(box) -> str:
    """Gmail's "All Mail" folder — sees every message regardless of which
    label/filter it landed under (a filter that skips the inbox would hide
    the email from an INBOX-only search). Found via the \\All special-use
    attribute so it works in any UI language; INBOX is the last resort."""
    try:
        typ, folders = box.list()
        if typ == "OK":
            for raw in folders:
                line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
                if "\\All" in line:
                    # ...(\HasNoChildren \All) "/" "[Gmail]/All Mail"
                    name = line.split(' "/" ')[-1].strip().strip('"')
                    if name:
                        return name
    except Exception as e:
        log.warning(f"folder list failed ({e}) — using INBOX")
    return "INBOX"


class Inbox:
    def __init__(self):
        self.box = imaplib.IMAP4_SSL("imap.gmail.com")
        self.box.login(GMAIL_USER, env("GMAIL_APP_PASSWORD").replace(" ", ""))
        folder = _all_mail_folder(self.box)
        log.info(f"searching folder: {folder}")
        # imaplib needs the mailbox name quoted when it contains spaces.
        self.box.select(f'"{folder}"' if " " in folder else folder)

    def unprocessed(self) -> list[dict]:
        # No FROM filter in the IMAP query: forwarded copies (Fwd:) come from
        # the owner's address, not esim.dog. Validation happens in Python —
        # the subject must match AND the body must carry an esim.dog success
        # link (parse_delivery returns None without one), which is a stronger
        # signal than the envelope sender anyway.
        since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        typ, data = self.box.uid("search", None, f"(UNFLAGGED SINCE {since})")
        out = []
        for uid_b in (data[0] or b"").split():
            uid = uid_b.decode()
            typ, sub = self.box.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
            if typ != "OK" or not sub or sub[0] is None:
                continue
            head = email.message_from_bytes(sub[0][1])
            subject = _decode(head.get("Subject"))
            if DELIVERY_SUBJECT.lower() not in subject.lower():
                continue                        # cheap header-only skip
            # Our own outgoing customer email ("Your eSIM is ready to use!")
            # sits in All Mail (Sent) and matches the subject — skip self-sent
            # so we never full-fetch our own mail every run.
            if GMAIL_USER.lower() in _decode(head.get("From")).lower():
                continue
            typ, msgdata = self.box.uid("fetch", uid, "(RFC822)")
            if typ != "OK" or not msgdata or msgdata[0] is None:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            if d := parse_delivery(uid, msg):
                out.append(d)
        return out

    def flag(self, uid: str):
        self.box.uid("store", uid, "+FLAGS", "(\\Flagged)")

    def close(self):
        try:
            self.box.logout()
        except Exception:
            pass


# ── success page scraping ────────────────────────────────────────────────────

# ── supplier lookup ──────────────────────────────────────────────────────────
SUPPLIER_ESIM_URL = "https://esim.dog/.netlify/functions/get-esim"
SESSION_ID_RE = re.compile(r"(?:session_id|payment_intent)=([A-Za-z0-9_\-]+)")


def fetch_esim_details(success_url) -> dict:
    """Accepts one URL or several candidate readings of it (longest first).
    The supplier settles which one is real: a truncated or mis-stitched id
    simply does not resolve. The winner comes back as `used_url` so the
    receipts row records a link that actually opens."""
    urls = [success_url] if isinstance(success_url, str) else list(success_url or [])
    for u in urls:
        if got := _fetch_one_esim(u):
            got["used_url"] = u
            return got
    return {}


def _fetch_one_esim(success_url: str) -> dict:
    """Read the finished eSIM straight from the supplier's own JSON endpoint.

    This used to drive a headless browser: the supplier's order page is a
    JavaScript app, so every completion cost a Playwright run on a CI machine
    that took a minute just to boot — and it had stopped finding the ICCID and
    APN, which sit behind a toggle whose label changed. The page reads its data
    from a plain endpoint, and so do we: one request, every field present.

    Returns {} while the eSIM is still being provisioned, so the caller simply
    retries on its next run.
    """
    m = SESSION_ID_RE.search(success_url or "")
    if not m:
        return {}
    try:
        r = requests.get(SUPPLIER_ESIM_URL, params={"session_id": m.group(1)},
                         headers={"Accept": "application/json",
                                  "User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning(f"supplier lookup failed: {_redact(str(e))}")
        return {}

    s, e = (data.get("session") or {}), (data.get("esim") or {})
    if not e.get("activation_code"):
        return {}
    code = str(e["activation_code"])
    out = {
        "activation_code": code,
        "smdp": str(e.get("smdp_address") or (code.split("$")[1] if "$" in code else "")),
        "iccid": str(e.get("iccid", "")),
        "apn": str(e.get("apn", "")),
        "qr_code": str(e.get("qr_code", "")),
        "page_region": str(s.get("country_name", "")),
    }
    if gb := re.sub(r"[^\d.]", "", str(s.get("plan_data", ""))):
        out["plan_gb"] = float(gb)
    if days := re.sub(r"[^\d]", "", str(s.get("plan_validity", ""))):
        out["plan_days"] = int(days)
    if nets := " • ".join(x for x in (s.get("coverage"), s.get("networks")) if x):
        out["networks"] = nets
    return out


# ── receipts sheet ───────────────────────────────────────────────────────────

class AmbiguousMatch(Exception):
    pass


def sheet_client():
    creds = env("GOOGLE_CREDENTIALS_JSON")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write(creds)
        path = f.name
    return gspread.service_account(filename=path)


def _row_time(s: str):
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    return None


def _link_ok(link: str, location: str) -> bool:
    if not location or not link:
        return True
    l, loc = link.lower(), location.lower()
    if loc.replace(" ", "-") in l:
        return True
    # Single-country links carry only the ISO code (esim.dog/il), never the
    # country name — every such order failed the match until mapped here
    # (first hit: the il test SKU, order WR-845JFY).
    m = re.search(r"esim\.dog/([a-z]{2})(?:[/?#]|$)", l)
    if m and m.group(1) in COUNTRY_DATA:
        name = COUNTRY_DATA[m.group(1)][0].lower()
        if name in loc or loc in name:
            return True
    m = re.search(r"[?&]region=([a-z\-]+)", l)
    if m and m.group(1) in loc:
        return True
    return "region=" in l


def find_pending_row(ws, delivery: dict) -> dict | None:
    rows = ws.get_all_values()
    hdr = [h.strip() for h in rows[0]]
    required = ["תאריך - Date", "איחסון - GB", "מס׳ הזמנה", "Activation Code",
                "מס סידורי -ICCID", "מייל - Mail", "Link - waverole"]
    if missing := [h for h in required if h not in hdr]:
        # A renamed header would otherwise silently match nothing, forever.
        raise RuntimeError(f"receipts sheet is missing expected headers: {missing}")
    idx = lambda name: hdr.index(name) if name in hdr else None
    i_date, i_gb = idx("תאריך - Date"), idx("איחסון - GB")
    i_order, i_act = idx("מס׳ הזמנה"), idx("Activation Code")
    i_iccid, i_link = idx("מס סידורי -ICCID"), idx("Link - esim.dog")
    i_mail, i_wave = idx("מייל - Mail"), idx("Link - waverole")

    email_time = delivery["received_at"].astimezone(TZ)
    window = timedelta(hours=MATCH_WINDOW_HOURS)
    cands = []
    for n, r in enumerate(rows[1:], start=2):
        get = lambda i: r[i].strip() if i is not None and len(r) > i else ""
        if not get(i_order) or get(i_act) or get(i_iccid):
            continue                                    # not pending
        gb = re.sub(r"[^\d.]", "", get(i_gb))
        if delivery["gb"] is not None and gb and float(gb) != delivery["gb"]:
            continue
        if not _link_ok(get(i_link), delivery["location"]):
            continue
        t = _row_time(get(i_date))
        if t and (t > email_time or email_time - t > window):
            continue
        cands.append({"row": n, "time": t, "order_id": get(i_order),
                      "customer_email": get(i_mail), "order_url": get(i_wave)})

    if not cands:
        return None
    cands.sort(key=lambda c: c["time"] or datetime.min.replace(tzinfo=TZ))
    if len(cands) > 1 and cands[0]["time"] and cands[1]["time"] and \
            abs((cands[1]["time"] - cands[0]["time"]).total_seconds()) < 180:
        raise AmbiguousMatch(
            f"rows {cands[0]['row']} and {cands[1]['row']} are both pending "
            f"{delivery['gb']}GB within 3 minutes"
        )
    return cands[0]


def already_completed(ws, success_url) -> bool:
    """True if some row already holds this delivery's supplier page AND its
    activation code — i.e. the purchase bot finished the order itself."""
    wanted = {success_url} if isinstance(success_url, str) else set(success_url or [])
    wanted.discard("")
    if not wanted:
        return False
    rows = ws.get_all_values()
    hdr = [h.strip() for h in rows[0]]
    if "Activation Code" not in hdr:
        return False
    i_act = hdr.index("Activation Code")
    # The supplier link lives in 'Link - esim.dog'; rows written before that
    # change kept it in 'QR', so check both.
    link_cols = [hdr.index(h) for h in ("Link - esim.dog", "QR") if h in hdr]
    for r in rows[1:]:
        act = r[i_act].strip() if len(r) > i_act else ""
        if not act:
            continue
        for i in link_cols:
            if (r[i].strip() if len(r) > i else "") in wanted:
                return True
    return False


def complete_row(ws, row_number: int, delivery: dict, details: dict):
    hdr = [h.strip() for h in ws.row_values(1)]
    gb = delivery.get("gb")
    updates = {
        "GB (0/X) - ניצול": f"{gb:g} / {gb:g}" if gb else "",
        # Each link in the column it is named after: the QR column gets the QR
        # image, and the supplier column gets the order's PACKAGE-DETAILS page.
        # It used to get the plan's shop page instead, which shows what we buy
        # rather than what this customer got.
        "QR": details.get("qr_code") or delivery["success_url"],
        "Link - esim.dog": delivery["success_url"],
        "Activation Code": details.get("activation_code", ""),
        "SM-DP+ Address": details.get("smdp", ""),
        # Leading apostrophe: USER_ENTERED turns a 19-digit ICCID into a float
        # (doubles hold ~15 digits) unless the column happens to be text-formatted.
        "מס סידורי -ICCID": f"'{details['iccid']}" if details.get("iccid") else "",
        "גישה - APN": details.get("apn", ""),
        "אזור - Region": delivery.get("location", ""),
        "חבילה - Plan": (
            f'{gb:g}GB - {delivery["days"]} days — {delivery.get("network", "")}'
            if gb and delivery.get("days") else delivery.get("network", "")
        ),
    }
    cells = [gspread.Cell(row_number, hdr.index(k) + 1, str(v))
             for k, v in updates.items() if k in hdr and v]
    if cells:
        ws.update_cells(cells, value_input_option="USER_ENTERED")
    log.info(f"row {row_number}: {len(cells)} cells written")


# ── site fulfillment ─────────────────────────────────────────────────────────

def awaiting_email_orders() -> list[dict]:
    """Orders the site has marked fulfilled whose buyer was never confirmed
    emailed. This is the safety net: the eSIM email is the customer's only
    permanent copy, so an order stays on this list until it provably went
    out — across runs, restarts and a missing address."""
    try:
        r = requests.get(ORDERS_URL, params={"status": "awaiting_email"}, timeout=20,
                         headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
        r.raise_for_status()
        return r.json().get("orders", [])
    except Exception as e:
        log.warning(f"could not read the delivery ledger: {_redact(str(e))}")
        return []


def report_email_sent(order_id: str, ok: bool, error: str = "", address: str = ""):
    """Close (or keep open) this order's ledger entry."""
    payload = {"order_id": order_id, "email_sent": bool(ok)}
    if error:
        payload["error"] = error[:300]
    if ok and address:
        payload["customer_email"] = address
    try:
        r = requests.post(ORDERS_URL, json=payload, timeout=20,
                          headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
        r.raise_for_status()
    except Exception as e:
        log.warning(f"order {order_id}: ledger update failed: {_redact(str(e))}")


def _delivery_from_record(o: dict) -> dict:
    """Rebuild the plan facts send_customer_email needs from a ledger entry."""
    e = o.get("esim") or {}
    gb = days = None
    if m := PLAN_RE.search(str(e.get("plan", ""))):
        gb, days = float(m.group(1)), int(m.group(2))
    elif m := re.search(r"(\d+(?:\.\d+)?)\s*GB\s*[-–]\s*(\d+)", str(e.get("plan", "")), re.I):
        gb, days = float(m.group(1)), int(m.group(2))
    return {"gb": gb, "days": days, "location": e.get("region", ""),
            "network": e.get("networks", "")}


def deliver_pending_emails(ws):
    """Send every eSIM email still owed to a buyer, then confirm it.

    Runs on EVERY invocation, not just when a new delivery email arrives —
    that is the whole point: a send that failed for any reason (bot down,
    SMTP refused, address missing at the time) is retried here until it
    succeeds. When the site has no address on file we look it up in the
    receipts sheet, so filling the Mail column by hand is a complete repair.
    """
    owed = awaiting_email_orders()
    if not owed:
        return
    log.info(f"{len(owed)} order(s) still owed their eSIM email")

    by_order = {}
    try:
        rows = ws.get_all_values()
        hdr = [h.strip() for h in rows[0]]
        i_ord = hdr.index("מס׳ הזמנה") if "מס׳ הזמנה" in hdr else None
        i_mail = hdr.index("מייל - Mail") if "מייל - Mail" in hdr else None
        if i_ord is not None and i_mail is not None:
            for r in rows[1:]:
                oid = r[i_ord].strip() if len(r) > i_ord else ""
                if oid:
                    by_order[oid] = r[i_mail].strip() if len(r) > i_mail else ""
    except Exception as e:
        log.warning(f"could not read addresses from the sheet: {e}")

    for o in owed:
        oid = str(o.get("order_id", ""))
        to = str(o.get("customer_email", "")).strip() or by_order.get(oid, "")
        if not to:
            # Nothing to send to. Keep the entry open and let the site escalate
            # to the owner — filling the sheet's Mail column repairs it.
            report_email_sent(oid, False, "no customer address on file")
            log.warning(f"order {oid}: still no customer address — email deferred")
            continue
        try:
            send_customer_email(to, oid, str(o.get("order_url", "")),
                                _delivery_from_record(o), esim=o.get("esim") or {},
                                lang=str(o.get("lang", "")), total=o.get("paid_usd"))
            report_email_sent(oid, True, address=to)
            log.info(f"order {oid}: eSIM email delivered (ledger closed)")
        except Exception as e:
            report_email_sent(oid, False, str(e))
            log.warning(f"order {oid}: eSIM email failed, will retry: {_redact(str(e))}")


def report_fulfilled(order_id: str, delivery: dict, details: dict):
    payload = {
        "order_id": order_id,
        "status": "fulfilled",
        "esim": {
            "activation_code": details.get("activation_code", ""),
            "qr_code": details.get("qr_code", ""),
            "smdp": details.get("smdp", ""),
            "iccid": details.get("iccid", ""),
            "apn": details.get("apn", ""),
            "region": delivery.get("location", ""),
            "plan": (f'{delivery["gb"]:g}GB - {delivery["days"]}d'
                     if delivery.get("gb") and delivery.get("days") else ""),
            "networks": delivery.get("network", ""),
        },
    }
    r = requests.post(ORDERS_URL, json=payload, timeout=20,
                      headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
    r.raise_for_status()
    log.info(f"order {order_id}: site fulfilled")


# ── customer "ready" email (from waverolesupply@gmail.com) ───────────────────
SUPPORT_EMAIL = "waverolesupport@gmail.com"
NAVY, BEIGE, BROWN, ACCENT = "#1B365D", "#f7ede2", "#7a5c40", "#C27A4E"


def send_customer_email(to: str, order_id: str, order_url: str, delivery: dict,
                        esim: dict | None = None, lang: str = "", total=None):
    """The buyer's 'your eSIM is ready' email. Sent from the real Waverole
    Gmail (waverolesupply) — the site's Resend sender (onboarding@resend.dev)
    looked untrustworthy, so this bot owns the customer email now.

    The email carries the FULL activation details (QR inline + manual codes),
    not just a link: the site's order records expire after 90 days, and this
    email must stay a working copy of the eSIM forever (new phone, late trip)."""
    # Only the address is mandatory. A missing order link costs the customer
    # the QR button, NOT the eSIM — the activation codes below install it on
    # any phone, so we still send rather than withhold their only copy.
    if not to:
        raise ValueError("missing customer email address")
    esim = esim or {}

    payload = order_payload(order_url)
    # Explicit values (from the site's own order record) win over whatever we
    # can decode from the link — the record is authoritative and always there.
    if total is None:
        total = payload.get("t")                 # what the customer actually paid
    heb = (lang or payload.get("l", "")) == "he" # site language at purchase time
    gb, days = delivery.get("gb"), delivery.get("days")
    L = {
        "subject": (f"ה-eSIM שלך מוכן לשימוש! \N{AIRPLANE} הזמנה {order_id}" if heb
                    else f"Your eSIM is ready to use! \N{AIRPLANE} Order {order_id}"),
        "title": "תודה על הרכישה! &#127881;" if heb else "Thank you for your purchase! &#127881;",
        "ready": "ה-eSIM שלך מוכן לשימוש!" if heb else "Your eSIM is ready to use!",
        "order": "הזמנה" if heb else "Order",
        "activate_hint": ("להפעלת ה-eSIM, פתחו את עמוד ההזמנה שלכם:" if heb
                          else "To activate your eSIM, open your order page:"),
        "cta": "להפעלת ה-eSIM שלי" if heb else "Activate my eSIM &#8594;",
        "guide": ("צריכים עזרה בהתקנה? מדריך מפורט שלב-אחר-שלב מחכה בעמוד ההזמנה." if heb
                  else "Need help installing? A step-by-step guide is on your order page."),
        "problem": "בעיה עם החבילה? נשמח לעזור:" if heb else "Any problem with your package? We're happy to help:",
        "fallback": ("הכפתור לא עובד? העתיקו את הקישור הזה לדפדפן:" if heb
                     else "Button not working? Copy this link into your browser:"),
        "dir": "rtl" if heb else "ltr",
    }
    rows = [("מספר הזמנה" if heb else "Order number", order_id),
            ("יעד" if heb else "Destination", delivery.get("location", "")),
            ("נפח גלישה" if heb else "Data", f"{gb:g} GB" if gb else ""),
            ("תוקף" if heb else "Validity",
             (f"{days} ימים" if heb else f"{days} days") if days else ""),
            ("רשת" if heb else "Network", delivery.get("network", "")),
            ("סה״כ שולם" if heb else "Total paid",
             f"${total:.2f}" if isinstance(total, (int, float)) else "")]
    detail_rows = "".join(
        f'<tr><td style="padding:7px 14px;color:{BROWN};font-size:13px">{k}</td>'
        f'<td style="padding:7px 14px;color:{NAVY};font-size:13px;font-weight:700;'
        f'text-align:right">{v}</td></tr>'
        for k, v in rows if v)

    cta = (f"""<p style="text-align:center;color:{BROWN};font-size:14px;margin:0 0 12px">{L['activate_hint']}</p>
  <div style="text-align:center;margin:0 0 22px">
    <a href="{order_url}" style="display:inline-block;background:{NAVY};color:#fff;font-weight:800;font-size:15px;text-decoration:none;padding:14px 34px;border-radius:12px">{L['cta']}</a>
  </div>""" if order_url else "")
    footer = (f"""<hr style="border:none;border-top:1px solid #e5d5c0;margin:0 0 12px">
  <p style="font-size:11px;color:#9a7a60;text-align:center;word-break:break-all;margin:0">{L['fallback']}<br><span dir="ltr">{order_url}</span></p>"""
              if order_url else "")

    html = f"""<div dir="{L['dir']}" style="font-family:'Nunito',Arial,sans-serif;max-width:520px;margin:0 auto;padding:32px;background:{BEIGE};border-radius:16px">
  <h1 style="font-size:22px;color:{NAVY};text-align:center;margin:0 0 6px">{L['title']}</h1>
  <p style="text-align:center;color:{NAVY};font-size:16px;font-weight:700;margin:0 0 4px">{L['ready']}</p>
  <p style="text-align:center;color:{BROWN};font-size:13px;margin:0 0 20px">{L['order']} <strong style="color:{NAVY}">{order_id}</strong></p>
  <table style="width:100%;background:#fff;border-radius:12px;border-collapse:collapse;margin:0 0 20px">{detail_rows}</table>
  {cta}
  {_esim_copy_html(esim, heb)}
  <p style="text-align:center;color:{BROWN};font-size:13px;margin:0 0 6px">{L['guide']}</p>
  <p style="text-align:center;color:{BROWN};font-size:13px;margin:0 0 18px">{L['problem']}
    <a href="mailto:{SUPPORT_EMAIL}" style="color:{ACCENT};font-weight:700;text-decoration:none">{SUPPORT_EMAIL}</a></p>
  {footer}
</div>"""

    # multipart/related so the QR renders inline (data: URIs are stripped by
    # Gmail — a real attachment referenced by cid: is the only reliable way).
    msg = MIMEMultipart("related")
    msg.attach(MIMEText(html, "html", "utf-8"))
    if qr := _qr_bytes(esim):
        img = MIMEImage(qr, _subtype="png")
        img.add_header("Content-ID", "<qr>")
        img.add_header("Content-Disposition", "inline", filename=f"esim-qr-{order_id}.png")
        msg.attach(img)
    msg["From"] = f"Waverole <{GMAIL_USER}>"
    msg["To"] = to
    msg["Subject"] = L["subject"]
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(GMAIL_USER, env("GMAIL_APP_PASSWORD").replace(" ", ""))
        s.send_message(msg)
    log.info(f"order {order_id}: customer email sent to {to}")


def _qr_bytes(esim: dict) -> bytes | None:
    """The QR as raw PNG bytes, so it can be attached inline (mail clients
    strip data: URIs, and a hotlinked image is blocked until the reader
    clicks 'show images' — this email must work on first open)."""
    qc = str((esim or {}).get("qr_code", ""))
    if qc.startswith("data:image/png;base64,"):
        try:
            return base64.b64decode(qc.split(",", 1)[1])
        except Exception:
            return None
    if qc.startswith("https://"):
        try:
            r = requests.get(qc, timeout=20)
            r.raise_for_status()
            return r.content
        except Exception as e:
            log.warning(f"could not download the QR image: {e}")
    return None


def _esim_copy_html(esim: dict, heb: bool = False) -> str:
    """Permanent in-email copy of the eSIM: inline QR + the manual codes.
    Shown under the CTA; empty string when there is nothing to show."""
    if not esim:
        return ""
    t_keep = ("ה-eSIM שלכם — שמרו את המייל הזה כעותק קבוע" if heb
              else "Your eSIM — keep this email as your permanent copy")
    t_scan = ("סרקו את הקוד ממכשיר אחר, או הוסיפו את ה-eSIM ידנית עם הקודים למעלה." if heb
              else "Scan the QR from another device, or add the eSIM manually with the codes above.")
    rows = [("Activation Code", esim.get("activation_code", "")),
            ("SM-DP+ Address", esim.get("smdp", "")),
            ("ICCID", esim.get("iccid", "")),
            ("APN", esim.get("apn", ""))]
    code_rows = "".join(
        f'<tr><td style="padding:6px 12px;color:{BROWN};font-size:12px;white-space:nowrap">{k}</td>'
        f'<td style="padding:6px 12px;color:{NAVY};font-size:12px;font-family:ui-monospace,Menlo,monospace;'
        f'word-break:break-all;text-align:right">{v}</td></tr>'
        for k, v in rows if v)
    if not code_rows and not _qr_bytes(esim):
        return ""
    qr_img = ('<div style="text-align:center;margin:0 0 10px">'
              '<img src="cid:qr" alt="eSIM QR code" width="180" height="180" '
              'style="border-radius:12px;background:#fff;padding:8px"></div>'
              if _qr_bytes(esim) else "")
    return f"""<div style="background:#fff;border-radius:12px;padding:16px 10px;margin:0 0 22px">
  <p style="text-align:center;color:{NAVY};font-size:13px;font-weight:800;margin:0 0 10px">{t_keep}</p>
  {qr_img}
  <table style="width:100%;border-collapse:collapse" dir="ltr">{code_rows}</table>
  <p style="text-align:center;color:#9a7a60;font-size:11px;margin:8px 0 0">{t_scan}</p>
</div>"""


# ── main ─────────────────────────────────────────────────────────────────────

def process(inbox: Inbox, ws, d: dict):
    uid = d["uid"]
    log.info(f"email uid={uid}: {d['gb']}GB/{d['days']}d {d['location']}")

    try:
        match = find_pending_row(ws, d)
    except AmbiguousMatch as e:
        alert("Ambiguous match — manual action needed",
              f"{e}\nEmail: {d['gb']}GB {d['location']} {d['success_url']}\n"
              "No row was touched.")
        inbox.flag(uid)
        return
    except RuntimeError as e:                    # e.g. renamed sheet headers
        alert("Receipts sheet problem — bot cannot match orders", str(e))
        return                                   # NOT flagged → retried

    if match is None:
        # Already done? The purchase bot now reads the eSIM straight off the
        # success page, so by the time this email lands its row is usually
        # complete — and a complete row is not "pending", so nothing matches.
        # That is success, not a problem: flag it and stay quiet.
        if already_completed(ws, d.get("success_urls") or [d["success_url"]]):
            log.info(f"email uid={uid}: order already completed by the purchase bot")
            inbox.flag(uid)
            return
        # Otherwise the email can simply have beaten the purchase bot's row.
        # Flagging immediately would burn the email forever; give the row time
        # to appear before giving up.
        age = datetime.now(timezone.utc) - d["received_at"]
        if age < timedelta(hours=UNMATCHED_GRACE_HOURS):
            log.info(f"email uid={uid}: no matching row yet "
                     f"({age.total_seconds() / 60:.0f} min old) — will retry")
            return                               # NOT flagged → retried
        alert("Delivery email without a matching order",
              f"{d['gb']}GB / {d['days']}d / {d['location']}\n{d['success_url']}\n"
              f"No pending receipts row matched within {UNMATCHED_GRACE_HOURS}h — "
              "if this was a manual purchase, ignore this message.")
        inbox.flag(uid)
        return

    order_id = match["order_id"]
    details = fetch_esim_details(d.get("success_urls") or [d["success_url"]])
    # Record the reading the supplier actually accepted, so the receipts row
    # and the site both get a link that opens.
    if details.get("used_url"):
        d["success_url"] = details["used_url"]
    if not (details.get("activation_code") or details.get("iccid")):
        log.warning(f"order {order_id}: no details on success page yet — will retry")
        return                                          # NOT flagged → retried

    # Backfill plan facts the email didn't parse from the success page itself.
    if d.get("gb") is None and details.get("plan_gb"):
        d["gb"], d["days"] = details["plan_gb"], details.get("plan_days")
    if not d.get("location") and details.get("page_region"):
        d["location"] = details["page_region"]

    complete_row(ws, match["row"], d, details)
    try:
        report_fulfilled(order_id, d, details)
    except Exception as e:
        # No customer email: it sends the buyer to their order page, which has
        # no eSIM on it until the site accepts this POST. Flagged anyway — the
        # row is no longer "pending" now that it is filled in, so a retry could
        # not re-match it and would just alert about an orphan email forever.
        alert(f"Order {order_id}: sheet done, site report FAILED — ACTION NEEDED",
              f"{e}\n\nThe customer email was NOT sent (their order page has no "
              f"eSIM yet). Fix the cause, then re-post the fulfillment; row "
              f"{match['row']} of the receipts sheet has every detail.")
        inbox.flag(uid)
        return
    # The buyer's email is now tracked in the site's delivery ledger (opened by
    # report_fulfilled above). Send it right away for speed, but a failure here
    # is no longer the end of the road: the entry stays open, deliver_pending_
    # emails retries it every run, and the site escalates on its own. No alert
    # from here — that would fire on every transient SMTP hiccup.
    to = match.get("customer_email", "")
    try:
        send_customer_email(to, order_id, match.get("order_url", ""), d, esim=details)
        report_email_sent(order_id, True, address=to)
    except Exception as e:
        report_email_sent(order_id, False, str(e))
        log.warning(f"order {order_id}: eSIM email failed, ledger keeps it "
                    f"for retry: {_redact(str(e))}")
    inbox.flag(uid)
    log.info(f"order {order_id} COMPLETED (row {match['row']})")


def main():
    inbox = Inbox()
    ws = None
    try:
        deliveries = inbox.unprocessed()
        log.info(f"{len(deliveries)} unprocessed delivery email(s)")
        if deliveries:
            ws = sheet_client().open_by_key(RECEIPTS_SHEET_ID).sheet1
        for d in deliveries:
            try:
                process(inbox, ws, d)
            except Exception as e:
                # No traceback: this repo's Actions logs are PUBLIC, and e.g. a
                # Playwright error embeds the success-page URL. One redacted line.
                log.error(f"email uid={d['uid']} failed — left for retry: "
                          f"{_redact(f'{type(e).__name__}: {e}')}")
    finally:
        inbox.close()

    # ALWAYS, even with an empty inbox: this is the retry loop that guarantees
    # every buyer eventually receives their eSIM. It runs last so an email that
    # just failed above gets a second chance in this same run.
    try:
        if ws is None:
            ws = sheet_client().open_by_key(RECEIPTS_SHEET_ID).sheet1
        deliver_pending_emails(ws)
    except Exception as e:
        log.error(f"delivery ledger sweep failed: {_redact(str(e))}")


if __name__ == "__main__":
    main()
