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
import time
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
# Gmail accepts the TCP connection and then simply stops answering when it
# throttles a client — and this bot connects every 5 minutes, ~288 times a
# day. With no timeout imaplib blocks on that read FOREVER: the run burned its
# full 10-minute job budget, held the concurrency slot the whole time, and the
# dispatch waiting behind it was cancelled — which is why one stuck socket
# turned into a stream of "all jobs have failed" emails. Fail in a minute
# instead; the next run five minutes later picks the work up untouched.
IMAP_TIMEOUT = 60
HEADER_BATCH = 200         # message headers per IMAP round-trip (see _headers)
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
        # The timeout rides on the socket, so login/list/search/fetch below are
        # all covered by it — not just the connect.
        self.box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=IMAP_TIMEOUT)
        self.box.login(GMAIL_USER, env("GMAIL_APP_PASSWORD").replace(" ", ""))
        folder = _all_mail_folder(self.box)
        log.info(f"searching folder: {folder}")
        # imaplib needs the mailbox name quoted when it contains spaces.
        self.box.select(f'"{folder}"' if " " in folder else folder)

    def _headers(self, uids: list[str]) -> dict:
        """SUBJECT+FROM for many messages, one round-trip per HEADER_BATCH.

        This was one FETCH per message, and that is what made a job with a few
        seconds of work in it routinely take half a minute. The search below
        cannot narrow much: the bot flags only the delivery emails it has
        processed, so "unflagged in the last week" is essentially the ENTIRE
        mailbox — sent customer mail, alerts, everything — and each one cost a
        full network round-trip, every five minutes, all day. The tail of that
        is what eventually crosses IMAP_TIMEOUT and fails a run.

        The reply is keyed by SEQUENCE number and the server may return the
        messages in any order, so UID is asked for as a field and read back out
        of each response. Matching these by position looks like it works right
        up until the day it silently pairs one email's headers with another's.
        """
        out = {}
        for i in range(0, len(uids), HEADER_BATCH):
            chunk = uids[i:i + HEADER_BATCH]
            typ, data = self.box.uid(
                "fetch", ",".join(chunk),
                "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
            if typ != "OK":
                # Nothing is flagged until it is fully processed, so the next
                # run sees these same messages untouched. Skip, don't fail.
                log.warning(f"header fetch failed for {len(chunk)} message(s) "
                            f"— left for the next run")
                continue
            for part in data or []:
                if not isinstance(part, tuple) or len(part) < 2 or part[1] is None:
                    continue                    # the b')' separators between messages
                if m := re.search(rb"UID (\d+)", part[0] or b""):
                    out[m.group(1).decode()] = email.message_from_bytes(part[1])
        return out

    def unprocessed(self) -> list[dict]:
        # No FROM filter in the IMAP query: forwarded copies (Fwd:) come from
        # the owner's address, not esim.dog. Validation happens in Python —
        # the subject must match AND the body must carry an esim.dog success
        # link (parse_delivery returns None without one), which is a stronger
        # signal than the envelope sender anyway.
        since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        typ, data = self.box.uid("search", None, f"(UNFLAGGED SINCE {since})")
        uids = [b.decode() for b in (data[0] or b"").split()]
        # Logged BEFORE the fetch, so a run that dies in it still says how much
        # it was carrying. This number is the job's real workload, and the day
        # it starts climbing is the day the runs get slow again.
        log.info(f"{len(uids)} unflagged message(s) since {since}")
        heads = self._headers(uids)
        out = []
        for uid in uids:
            head = heads.get(uid)
            if head is None:
                continue
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
SUPPLIER_USAGE_URL = "https://esim.dog/.netlify/functions/check-esim-usage"
SESSION_ID_RE = re.compile(r"(?:session_id|payment_intent)=([A-Za-z0-9_\-]+)")
GB = 1024 ** 3


# esim.dog does not run one eSIM network — it resells several, and the usage
# endpoint answers for ONE of them per request, chosen by `providerCode`. Left
# unset it answers for the default provider only, and an eSIM belonging to any
# other simply comes back absent, exactly like an id it has never heard of.
#
# That silence is why the usage bot appeared to do nothing: measured
# 2026-07-29 across the six eSIMs actually sold, the default provider answered
# for ONE and provider 4 answered for the other FIVE. Every Israeli Hot Mobile
# package — i.e. nearly everything being sold — was invisible, so the meter on
# those customers' pages stayed at whatever it was seeded with and the 90%
# emails could never fire.
#
# Ordered by how much of the catalogue each one covers, so the common case is
# settled in the first request. Provider 3 is deliberately included even
# though it answers with a zero-filled placeholder for ids it does not own:
# `total <= 0` throws those away below, which is also what protects us from
# recording "0 of 0 GB" as if it were a real reading.
USAGE_PROVIDER_CODES = (None, 4, 3, 2, 1)

# The supplier's hard cap, in its own words:
#     {"error":"Maximum 10 eSIMs can be queried at once."}
# Ask for eleven and it rejects the WHOLE request with a 400 — not the excess,
# everything — so one order too many silently froze every customer's meter.
# This is a growth cliff, not a glitch: it fired the day the eleventh live
# package was sold and would have stayed broken forever after. Never send the
# whole fleet in one call, however small the fleet looks today.
USAGE_BATCH = 10


def _usage_request(iccids: list[str], provider) -> list:
    """Usage for these eSIMs from one provider, in cap-sized calls. [] on error.

    A failed chunk costs only its own rows: the others still return, and any
    ICCID left unanswered simply moves on to the next provider in the sweep.
    """
    rows: list = []
    for start in range(0, len(iccids), USAGE_BATCH):
        chunk = iccids[start:start + USAGE_BATCH]
        payload = {"iccidList": chunk}
        if provider is not None:
            payload["providerCode"] = provider
        try:
            r = requests.post(SUPPLIER_USAGE_URL, json=payload,
                              headers={"Accept": "application/json",
                                       "User-Agent": "Mozilla/5.0"}, timeout=60)
            r.raise_for_status()
            rows += (r.json() or {}).get("usage") or []
        except Exception as e:
            log.warning(f"usage lookup failed for {len(chunk)} eSIM(s) "
                        f"(provider {provider}): {_redact(str(e))}")
    return rows


def fetch_usage(iccids: list[str]) -> dict:
    """How much data each of these eSIMs has used, keyed by ICCID.

    The supplier takes a LIST, but only ten at a time (USAGE_BATCH), so a
    fleet costs one request per ten rather than one per eSIM — still cheap
    enough to sweep every active customer forever. Each provider needs its own
    pass (see USAGE_PROVIDER_CODES), but only over the eSIMs nobody has
    accounted for yet, so the later providers are asked about a shrinking
    remainder — usually nothing at all, which ends the sweep early.

    An unknown ICCID is simply absent from the reply (no error), so always
    read the result by key and never by position.
    """
    iccids = [re.sub(r"\D", "", str(i or "")) for i in iccids]
    iccids = [i for i in iccids if i]
    if not iccids:
        return {}

    out = {}
    for provider in USAGE_PROVIDER_CODES:
        missing = [i for i in iccids if i not in out]
        if not missing:
            break
        found_here = 0
        for u in _usage_request(missing, provider):
            iccid = re.sub(r"\D", "", str(u.get("iccid", "")))
            total = float(u.get("totalData") or 0)
            used = float(u.get("dataUsage") or 0)
            # total <= 0 is a placeholder for an eSIM this provider does not
            # own, not a real package — never record it as a reading.
            if not iccid or total <= 0 or iccid in out:
                continue
            found_here += 1
            out[iccid] = {
                "used_gb": round(used / GB, 3),
                "total_gb": round(total / GB, 3),
                "pct": max(0, min(100, round(used / total * 100))),
                "expires": str(u.get("expiryDate") or ""),
                "remaining_days": u.get("remainingDays"),
                # The supplier's own verdict ("used_expired", …). Used only as
                # a corroborating hint — our stop rule is the one below.
                "status": str(u.get("status") or ""),
                "provider": provider,
            }
        if found_here:
            log.info(f"provider {provider if provider is not None else 'default'}: "
                     f"{found_here} of {len(missing)} eSIM(s) answered")
    return out


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


def _sup(v) -> str:
    """A supplier field as text, with every spelling of "nothing" removed.

    `.get(key, "")` does NOT protect against this. The supplier sends a real
    JSON `null` for a field it has no value for — the key is present, so the
    default never applies, and `str()` turns it into the literal "None".

    That string is truthy, so it survived the "skip empty rows" filter and
    travelled all the way into the buyer's eSIM email as

        APN     None

    which reads like an instruction to type it in. A plan with no APN needs
    the field left BLANK; typing anything there stops the data working. Empty
    means empty, in the sheet, on the order page and in the email.
    """
    s = "" if v is None else str(v).strip()
    return "" if s.lower() in ("none", "null", "undefined", "nan", "n/a") else s


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
        "smdp": _sup(e.get("smdp_address")) or (code.split("$")[1] if "$" in code else ""),
        "iccid": _sup(e.get("iccid")),
        "apn": _sup(e.get("apn")),
        "qr_code": _sup(e.get("qr_code")),
        "page_region": _sup(s.get("country_name")),
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
    # A per-order success link names no country at all — it is a session id.
    # This check exists to reject a row bought for a DIFFERENT destination, and
    # a link that states no destination cannot do that: answering False here
    # vetoed every row in the sheet the moment the column started holding the
    # order's own page instead of the plan's shop page, so no delivery email
    # could be matched to anything and every order needed a human.
    if SESSION_ID_RE.search(link):
        return True
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

    # ── the certain answer first: the same purchase session ──
    # The purchase bot writes the order's supplier page into the row, and the
    # delivery email is about that very page — so when both are present this
    # is an identity, not a guess. Everything below is the fallback for rows
    # that predate it (matching on size, destination and a time window, which
    # can only ever be circumstantial and needs a human when two orders look
    # alike).
    wanted = {m.group(1) for u in (delivery.get("success_urls")
                                   or [delivery.get("success_url", "")])
              if (m := SESSION_ID_RE.search(str(u or "")))}
    if wanted:
        for n, r in enumerate(rows[1:], start=2):
            get = lambda i: r[i].strip() if i is not None and len(r) > i else ""
            if not get(i_order) or get(i_act) or get(i_iccid):
                continue                                # not pending
            for col in (i_link, idx("QR")):             # older rows kept it in QR
                m = SESSION_ID_RE.search(get(col))
                if m and m.group(1) in wanted:
                    log.info(f"row {n} matched by purchase session (exact)")
                    return {"row": n, "time": _row_time(get(i_date)),
                            "order_id": get(i_order), "customer_email": get(i_mail),
                            "order_url": get(i_wave)}

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
        # Consumption, as used / total — so a brand-new package is 0 of X.
        # This used to write "X / X", which showed every eSIM as fully used
        # from the moment it was delivered. The usage bot keeps it current.
        "GB (0/X) - ניצול": f"0 / {gb:g}" if gb else "",
        # Start the daily usage checks for this package.
        "סטטוס - Status": "פעיל",
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


STALE_PENDING_MIN = 15


def check_stale_pending():
    """Alert while a PAID order sits 'pending' with nothing buying it.

    The purchase bot lives on a home PC; every failure mode of that machine
    (off, crashed, stuck ledger claim) looks from here like an order aging
    quietly in the queue — which is exactly how WR-ZWLM87 waited 9 hours on
    2026-08-19 with every screen green. This runs in the cloud every 5
    minutes, so the owner hears about it no matter what died: first alert
    once the order is STALE_PENDING_MIN old, then a reminder every ~2 hours.
    The age bands keep that stateless — with a 5-minute cadence a 10-minute
    band fires once or twice per band, never forever.

    probe=1: reading the queue here must not refresh the purchase bot's
    heartbeat, or this very check would hide the outage it looks for.
    """
    r = requests.get(ORDERS_URL, params={"status": "pending", "probe": "1"},
                     timeout=20,
                     headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
    r.raise_for_status()
    now = datetime.now(timezone.utc)
    for o in r.json().get("orders", []):
        ts = str(o.get("ts", ""))
        try:
            age_min = (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))
                       ).total_seconds() / 60
        except ValueError:
            continue
        fresh_alert = STALE_PENDING_MIN <= age_min < STALE_PENDING_MIN + 10
        reminder = age_min >= 120 and (age_min % 120) < 10
        if fresh_alert or reminder:
            oid = o.get("order_id", "?")
            log.error(f"order {oid} has been PAID and pending for "
                      f"{age_min:.0f} min - nothing is buying it")
            alert(
                f"Order {oid} paid {age_min:.0f} min ago — nothing is buying it",
                f"SKU: {o.get('sku', '?')}\n"
                f"Paid at: {ts}\n"
                f"Amount: ${o.get('paid_usd', '?')}\n\n"
                "The order is still 'pending' in the site queue. Either the "
                "purchase bot on the PC is down, or its ledger holds a stuck "
                "claim for this order.\n\n"
                "CHECK, IN ORDER:\n"
                "1. Bot panel on the PC - the BOT row must say RUNNING "
                "(key 1 starts it; the watchdog restarts it within 5 min).\n"
                "2. A red STUCK row in the panel -> in a command window run:\n"
                "     venv\\Scripts\\python.exe -m bot.main --stuck\n"
                "   and follow what it prints.\n"
                "3. Bot running, nothing stuck, order still pending -> read "
                "bot.log for why the order is being refused.",
            )


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
    # _sup again, deliberately. This block is also rendered from records the
    # SITE stored — including ones written before the parser stopped turning a
    # null into "None" — so the guard has to sit where the buyer's email is
    # built, not only where the supplier is read.
    rows = [("Activation Code", _sup(esim.get("activation_code"))),
            ("SM-DP+ Address", _sup(esim.get("smdp"))),
            ("ICCID", _sup(esim.get("iccid"))),
            ("APN", _sup(esim.get("apn")))]
    code_rows = "".join(
        f'<tr><td style="padding:6px 12px;color:{BROWN};font-size:12px;white-space:nowrap">{k}</td>'
        f'<td style="padding:6px 12px;color:{NAVY};font-size:12px;font-family:ui-monospace,Menlo,monospace;'
        f'word-break:break-all;text-align:right">{v}</td></tr>'
        for k, v in rows if v)
    if not code_rows and not _qr_bytes(esim):
        return ""
    qr_img = ('<div style="text-align:center;margin:0 0 10px">'
              # 200 = the supplier QR's own resolution. Mail clients do not
              # resample kindly, and a scaled QR is a QR that fails to scan.
              '<img src="cid:qr" alt="eSIM QR code" width="200" height="200" '
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


def _open_inbox(attempts: int = 3):
    """Connect to Gmail, riding out a brief refusal.

    Nothing is lost when this ultimately fails — the bot keeps no state of its
    own, so the next run five minutes from now sees exactly the same mail. The
    retry is only here so a two-second blip does not become an alert in the
    owner's inbox. A real outage still fails loudly, on purpose.
    """
    for i in range(1, attempts + 1):
        try:
            return Inbox()
        except Exception as e:
            if i == attempts:
                raise
            log.warning(f"IMAP connect failed ({type(e).__name__}: {e}) — "
                        f"retry {i}/{attempts - 1} in {i * 5}s")
            time.sleep(i * 5)


def _needs_a_human(e: Exception) -> bool:
    """Is this mailbox failure one that will still be here in five minutes?

    A refused connection, a slow socket, a Gmail hiccup: the bot keeps no state
    of its own, so the next run reads exactly the same mail and loses nothing.
    Failing the job for one of those sends an "all jobs have failed" email that
    says nothing, links a log the owner cannot open, and — repeated often
    enough — teaches them to ignore the next one, which will be the real one.

    A rejected password or a missing secret is the opposite: it never fixes
    itself, and every run that stays quiet about it is orders piling up unseen.
    """
    if isinstance(e, RuntimeError):        # missing secret, renamed sheet headers
        return True
    text = f"{type(e).__name__}: {e}".lower()
    return any(w in text for w in ("authenticationfailed", "invalid credentials",
                                   "authentication failed", "application-specific"))


def main():
    ws = None
    mail_error = None
    try:
        inbox = _open_inbox()
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
    except Exception as e:
        # Caught, not raised: the sweep below is the promise that every buyer
        # gets their eSIM, and it used to be skipped entirely whenever reading
        # the mailbox failed — so a Gmail blip silently cancelled the retry for
        # customers whose email had nothing to do with the mailbox.
        mail_error = e
        log.error(f"mailbox unreadable this cycle — nothing lost, the next run "
                  f"re-reads it: {_redact(f'{type(e).__name__}: {e}')}")

    # ALWAYS, even with an empty inbox: this is the retry loop that guarantees
    # every buyer eventually receives their eSIM. It runs last so an email that
    # just failed above gets a second chance in this same run.
    try:
        if ws is None:
            ws = sheet_client().open_by_key(RECEIPTS_SHEET_ID).sheet1
        deliver_pending_emails(ws)
    except Exception as e:
        log.error(f"delivery ledger sweep failed: {_redact(str(e))}")

    # The purchase-side net: a paid order still 'pending' means NOTHING is
    # buying it (PC off, bot dead, stuck ledger claim). Independent of the
    # mailbox and the sheet on purpose - it must fire when everything else
    # is down.
    try:
        check_stale_pending()
    except Exception as e:
        log.error(f"stale-pending check failed: {_redact(str(e))}")

    if mail_error is not None and _needs_a_human(mail_error):
        alert("Cannot read the delivery mailbox — ACTION NEEDED",
              f"{_redact(f'{type(mail_error).__name__}: {mail_error}')}\n\n"
              f"This is not the kind of failure that clears on its own, so no "
              f"delivery email is being processed until it is fixed. Paid "
              f"orders are NOT lost — they stay queued and are picked up as "
              f"soon as the bot can read {GMAIL_USER} again.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
