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
ALERT_PREFIX = "[fulfillment bot] "
# How hard we try to tell the site an eSIM email went out. An unconfirmed send
# leaves the ledger entry open, and an open entry is mailed AGAIN next sweep —
# so this POST failing once is what sends a buyer a second copy of their eSIM.
LEDGER_CONFIRM_ATTEMPTS = 3
# Order ids and addresses go into an IMAP SEARCH command verbatim. Anything
# that could close the quoting is not searched for at all.
SAFE_IMAP_ATOM = re.compile(r"^[A-Za-z0-9._%+@-]+$")

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
    ({id, sku, ts, t}) — `t` is the total the customer actually paid, and
    `fr: 1` (from 2026-10-20) marks a package the owner gave away."""
    try:
        b64 = order_url.split("order=", 1)[1].split("&", 1)[0]
        b64 += "=" * (-len(b64) % 4)
        return json.loads(base64.urlsafe_b64decode(b64))
    except Exception:
        return {}


def is_free(order: dict) -> bool:
    """A package the OWNER gave away (2026-10-20) — the same three facts the
    PC bot demands (bot/fallback.is_free), so the two machines can never
    disagree about whether an order was a gift:

      * the site stored `free: true` — only its bearer-authenticated create
        action can write it, never a till, so no buyer can mint one;
      * paid_usd is exactly zero — `free` arriving WITH a price is a
        contradiction about money, and that is read as the paid order;
      * the order is the owner's own ('manual', or no source at all — the
        awaiting_email rows do not carry one).

    A paid_usd of 0 WITHOUT the marker keeps meaning what it always meant:
    nothing was recorded. It is not a gift and is never shown as "Free"."""
    if order.get("free") is not True:
        return False
    try:
        paid = float(order.get("paid_usd"))
    except (TypeError, ValueError):
        return False
    if abs(paid) > 1e-9:
        return False
    return str(order.get("source") or "").strip().lower() in ("manual", "")


# ── alerts ───────────────────────────────────────────────────────────────────

def alert(subject: str, body: str):
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = GMAIL_USER
        msg["To"] = ALERTS_EMAIL
        msg["Subject"] = f"{ALERT_PREFIX}{subject}"
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
        self._mailed: dict[tuple[str, str], bool] = {}   # see already_mailed_to

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

    def already_mailed_to(self, order_id: str, to: str) -> bool:
        """True if this buyer's 'your eSIM is ready' email is already in Sent.

        The delivery ledger is meant to answer this, but it only knows what the
        confirming POST managed to tell it: when that POST is lost the entry
        stays open even though the buyer already has their eSIM, and the next
        sweep mails them a second copy. The mailbox has no such blind spot —
        what went out is in Sent whatever the site believes. The folder
        selected in __init__ is All Mail, so Sent is already in scope.
        """
        key = (order_id, to.lower())
        if key in self._mailed:
            return self._mailed[key]
        found = False
        if SAFE_IMAP_ATOM.match(order_id) and SAFE_IMAP_ATOM.match(to):
            try:
                typ, data = self.box.uid(
                    "search", None,
                    f'(FROM "{GMAIL_USER}" TO "{to}" HEADER SUBJECT "{order_id}")')
                uids = ([b.decode() for b in (data[0] or b"").split()]
                        if typ == "OK" else [])
                # An ALERT about this order is also from us and also names it in
                # the subject. It is addressed to the owner, so TO normally
                # separates the two — but when the owner is the buyer (internal
                # orders) it does not, and mistaking an alert for the customer's
                # copy would withhold the eSIM. The prefix always separates them.
                for head in self._headers(uids).values():
                    if not _decode(head.get("Subject")).startswith(ALERT_PREFIX):
                        found = True
                        break
            except Exception as e:
                # "Unknown", answered as "not yet". Withholding on a failed
                # lookup would break the one promise this bot makes — every
                # buyer gets their eSIM — to avoid a duplicate that only
                # happens when the ledger confirmation ALSO failed.
                log.warning(f"order {order_id}: cannot read Sent ({_redact(str(e))}) "
                            f"— treating as not yet mailed")
        self._mailed[key] = found
        return found

    def note_sent(self, order_id: str, to: str):
        """Remember a send made in THIS run. already_mailed_to caches its
        answer per (order, address), and the answer it cached before the send
        was "not yet" — a second sender in the same run (the delivery-mail
        matcher, then the ledger sweep a second later) would have read that
        stale "not yet" and written again."""
        self._mailed[(order_id, to.lower())] = True

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

# What the PC bot (bot/sheets.py, 2026-10-20) writes on a receipts row that
# another purchase SUPERSEDED: the placeholder an uncertain or abandoned
# checkout left behind, once the order was bought again (same number) or
# replaced (a new one). A row is never deleted — it is the audit trail, and
# the duplicate guard reads the order number off it — so it is marked:
#   Status REPLACED_STATUS ('replaced')         Buy 0 too: PROVEN unpaid;
#   Status REPLACED_CHECK_STATUS ('replaced -   Buy kept: nobody can rule
#          check the charge')                  out the charge.
# Spelled in \u escapes on purpose (a paste once broke on mojibake), and
# compared by PREFIX so both spellings — and any note the owner appends by
# hand — read the same.
REPLACED_STATUS = "\u05d4\u05d5\u05d7\u05dc\u05e4\u05d4"
REPLACED_CHECK_STATUS = REPLACED_STATUS + " - \u05dc\u05d1\u05d3\u05d5\u05e7 \u05d7\u05d9\u05d5\u05d1"
# ...and the same row's supplier link, neutralised: the session id stays
# readable for the owner checking the charge, but no `session_id=` reader
# matches it any more, so a late delivery mail cannot be written onto the dead
# row of an order the customer already has an eSIM for.
SUPERSEDED_SESSION = "superseded-session:"
SUPERSEDED_RE = re.compile(re.escape(SUPERSEDED_SESSION) + r"([A-Za-z0-9_\-]+)")
# The Purchase-column word for a package the owner gave away (owner decision
# D3). Not a paid rail: green means money banked, and a gift banked nothing.
FREE_RAIL = "free"
H_STATUS, H_BUY, H_SELL, H_SALE = ("\u05e1\u05d8\u05d8\u05d5\u05e1 - Status", "\u05e7\u05e0\u05d9\u05d9\u05d4 - Buy",
                                   "\u05de\u05db\u05d9\u05e8\u05d4 - Sell", "\u05d4\u05e0\u05d7\u05d4 - Sale")
H_PURCHASE, H_MAIL = "\u05e8\u05db\u05d9\u05e9\u05d4 - Purchase", "\u05de\u05d9\u05d9\u05dc - Mail"
H_ORDER, H_ROUTE = "\u05de\u05e1\u05f3 \u05d4\u05d6\u05de\u05e0\u05d4", "Route"
H_ACT, H_ICCID = "Activation Code", "\u05de\u05e1 \u05e1\u05d9\u05d3\u05d5\u05e8\u05d9 -ICCID"
H_LINK_SUP, H_QR = "Link - esim.dog", "QR"
# The Mail cell's colour is the owner's "money banked" mark (memory:
# receipts-sheet): green on the ONE row that carries a payment on a rail we can
# see, white everywhere else. One spelling for both cloud writers —
# stellar_buyer reads these from here — and the same values as the PC bot's
# bot/sheets.py, so a row painted by any of the three looks the same.
PAID_RAILS = {"paypal", "payme"}
PAID_BG = {"red": 217 / 255, "green": 234 / 255, "blue": 211 / 255}   # #d9ead3
WHITE_BG = {"red": 1.0, "green": 1.0, "blue": 1.0}


def is_replaced_status(status) -> bool:
    """A receipts Status that says this row was superseded by another one."""
    return str(status or "").strip().startswith(REPLACED_STATUS)


def _is_zero_money(text) -> bool:
    """'0$' / '0.00$' / '$0' -> True. A BLANK cell is not zero: blank is
    "nobody wrote a price", which is no reason to call a row dead."""
    digits = re.sub(r"[^\d.]", "", str(text or ""))
    try:
        return bool(digits) and float(digits) == 0.0
    except ValueError:
        return False


def neutralised_link(link: str) -> str:
    """`link` with its session id no longer readable as one (SUPERSEDED_SESSION)
    — the PC bot's bot/sheets.neutralised_link, spelled the same so either
    side's marking is read by the other. A link without a session is returned
    as it is."""
    s = str(link or "").strip()
    if not s or SUPERSEDED_SESSION in s:
        return s
    m = SESSION_ID_RE.search(s)
    return f"{SUPERSEDED_SESSION}{m.group(1)}" if m else s


def _dead_row(status, link, qr, buy, sell) -> bool:
    """A receipts row no delivery may ever be written onto: one the PC bot (or
    the owner) marked as superseded, or a placeholder zeroed on both sides.
    Without this, the receipts row an abandoned checkout left behind — order
    number, no eSIM, so 'pending' to the matcher — took the next delivery of
    the same size within twelve hours, and the customer's eSIM was written
    onto the dead order instead of the one that replaced it (WR-4YWATY /
    WR-T3QD3Q, receipts rows 188-189, 2026-10-11)."""
    if is_replaced_status(status):
        return True
    if SUPERSEDED_SESSION in str(link or "") or SUPERSEDED_SESSION in str(qr or ""):
        return True
    return _is_zero_money(buy) and _is_zero_money(sell)


def _session_ids(text) -> set[str]:
    return {m.group(1) for m in SESSION_ID_RE.finditer(str(text or ""))}


def _names_other_session(cells: list[str], wanted: set[str]) -> bool:
    """True when the row's own supplier link identifies a DIFFERENT purchase
    from the one this delivery mail is about.

    A row that names its own session is waiting for that session's mail, and
    the exact pass in find_pending_row already gave it every chance to be
    this one. Letting the size-and-time guess hand it a different package is
    how a late delivery of an abandoned checkout lands on the row of the
    purchase that replaced it (review 2026-10-20: a same-number re-buy, then
    the first, uncertain payment approved after all).

    Conservative on purpose — only a clear contradiction counts:
      * no session on the row (older rows hold the plan's shop page) -> no;
      * one id a prefix of the other (a mail-wrapped, truncated link) -> no;
      * different id KINDS (cs_… against pi_…, the two formats seen in real
        mail) -> cannot tell, so no."""
    if not wanted:
        return False
    have = set()
    for c in cells:
        have |= _session_ids(c)
    if not have:
        return False

    def kind(s):
        return s.split("_", 1)[0]
    for r in have:
        for w in wanted:
            if kind(r) != kind(w) or r.startswith(w) or w.startswith(r):
                return False
    return True


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
    i_qr = idx("QR")
    # Optional: a sheet without them simply cannot mark a row dead, and every
    # row stays a candidate exactly as before 2026-10-20.
    i_status, i_buy, i_sell = idx(H_STATUS), idx(H_BUY), idx(H_SELL)

    email_time = delivery["received_at"].astimezone(TZ)
    window = timedelta(hours=MATCH_WINDOW_HOURS)

    def dead(get) -> bool:
        return _dead_row(get(i_status), get(i_link), get(i_qr), get(i_buy), get(i_sell))

    # ── the certain answer first: the same purchase session ──
    # The purchase bot writes the order's supplier page into the row, and the
    # delivery email is about that very page — so when both are present this
    # is an identity, not a guess. Everything below is the fallback for rows
    # that predate it (matching on size, destination and a time window, which
    # can only ever be circumstantial and needs a human when two orders look
    # alike). The PC bot's uncertain-payment placeholder carries its session
    # with a '#payment-uncertain' fragment; the id stops at the '#', so that
    # row is matched here exactly too, and complete_row rewrites the cell with
    # the clean link.
    wanted = set()
    for u in (delivery.get("success_urls") or [delivery.get("success_url", "")]):
        wanted |= _session_ids(u)
    if wanted:
        for n, r in enumerate(rows[1:], start=2):
            get = lambda i: r[i].strip() if i is not None and len(r) > i else ""
            if not get(i_order) or get(i_act) or get(i_iccid):
                continue                                # not pending
            if dead(get):
                continue
            for col in (i_link, i_qr):                  # older rows kept it in QR
                if _session_ids(get(col)) & wanted:
                    log.info(f"row {n} matched by purchase session (exact)")
                    return {"row": n, "time": _row_time(get(i_date)),
                            "order_id": get(i_order), "customer_email": get(i_mail),
                            "order_url": get(i_wave)}

    cands = []
    for n, r in enumerate(rows[1:], start=2):
        get = lambda i: r[i].strip() if i is not None and len(r) > i else ""
        if not get(i_order) or get(i_act) or get(i_iccid):
            continue                                    # not pending
        if dead(get):
            continue
        if _names_other_session([get(i_link), get(i_qr)], wanted):
            continue                                    # waiting for its OWN mail
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
    activation code — i.e. the purchase bot finished the order itself.

    Compared by session id as well as by the whole link: the PC bot's
    placeholder link carries a '#payment-uncertain' fragment (2026-10-20), and
    a row it later completed itself with that link still in place would
    otherwise read as "no row has this delivery" and alert the owner about an
    order that is fine."""
    wanted = {success_url} if isinstance(success_url, str) else set(success_url or [])
    wanted.discard("")
    if not wanted:
        return False
    wanted_ids = set()
    for u in wanted:
        wanted_ids |= _session_ids(u)
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
            cell = r[i].strip() if len(r) > i else ""
            if cell in wanted or (wanted_ids and _session_ids(cell) & wanted_ids):
                return True
    return False


def find_replaced_row(ws, delivery: dict) -> dict | None:
    """The superseded receipts row this delivery belongs to, or None.

    A delivery for a row marked replaced is a purchase that went through after
    the order had already been bought again or replaced — the checkout closed
    mid-payment, a 3DS approval that landed late. find_pending_row never
    offers this row; this is how the mail is recognised instead of waiting two
    hours to be called an orphan. The row's session survives as
    `superseded-session:<id>` (or intact, when the owner marked the Status by
    hand).

    It does NOT say whose package it is — handle_replaced_delivery asks the
    site that. `live_rows` are the other rows of the same order number that
    still read as the sale (a same-number re-buy): when the site turns out to
    hold THIS package, one of them is the spare instead."""
    wanted = set()
    for u in (delivery.get("success_urls") or [delivery.get("success_url", "")]):
        wanted |= _session_ids(u)
    if not wanted:
        return None
    rows = ws.get_all_values()
    hdr = [h.strip() for h in rows[0]]
    idx = lambda name: hdr.index(name) if name in hdr else None
    i_order, i_status = idx(H_ORDER), idx(H_STATUS)
    i_link, i_qr, i_buy, i_sell = idx(H_LINK_SUP), idx(H_QR), idx(H_BUY), idx(H_SELL)
    cols = [i for i in (i_link, i_qr) if i is not None]
    for n, r in enumerate(rows[1:], start=2):
        get = lambda i: r[i].strip() if i is not None and len(r) > i else ""
        replaced = is_replaced_status(get(i_status))
        links = [get(i) for i in cols]
        if not (replaced or any(SUPERSEDED_SESSION in c for c in links)):
            continue
        ids = set()
        for c in links:
            ids |= {m.group(1) for m in SUPERSEDED_RE.finditer(c)}
            if replaced:
                ids |= _session_ids(c)
        if not ids & wanted:
            continue
        oid = get(i_order)
        live = []
        for m, r2 in enumerate(rows[1:], start=2):
            g2 = lambda i: r2[i].strip() if i is not None and len(r2) > i else ""
            if m == n or not oid or g2(i_order).upper() != oid.upper():
                continue
            if not _dead_row(g2(i_status), g2(i_link), g2(i_qr), g2(i_buy), g2(i_sell)):
                live.append(m)
        return {"row": n, "order_id": oid, "status": get(i_status), "live_rows": live}
    return None


def _a1(ws_hdr: list, row_number: int, name: str) -> str | None:
    if name not in ws_hdr:
        return None
    return gspread.utils.rowcol_to_a1(row_number, ws_hdr.index(name) + 1)


def _paint(ws, hdr: list, row_number: int, colour: dict) -> bool:
    """The Mail cell's fill, by header name. Best effort: the colour is the
    owner's convenience, never a reason to fail the write it follows."""
    cell = _a1(hdr, row_number, H_MAIL)
    if not cell:
        return False
    try:
        ws.format(cell, {"backgroundColor": colour})
        return True
    except Exception as e:
        log.warning(f"row {row_number}: could not set the Mail colour: {_redact(str(e))}")
        return False


def supersede_row(ws, row_number: int, hdr: list | None = None, before: list | None = None,
                  neutralise: bool = True) -> list[tuple[str, str, str]]:
    """Mark one receipts row as NOT a sale, in place — the PC bot's
    bot/sheets._supersede_row with zero_buy=False, in one shared spelling for
    both cloud writers: Status REPLACED_CHECK_STATUS ('replaced - check the
    charge'), Sell 0, no discount, the Mail cell WHITE, and (neutralise) the
    session in its supplier link defused so no reader takes it for a pending
    purchase again.

    Buy is KEPT: nobody here can prove the charge was never made, and a cost
    the owner zeroes after checking is safer than one that vanished. The fill
    matters as much as the Sell: green Mail is the owner's "money banked"
    mark, and a superseded row left green shows one payment as two when he
    reconciles PayPal's deposits by hand (review 2026-10-20).

    Returns [(header, before, after)] for the owner's alert, so every change
    can be undone by hand."""
    hdr = hdr if hdr is not None else [h.strip() for h in ws.row_values(1)]
    before = before if before is not None else ws.row_values(row_number)

    def was(name):
        i = hdr.index(name) if name in hdr else -1
        return before[i] if 0 <= i < len(before) else ""
    target = {H_STATUS: REPLACED_CHECK_STATUS, H_SELL: "0.00$", H_SALE: ""}
    if neutralise:
        for name in (H_LINK_SUP, H_QR):
            old = str(was(name)).strip()
            if old and neutralised_link(old) != old:
                target[name] = neutralised_link(old)
    cells, changes = [], []
    for name, val in target.items():
        if name not in hdr:
            continue
        cells.append(gspread.Cell(row_number, hdr.index(name) + 1, val))
        changes.append((name, was(name), val))
    if cells:
        ws.update_cells(cells, value_input_option="USER_ENTERED")
    if _paint(ws, hdr, row_number, WHITE_BG):
        changes.append((H_MAIL + " (fill)", "?", "white"))
    return changes


def mark_row_superseded(ws, row_number: int) -> list[tuple[str, str, str]]:
    """Mark a row the cloud bot just completed as NOT a sale, because the site
    said its order had already been replaced (or already holds another eSIM).
    supersede_row without touching the link: the row holds the package now,
    and its supplier page is what the owner opens to shelve or refund it.
    Without this the row read as a second sale of an order the customer paid
    for once. Best effort per the caller."""
    return supersede_row(ws, row_number, neutralise=False)


def supersede_placeholders(ws, order_id: str, keep=(), skip_route_prefix: str = "") -> list:
    """Every eSIM-less row of `order_id` (no activation code, no ICCID) that
    still reads as the sale, other than `keep`, marked with supersede_row.
    Returns [(row, header, before, after)].

    For a purchase that carries the order's money on a NEW row of the same
    number — the Stellar buyer taking over an esim.dog order whose checkout
    ended uncertain (rebuy_order.py, 's'). Left alone, the old placeholder
    kept its Sell and its green rail beside the new row's, and the books
    counted the customer's one payment twice (the 10-11 double count, review
    2026-10-20). `skip_route_prefix` keeps the caller's own supplier's rows
    out of it: they are its attempts, and it settles them itself."""
    oid = str(order_id or "").strip().upper()
    if not oid:
        return []
    rows = ws.get_all_values()
    if not rows:
        return []
    hdr = [h.strip() for h in rows[0]]
    idx = lambda name: hdr.index(name) if name in hdr else None
    i_order, i_act, i_iccid, i_route = idx(H_ORDER), idx(H_ACT), idx(H_ICCID), idx(H_ROUTE)
    i_status, i_link, i_qr, i_buy, i_sell = (idx(H_STATUS), idx(H_LINK_SUP), idx(H_QR),
                                             idx(H_BUY), idx(H_SELL))
    if i_order is None:
        return []
    out = []
    for n, r in enumerate(rows[1:], start=2):
        get = lambda i: r[i].strip() if i is not None and len(r) > i else ""
        if n in keep or get(i_order).upper() != oid or get(i_act) or get(i_iccid):
            continue
        if skip_route_prefix and get(i_route).startswith(skip_route_prefix):
            continue
        if _dead_row(get(i_status), get(i_link), get(i_qr), get(i_buy), get(i_sell)):
            continue
        for h, b, a in supersede_row(ws, n, hdr, list(r), neutralise=True):
            out.append((n, h, b, a))
    return out


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

# When to shout about a paid order nobody is buying, in minutes since payment.
# The first version reminded every 2 hours for as long as the order sat there,
# and per order — four stuck test orders produced ~100 identical emails in a
# day (2026-08-20). An alert that cannot be exhausted trains you to ignore the
# whole channel, which costs more than the alert ever bought. So: a ladder that
# ENDS. After the last rung the order is still broken and still listed by the
# bot panel's Orders screen and the daily digest, but it stops mailing.
STALE_ALERT_LADDER_MIN = (15, 60, 240, 720, 1440)
# Belt and braces on top of the stored rung below: even with no memory at all,
# a rung goes quiet this many minutes after it came due. The stored state is
# what makes each rung mail exactly ONCE; this is what bounds the damage if the
# site cannot store it (an old deploy, a KV outage) - tens of emails in the
# worst case instead of one every five minutes forever.
STALE_ALERT_BACKSTOP_MIN = 30

# Which rungs have already been mailed is kept ON THE ORDER, at the site
# (`stale_alert_rung`), never inferred from the clock here. This job is
# stateless and runs every 5 minutes with drift, so any time-window guess is
# wrong in one of two ways: wider than the drift and it mails twice, narrower
# and a late run skips the rung entirely. The window version of this was still
# sending 35 emails where 5 were wanted.


def _rung_for(age_min: float) -> int:
    """Highest ladder rung this order has reached, 0 before the first."""
    reached = [t for t in STALE_ALERT_LADDER_MIN if age_min >= t]
    return reached[-1] if reached else 0


def _mark_alerted(marks):
    """Record the rungs just mailed, so the next run does not repeat them."""
    if not marks:
        return
    try:
        r = requests.post(ORDERS_URL, json={"action": "alert_sent", "marks": marks},
                          timeout=20,
                          headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
        r.raise_for_status()
    except Exception as e:
        # Worst case the next run repeats one email. Failing to mark must not
        # cost the alert itself - the alert is the thing that matters.
        log.warning(f"could not record stale-alert state: {_redact(str(e))}")


def check_stale_pending():
    """Alert while PAID orders sit 'pending' with nothing buying them.

    The purchase bot lives on a home PC; every failure mode of that machine
    (off, crashed, stuck ledger claim) looks from here like an order aging
    quietly in the queue — which is exactly how WR-ZWLM87 waited 9 hours on
    2026-08-19 with every screen green. This runs in the cloud every 5
    minutes, so the owner hears about it no matter what died.

    ONE email covers every order due, each order climbs a ladder that ENDS,
    and each rung is mailed exactly once because the site remembers it. All
    three were missing: the first version mailed per order on a repeating
    2-hour modulo, and four stuck test orders produced ~100 identical emails
    in a day (2026-08-20). An alert you cannot exhaust teaches you to ignore
    the channel, which costs more than the alert was ever worth.

    probe=1: reading the queue here must not refresh the purchase bot's
    heartbeat, or this very check would hide the outage it looks for.
    """
    r = requests.get(ORDERS_URL, params={"status": "pending", "probe": "1"},
                     timeout=20,
                     headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
    r.raise_for_status()
    now = datetime.now(timezone.utc)

    due, marks, silent = [], [], 0
    for o in r.json().get("orders", []):
        ts = str(o.get("ts", ""))
        try:
            age_min = (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))
                       ).total_seconds() / 60
        except ValueError:
            continue
        if age_min < STALE_PENDING_MIN:
            continue
        log.error(f"order {o.get('order_id', '?')} has been PAID and pending "
                  f"for {age_min:.0f} min - nothing is buying it")
        rung = _rung_for(age_min)
        fresh = rung > (int(o.get("stale_alert_rung") or 0))
        if fresh and age_min < rung + STALE_ALERT_BACKSTOP_MIN:
            due.append((age_min, o))
            marks.append({"order_id": o.get("order_id"), "rung": rung})
        else:
            silent += 1

    if not due:
        if silent:
            log.warning(f"{silent} order(s) still pending; every alert rung "
                        f"already sent - see the bot panel, Orders screen")
        return

    due.sort(key=lambda x: -x[0])
    lines, suppliers = [], set()
    for age_min, o in due:
        sup = str(o.get("supplier") or "esim.dog")
        suppliers.add(sup)
        age = (f"{age_min:.0f} min" if age_min < 90
               else f"{age_min / 60:.1f} hours")
        # A gift says so: "$0" here read like an order whose price was lost.
        paid = "FREE (gift)" if is_free(o) else f"${o.get('paid_usd', '?')}"
        lines.append(
            f"  {o.get('order_id', '?')}  -  {age} ago  -  "
            f"{paid}  -  SKU {o.get('sku', '?')}  -  buys at {sup}"
        )
    n = len(due)
    # Each buyer has its own fix. Sending the owner to the PC for a Stellar
    # order (bought from the cloud, or by nobody yet) wastes the one thing
    # this alert exists to save: the minutes before the customer writes in.
    hints = []
    if "esim.dog" in suppliers:
        hints.append(
            "esim.dog orders: either the purchase bot on the PC is down, or its "
            "ledger holds a stuck claim.\n\n"
            "OPEN THE BOT PANEL ON THE PC AND PRESS 5 (Orders).\n"
            "That screen lists these orders, says why each one is stuck, and "
            "fixes them: release a stuck claim, retry, or close an order you "
            "have already handled yourself.\n\n"
            "If the panel will not open:\n"
            "1. The BOT row must say RUNNING (key 1 starts it; the watchdog "
            "restarts it within 5 min).\n"
            "2. In a command window:  venv\\Scripts\\python.exe -m bot.main --stuck\n"
            "3. Bot running, nothing stuck, order still pending -> read bot.log."
        )
    others = sorted(suppliers - {"esim.dog"})
    if others:
        hints.append(
            f"{', '.join(others)} orders: NOT the PC's job. That buyer runs in "
            "GitHub Actions (or does not exist yet) - check its workflow runs, "
            "or settle the order by hand. Nothing on the PC can buy or release "
            "these."
        )
    alert(
        f"{n} paid order{'s' if n > 1 else ''} waiting - nothing is buying "
        f"{'them' if n > 1 else 'it'}",
        f"{n} order{'s have' if n > 1 else ' has'} been paid for and "
        f"{'are' if n > 1 else 'is'} still sitting in the site queue:\n\n"
        + "\n".join(lines) +
        (f"\n\n(plus {silent} older one(s) already reported)" if silent else "")
        + "\n\n" + "\n\n".join(hints) + "\n\n"
        f"Reminders for one order stop "
        f"{STALE_ALERT_LADDER_MIN[-1] // 60} hours after payment, fixed or "
        f"not - do not read silence as resolved. The Orders screen always "
        f"shows the truth.",
    )
    _mark_alerted(marks)


def report_email_sent(order_id: str, ok: bool, error: str = "", address: str = "",
                      lease: str = "") -> bool:
    """Close (or keep open) this order's ledger entry. True if the site took it.

    Retried, because this POST is not bookkeeping — it is what stops the buyer
    being mailed again. A single lost confirmation used to be the whole story
    behind a duplicate eSIM email: the send succeeded, the site never heard so,
    and the next sweep dutifully sent a second copy.

    `lease` (2026-10-21) is the mail lease send_once took: the report hands it
    back, so the next sender does not wait out its fifteen minutes.
    """
    payload = {"order_id": order_id, "email_sent": bool(ok)}
    if error:
        payload["error"] = error[:300]
    if ok and address:
        payload["customer_email"] = address
    if lease:
        payload["lease"] = lease
    last = None
    for attempt in range(1, LEDGER_CONFIRM_ATTEMPTS + 1):
        try:
            r = requests.post(ORDERS_URL, json=payload, timeout=20,
                              headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
            r.raise_for_status()
            return True
        except Exception as e:
            last = e
            if attempt < LEDGER_CONFIRM_ATTEMPTS:
                time.sleep(2 * attempt)
    log.warning(f"order {order_id}: ledger update failed after "
                f"{LEDGER_CONFIRM_ATTEMPTS} tries: {_redact(str(last))}")
    return False


# ── one email per order (2026-10-21) ─────────────────────────────────────────
# Three senders in this repo can write a customer's "your eSIM is ready" mail
# — process() on the supplier's delivery mail, deliver_pending_emails() on the
# site's ledger, and the Stellar buyer the moment it delivers — plus the
# site's own escalation. Each had its own guard, and process() had none at
# all: the PC hands a session to the site, the site completes the order, the
# ledger sweep mails the customer; the supplier's delivery mail lands a run
# later, matches the still-pending receipts row, and process() mailed the
# customer AGAIN (G2). The owner's rule since 2026-10-21 is that a second
# PACKAGE for an order is acceptable and a second EMAIL is not, so every
# automatic sender now goes through the one function below, with the same
# four checks in the same order.
MAIL_LEASE_TTL_S = 900


def claim_mail(order_id: str, by: str, ttl_s: int = MAIL_LEASE_TTL_S) -> dict:
    """Take the site's mail lease for this order (api/orders.js mail_claim).

      {"claim": True, "lease": "..."}            ours: write the mail
      {"claim": False, "reason": "sent"}          the mail already went
      {"claim": False, "reason": "claimed"}       another sender is writing it NOW
      {"claim": False, "reason": "not_owed"}      parked, closed or no eSIM yet
      {"claim": None, "reason": "..."}            no lease to be had: a site from
                                                  before 2026-10-21 (400), or the
                                                  site could not be asked

    The last one is NOT a refusal. A lease that cannot be taken leaves the
    sent stamp and the Sent folder as the guards, exactly as before the lease
    existed — withholding the mail because a bookkeeping call failed would
    trade a duplicate that needs two failures for a no-show that needs one."""
    try:
        r = requests.post(ORDERS_URL, json={"action": "mail_claim", "order_id": order_id,
                                            "by": by, "ttl_s": int(ttl_s)},
                          timeout=20, headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
        if r.status_code == 400:
            return {"claim": None, "reason": "old_site"}
        r.raise_for_status()
        out = r.json()
    except Exception as e:
        log.warning(f"order {order_id}: mail lease not available ({_redact(str(e))}) "
                    f"- the sent stamp and the Sent folder still guard the mail")
        return {"claim": None, "reason": "unreachable"}
    if not isinstance(out, dict) or "claim" not in out:
        return {"claim": None, "reason": "old_site"}
    if out.get("claim") is True and out.get("lease"):
        return {"claim": True, "lease": str(out["lease"])}
    if out.get("claim") is False:
        return {"claim": False, "reason": str(out.get("reason") or "claimed"),
                "by": str(out.get("by") or "")}
    return {"claim": None, "reason": "old_site"}


def send_once(inbox, order_id: str, to: str, send_fn, by: str, facts: dict | None = None) -> str:
    """Send this order's customer mail AT MOST ONCE, whoever is asking.

    Returns what happened, for the caller's log:
      'sent'         written and recorded at the site;
      'unconfirmed'  written, but the site would not record it (the Sent
                     folder and the lease still stop a second one);
      'already'      it went before — the site's stamp or the Sent folder;
      'claimed'      another sender holds the lease and is writing it now;
      'withheld'     the site says no mail is owed (parked, closed, no eSIM);
      'failed'       the send itself failed; the ledger keeps it for retry.

    The checks, cheapest and surest first:
      1. the site's customer_email_sent_ts on `facts` (read here when the
         caller has not already);
      2. the mail lease — refused, nothing is written; unavailable (an old
         site), carry on with the two guards below;
      3. the Sent folder — what went out is in Sent whatever the site
         believes (memory: duplicate-esim-email). A Sent folder that cannot
         be read is "not yet": the lease is the guard then, and a no-show is
         worse than the duplicate that would need every guard to fail;
      4. send, then report_email_sent with the lease, which hands it back.

    `inbox` may be an Inbox, None, or a zero-argument callable returning one
    (the Stellar buyer opens the mailbox only when a mail is due)."""
    oid = str(order_id or "")
    if not to:
        report_email_sent(oid, False, "no customer address on file")
        log.warning(f"order {oid}: still no customer address - email deferred")
        return "failed"
    if facts is None:
        facts = order_facts(oid)
    if (facts or {}).get("customer_email_sent_ts"):
        log.info(f"order {oid}: the site says the customer was already mailed - not sent again")
        return "already"
    got = claim_mail(oid, by)
    if got.get("claim") is False:
        why = got.get("reason")
        if why == "sent":
            log.info(f"order {oid}: already mailed (mail lease: sent) - not sent again")
            return "already"
        if why == "not_owed":
            log.info(f"order {oid}: the site owes this order no mail now - not sent")
            return "withheld"
        log.info(f"order {oid}: {got.get('by') or 'another sender'} is writing this mail now - not sent here")
        return "claimed"
    lease = got.get("lease") or ""
    box = inbox() if callable(inbox) else inbox
    if box is not None and box.already_mailed_to(oid, to):
        # The buyer has it; only the bookkeeping is behind. Close the entry —
        # and if even that will not go through, still do not send: a stale
        # ledger row is cheaper than a second eSIM.
        if report_email_sent(oid, True, address=to, **({"lease": lease} if lease else {})):
            log.info(f"order {oid}: already in Sent - ledger closed, not resent")
        else:
            log.warning(f"order {oid}: already in Sent but the ledger would not close - still not resending")
        return "already"
    try:
        send_fn()
    except Exception as e:
        report_email_sent(oid, False, str(e), **({"lease": lease} if lease else {}))
        log.warning(f"order {oid}: eSIM email failed, the ledger keeps it for retry: {_redact(str(e))}")
        return "failed"
    if box is not None and hasattr(box, "note_sent"):
        box.note_sent(oid, to)
    if report_email_sent(oid, True, address=to, **({"lease": lease} if lease else {})):
        log.info(f"order {oid}: eSIM email delivered (ledger closed)")
        return "sent"
    log.warning(f"order {oid}: eSIM email delivered but the site did not record it")
    return "unconfirmed"


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

    "Owed" is the site's opinion, and it is wrong in one direction: an order
    whose email went out but whose confirmation was lost still looks owed, and
    retrying THAT sends the buyer a second eSIM. So before every send we ask
    the mailbox — which cannot be wrong about what it sent — and close the
    entry instead of mailing again.
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

    # Worth a mailbox connection only now that there is something to resend.
    # main() has already closed its own by the time the sweep runs, and the
    # sweep must keep working when that one could not be opened at all.
    inbox = None
    try:
        inbox = _open_inbox(attempts=1)
    except Exception as e:
        log.warning(f"cannot open the mailbox to check for earlier sends "
                    f"({_redact(str(e))}) — sending on the ledger's word alone")

    try:
        for o in owed:
            oid = str(o.get("order_id", ""))
            to = str(o.get("customer_email", "")).strip() or by_order.get(oid, "")
            # Nothing to send to: send_once keeps the entry open and lets the
            # site escalate to the owner — filling the sheet's Mail column
            # repairs it. The awaiting row IS the site's fresh word that the
            # mail has not gone (the list leaves out every stamped order), so
            # it stands in for the facts read; the lease covers the minutes
            # since. free / replaces ride on the row from 2026-10-20; an older
            # site sends neither and the mail is as before.
            send_once(inbox, oid, to,
                      lambda o=o, to=to, oid=oid: send_customer_email(
                          to, oid, str(o.get("order_url", "")), _delivery_from_record(o),
                          esim=o.get("esim") or {}, lang=str(o.get("lang", "")),
                          total=o.get("paid_usd"), free=is_free(o),
                          replaces=str(o.get("replaces") or "")),
                      by="cloud:sweep", facts=o)
    finally:
        if inbox is not None:
            inbox.close()


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
    # Which purchase this is (2026-10-21): the delivery mail's own session. A
    # site that parks the package records it on the extra; one that serves it
    # marks that session as the customer's, and watches any other.
    if m := SESSION_ID_RE.search(str(delivery.get("success_url") or "")):
        payload["supplier_session"] = m.group(1)
    r = requests.post(ORDERS_URL, json=payload, timeout=20,
                      headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
    r.raise_for_status()
    try:
        answer = r.json()
    except Exception:
        answer = {}
    answer = answer if isinstance(answer, dict) else {}
    if site_parked(answer):
        log.warning(f"order {order_id}: the site kept this package as a spare "
                    f"(late_delivery) - the customer is NOT to be mailed")
    else:
        log.info(f"order {order_id}: site fulfilled")
    return answer


def site_parked(answer: dict) -> bool:
    """The site's answer to a 'fulfilled' report says it PARKED this package
    (api/orders.js, 2026-10-20) — it is not the customer's:

      * the order was settled and REPLACED by another one, which carries the
        customer's eSIM — the package is kept on the record as an extra;
      * the order already holds a DIFFERENT eSIM, which the customer may have
        installed — the second package is kept the same way, and the first
        one's mail stays owed on the delivery ledger.

    This is the question for the RECEIPTS ROW: a parked package's row stops
    reading as a sale. An older site answers neither key, and that reads as
    "not parked", exactly as before."""
    return bool(answer.get("late_delivery")) or answer.get("customer_mail") is False


def site_withholds_mail(answer: dict) -> bool:
    """The site's answer says the customer must NOT be mailed for this report:
    the package was parked (site_parked), or — 2026-10-21 — the mail is simply
    not owed any more (`mail_owed: false`: it already went, from another
    sender). Either way the mail must not go, and report_email_sent must not
    be called: for a parked package it would close the ledger entry of the
    eSIM the customer actually needs.

    Only the mail. A plain answer with mail_owed:false is the customer's OWN
    package reported again after the mail went (the delivery mail arriving
    after the sweep mailed them, G2) — that row is the sale, and treating it
    like a parked one would zero it."""
    return site_parked(answer) or answer.get("mail_owed") is False


def order_facts(order_id: str) -> dict:
    """The site's owner-only view of one order ({} when it cannot be read):
    `free`, `replaces`, `replaced_by` among the rest. probe=1 so the read is
    never mistaken for a buyer's poll.

    Asked only where the delivery mail would otherwise go out without them —
    process() completes a row from the supplier's mail, and the receipts row
    does not say whether its order replaced another. A failed read costs one
    line of the mail, never the mail."""
    try:
        r = requests.get(ORDERS_URL, params={"order_id": order_id, "probe": "1"}, timeout=20,
                         headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
        r.raise_for_status()
        out = r.json()
        return out if isinstance(out, dict) else {}
    except Exception as e:
        log.warning(f"order {order_id}: could not read the order's facts: {_redact(str(e))}")
        return {}


def site_park_extra(order_id: str, session_id: str, details: dict, via: str,
                    supplier: str = "esim.dog", supplier_ref: str = "") -> dict:
    """Tell the site about a package it never saw, for an order that is not its
    home (api/orders.js extra_package, 2026-10-21): the package of a REPLACED
    receipts row whose order holds no eSIM it could be. The site keeps it on
    the order's extras, and the stock shelf (stock_shelf.py) takes it from
    there — the ONE alert the owner gets about it.

    Returns the site's answer: {"parked": True} — or {"error": "order_waiting"}
    (the order is still waiting for its eSIM: this package is the customer's,
    not an extra) / {"error": "is_serving"} (it IS the eSIM on the page) on a
    409. RAISES when the site cannot be asked, or predates the action (400)."""
    esim = {"activation_code": details.get("activation_code", ""),
            "qr_code": details.get("qr_code", ""), "smdp": details.get("smdp", ""),
            "iccid": details.get("iccid", ""), "apn": details.get("apn", "")}
    body = {"action": "extra_package", "order_id": order_id, "esim": esim, "via": via,
            "supplier": supplier}
    # `sid`, never `session_id`: to a site from before this action, a body
    # with session_id and no status IS a session handover -- it would put
    # this package on the customer's page of a failed order. Under `sid` an
    # old site answers 400, and the owner is told instead.
    if session_id:
        body["sid"] = session_id
    if supplier_ref:
        body["supplier_ref"] = supplier_ref
    r = requests.post(ORDERS_URL, json=body, timeout=20,
                      headers={"Authorization": f"Bearer {env('ORDERS_TOKEN')}"})
    if r.status_code == 409:
        try:
            out = r.json()
        except Exception:
            out = {}
        return out if isinstance(out, dict) and out.get("error") else {"error": "conflict"}
    r.raise_for_status()
    out = r.json()
    return out if isinstance(out, dict) else {}


# ── customer "ready" email (from waverolesupply@gmail.com) ───────────────────
SUPPORT_EMAIL = "waverolesupport@gmail.com"
NAVY, BEIGE, BROWN, ACCENT = "#1B365D", "#f7ede2", "#7a5c40", "#C27A4E"


REPLACES_ID_RE = re.compile(r"^WR-[A-Z0-9]{4,12}$")


def send_customer_email(to: str, order_id: str, order_url: str, delivery: dict,
                        esim: dict | None = None, lang: str = "", total=None,
                        free=None, replaces: str = ""):
    """The buyer's 'your eSIM is ready' email. Sent from the real Waverole
    Gmail (waverolesupply) — the site's Resend sender (onboarding@resend.dev)
    looked untrustworthy, so this bot owns the customer email now.

    LINK ONLY since 2026-09-10 (owner's decision of 09-09): no QR, no
    activation code, no SM-DP+, ICCID or APN in the mail. An emailed credential
    is irrevocable — on 2026-09-08 three customers were mailed a second working
    eSIM by mistake and nothing could take it back; the order page can be
    blocked in seconds. The customer installs from the order page, where the
    QR is drawn on the device. `esim` is accepted for the callers' sake and
    not read.

    free (2026-10-20): a package the owner GAVE away. The 'Total paid' row
    says "Free" — the same word the order page uses — instead of "$0.00",
    which reads like a failed charge. None means "not told": the order link's
    `fr: 1` decides. A free flag next to a real price is a contradiction about
    money and the price wins (same rule as is_free).

    replaces: the order number this one REPLACES (new_order.py --replaces).
    The customer's first order page now forwards here, and without a word the
    mail reads like a second purchase — so one line says it replaces their
    order and that there is nothing more to pay."""
    if not to:
        raise ValueError("missing customer email address")
    # With nothing but the link in the mail, a mail without the link would say
    # "your eSIM is ready" and give no way to reach it. Refuse; the ledger
    # keeps the order and the site escalates to a person (same rule as the
    # site's own Resend fallback).
    if not order_url:
        raise ValueError("missing order link — a link-only email would be empty")

    payload = order_payload(order_url)
    # Explicit values (from the site's own order record) win over whatever we
    # can decode from the link — the record is authoritative and always there.
    if total is None:
        total = payload.get("t")                 # what the customer actually paid
    heb = (lang or payload.get("l", "")) == "he" # site language at purchase time
    gb, days = delivery.get("gb"), delivery.get("days")
    if free is None:
        free = payload.get("fr") == 1
    try:
        priced = total is not None and float(total) > 1e-9
    except (TypeError, ValueError):
        priced = False
    gift = free is True and not priced
    old = str(replaces or "").strip().upper()
    old = old if REPLACES_ID_RE.match(old) and old != str(order_id).strip().upper() else ""
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
        # "Free" (Hebrew: chinam) — the order page's own word (index.html dictionary).
        "free": "\u05d7\u05d9\u05e0\u05dd" if heb else "Free",
        # "This order replaces your order {old} — nothing more to pay."
        "replaces": (("\u05d4\u05d4\u05d6\u05de\u05e0\u05d4 \u05d4\u05d6\u05d5 "
                      "\u05de\u05d7\u05dc\u05d9\u05e4\u05d4 \u05d0\u05ea "
                      "\u05d4\u05d6\u05de\u05e0\u05d4 {old} \u05e9\u05dc\u05da — "
                      "\u05d0\u05d9\u05df \u05e6\u05d5\u05e8\u05da \u05dc\u05e9\u05dc\u05dd "
                      "\u05e9\u05d5\u05d1.") if heb
                     else "This order replaces your order {old} — nothing more to pay."),
    }
    if gift:
        paid_text = L["free"]
    else:
        paid_text = f"${total:.2f}" if isinstance(total, (int, float)) else ""
    rows = [("מספר הזמנה" if heb else "Order number", order_id),
            ("יעד" if heb else "Destination", delivery.get("location", "")),
            ("נפח גלישה" if heb else "Data", f"{gb:g} GB" if gb else ""),
            ("תוקף" if heb else "Validity",
             (f"{days} ימים" if heb else f"{days} days") if days else ""),
            ("רשת" if heb else "Network", delivery.get("network", "")),
            ("סה״כ שולם" if heb else "Total paid", paid_text)]
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
    # The old number sits in its own left-to-right span: inside a Hebrew
    # sentence a bare "WR-ABC123" is reordered by the bidi algorithm.
    replaces_line = (f"""<p style="text-align:center;color:{NAVY};font-size:13px;margin:0 0 20px">"""
                     + L["replaces"].format(old=f'<strong dir="ltr">{old}</strong>') + "</p>"
                     if old else "")

    html = f"""<div dir="{L['dir']}" style="font-family:'Nunito',Arial,sans-serif;max-width:520px;margin:0 auto;padding:32px;background:{BEIGE};border-radius:16px">
  <h1 style="font-size:22px;color:{NAVY};text-align:center;margin:0 0 6px">{L['title']}</h1>
  <p style="text-align:center;color:{NAVY};font-size:16px;font-weight:700;margin:0 0 4px">{L['ready']}</p>
  <p style="text-align:center;color:{BROWN};font-size:13px;margin:0 0 20px">{L['order']} <strong style="color:{NAVY}">{order_id}</strong></p>
  {replaces_line}
  <table style="width:100%;background:#fff;border-radius:12px;border-collapse:collapse;margin:0 0 20px">{detail_rows}</table>
  {cta}
  <p style="text-align:center;color:{BROWN};font-size:13px;margin:0 0 6px">{L['guide']}</p>
  <p style="text-align:center;color:{BROWN};font-size:13px;margin:0 0 18px">{L['problem']}
    <a href="mailto:{SUPPORT_EMAIL}" style="color:{ACCENT};font-weight:700;text-decoration:none">{SUPPORT_EMAIL}</a></p>
  {footer}
</div>"""

    msg = MIMEMultipart("related")
    msg.attach(MIMEText(html, "html", "utf-8"))
    msg["From"] = f"Waverole <{GMAIL_USER}>"
    msg["To"] = to
    msg["Subject"] = L["subject"]
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(GMAIL_USER, env("GMAIL_APP_PASSWORD").replace(" ", ""))
        s.send_message(msg)
    log.info(f"order {order_id}: customer email sent")  # the log is public; never the address


# ── main ─────────────────────────────────────────────────────────────────────

def process(inbox: Inbox, ws, d: dict):
    uid = d["uid"]
    log.info(f"email uid={uid}: {d['gb']}GB/{d['days']}d {d['location']}")

    # An identity before any guess: a delivery whose session belongs to a row
    # marked REPLACED is never written onto a look-alike pending row by the
    # size-and-time fallback below. Whose package it is, the site decides
    # (handle_replaced_delivery) — the mark alone is not proof.
    try:
        late = find_replaced_row(ws, d)
    except Exception as e:                       # the guess below still runs
        log.warning(f"email uid={uid}: replaced-row check failed: {_redact(str(e))}")
        late = None
    if late:
        handle_replaced_delivery(inbox, ws, d, late)
        return

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
        answer = report_fulfilled(order_id, d, details)
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
    if site_parked(answer):
        # The site has already replaced this order (or it holds another eSIM),
        # so the package just written to row N is a second one for a customer
        # who paid once. The site parked it on the order's extras; this side
        # owes two things: no customer mail, and a row that stops reading as a
        # second sale. The row keeps the eSIM — it is the extra's record, and
        # stock_shelf.py reads its cost from it when it shelves the package.
        # No alert from here any more (2026-10-21): the shelf's alert is the
        # owner's ONE mail about this package. Only a row that could NOT be
        # marked is worth his attention now, because that row still reads as
        # a sale in the books.
        try:
            changes = mark_row_superseded(ws, match["row"])
            done = "; ".join(f"{h}: '{b}' -> '{a}'" for h, b, a in changes) or "nothing"
            log.info(f"order {order_id}: second package parked by the site; receipts row "
                     f"{match['row']} marked not-a-sale ({len(changes)} change(s))")
        except Exception as e:
            done = f"NOT marked ({_redact(str(e))}) - mark it by hand"
            alert(f"Order {order_id}: second package parked - its receipts row is NOT marked",
                  f"The site answered that order {order_id} was already replaced, or already "
                  f"holds a different eSIM, so this delivery was not shown or mailed to the "
                  f"customer. It goes to the stock shelf by itself.\n\nReceipts row "
                  f"{match['row']} holds the package and still reads as a sale: {done}.\n\n"
                  f"Set its Status to '{REPLACED_CHECK_STATUS}', Sell 0, no discount, Mail white.")
        inbox.flag(uid)
        log.info(f"order {order_id}: late package parked (row {match['row']})")
        return
    if answer.get("mail_owed") is False:
        # The customer's OWN package, reported again after their mail already
        # went: the PC handed the session, the site completed the order, the
        # ledger sweep mailed them — and the supplier's delivery mail landed a
        # run later and matched the still-pending row (G2, 2026-10-21). The row
        # is the sale and is now complete; the mail is not owed again.
        inbox.flag(uid)
        log.info(f"order {order_id} COMPLETED (row {match['row']}) - the customer was already "
                 f"mailed, not mailed again")
        return
    # The buyer's email is now tracked in the site's delivery ledger (opened by
    # report_fulfilled above). Send it right away for speed, through the one
    # sender that sends an order's mail at most once (send_once): a failure
    # here is not the end of the road, deliver_pending_emails retries it every
    # run and the site escalates on its own. No alert from here — that would
    # fire on every transient SMTP hiccup.
    to = match.get("customer_email", "")
    # The receipts row cannot say whether this order is a gift or replaces an
    # earlier one; the site's record can (one read, and only on this path —
    # the sweep's rows already carry both). The same read carries the sent
    # stamp send_once checks first.
    facts = order_facts(order_id)
    send_once(inbox, order_id, to,
              lambda: send_customer_email(to, order_id, match.get("order_url", ""), d, esim=details,
                                          free=is_free(facts) if facts else None,
                                          replaces=str(facts.get("replaces") or "")),
              by="cloud:delivery", facts=facts)
    inbox.flag(uid)
    log.info(f"order {order_id} COMPLETED (row {match['row']})")


def sale_cells_from_facts(facts: dict, values: list | None = None, hdr: list | None = None) -> dict:
    """The Sell, Sale and Purchase cells of an order's sale row, rebuilt from
    the site's own record of it ({paid_usd, list_usd, source, free, replaces}
    from order_facts) — in the very spelling the buyers write them: the Sell
    as '6.99$', the Sale as the PC bot's and the Stellar buyer's discount text
    ('-' for none), the Purchase as the rail (or the replaced order's rail, or
    'free' for a gift; stellar_buyer.rail_for). For a row whose sale has to be
    written back with no live row to copy it from (G4). {} when the record
    says nothing: a cell left as it was beats a cell guessed."""
    import stellar_buyer as sb      # here, not at the top: stellar_buyer imports this module
    facts = facts or {}
    out = {}
    free = is_free(facts)
    try:
        paid = float(facts.get("paid_usd")) if facts.get("paid_usd") not in (None, "") else None
    except (TypeError, ValueError):
        paid = None
    if free:
        out[H_SELL] = "0.00$"
    elif paid is not None and paid > 0:
        out[H_SELL] = f"{paid:.2f}$"
        if sale := sb.discount_text(facts.get("list_usd"), paid):
            out[H_SALE] = sale
    rail = sb.rail_for(facts, values or [], hdr or [],
                       site_source=lambda old: order_facts(old).get("source"))
    if rail and rail != sb.SOURCE_UNKNOWN:
        out[H_PURCHASE] = rail
    return out


def _late_alert_body(late: dict, d: dict) -> str:
    return (f"Receipts row {late['row']} ({late['order_id'] or 'no order number'}) is marked "
            f"'{late['status'] or 'superseded'}', and this delivery belongs to its purchase:\n"
            f"{d['gb']}GB / {d['days']}d / {d['location']}\n{d['success_url']}\n\n")


def handle_replaced_delivery(inbox, ws, d: dict, late: dict):
    """A delivery mail for a receipts row marked replaced: whose package is it?

    The PC bot's mark says the row was superseded; it does not say which
    purchase reached the customer, and on a same-number re-buy that is not the
    question it looks like (review 2026-10-20). The first, uncertain checkout
    S1 is handed to the site; the PC buys S2; S1's bank approval lands and the
    site's sweep completes the order with ITS eSIM and mails the customer; S2
    is then reported on the session path, which answers "fulfilled" for an
    order that already is. The PC writes S2 as the live row and S1's as
    replaced — the opposite of what the customer installed. Calling S1's
    package a spare then shelves an eSIM somebody is using, and the next
    customer served from stock shares it.

    So the site is asked, and only the site's own record decides:
      * the order was replaced by ANOTHER order (replaced_by), or holds no
        eSIM and is no longer waiting for one -> nobody's page shows this
        package: a spare. Told to the owner; nothing written, nothing posted.
      * the order is fulfilled -> this package is reported to it. A site that
        holds a DIFFERENT eSIM parks this one (late_esim, customer_mail:false)
        and it is a spare, as above. A plain answer means the site already
        holds THIS one: it is the customer's eSIM, so its row becomes the sale
        and the live row of the same number becomes the spare.
      * the order is still pending -> its own session may yet complete it
        with this package; asked again next run, for UNMATCHED_GRACE_HOURS.
      * the site cannot be read, or cannot be trusted with the comparison (a
        site from before 2026-10-20 swaps the eSIM on the customer's page when
        a different one is reported; the `has_session` key in its lookup is
        how the new one is recognised), or holds an eSIM on a record that is
        not 'fulfilled' -> nobody decides blind: retried, then handed to the
        owner.
    The customer is never mailed from here: when this package is theirs, they
    have had it since the site completed the order, and the delivery ledger
    owns that mail.

    A spare (2026-10-21) is no longer only an alert asking the owner to shelve
    it by hand: it is reported to the site as an extra of its order
    (site_park_extra), and stock_shelf.py puts it on the stock tab and sends
    the ONE alert about it. Only a spare that cannot be reported — no order
    number on the row, a site that cannot be asked or predates the action —
    still alerts from here."""
    uid, oid = d["uid"], str(late.get("order_id") or "").strip()
    age = datetime.now(timezone.utc) - d["received_at"]
    head = _late_alert_body(late, d)
    got_details: dict = {}

    def details_of() -> dict:
        if not got_details:
            got_details.update(fetch_esim_details(d.get("success_urls") or [d["success_url"]]) or {})
            if got_details.get("used_url"):
                d["success_url"] = got_details["used_url"]
        return got_details

    def spare_alert(why: str, extra: str = ""):
        alert(f"Order {oid or '?'}: a package arrived for a REPLACED order",
              head + why + "\n\nNOTHING was sent to the customer and nothing was written "
              "to the sheet." + extra + "\n\nThe package is real and paid for: put it on the "
              f"spare-stock shelf (python -m bot.spares add {late['row']}), or ask the supplier for "
              f"a refund. If row {late['row']} says Buy 0, the charge was booked as not made - put "
              "the cost back.")
        inbox.flag(uid)

    def spare(why: str):
        if not oid:
            spare_alert(why)
            return
        det = details_of()
        if not (det.get("activation_code") or det.get("iccid")):
            undecided("the supplier's page did not give this package's details yet")
            return
        m = SESSION_ID_RE.search(str(d.get("success_url") or ""))
        try:
            ans = site_park_extra(oid, m.group(1) if m else "", det, via="cloud:replaced-row")
        except Exception as e:
            spare_alert(why, f" The site could not be told about it ({_redact(str(e))}), so it is "
                             "NOT on the order's extras and will not reach the stock shelf by itself.")
            return
        if ans.get("error") == "order_waiting":
            undecided(f"the site says {oid} is still waiting for its eSIM, so this package may be "
                      f"the customer's")
            return
        if ans.get("error"):
            spare_alert(why, f" The site refused it as an extra ('{ans.get('error')}')"
                             + (" - it says this IS the eSIM on the customer's page, so it must "
                                "never be shelved: compare the rows by hand."
                                if ans.get("error") == "is_serving" else "."))
            return
        log.info(f"order {oid}: the package of replaced row {late['row']} is parked on the site as "
                 f"an extra ({why.split('.')[0]}) - the stock shelf takes it from there")
        inbox.flag(uid)

    def undecided(why: str):
        if age < timedelta(hours=UNMATCHED_GRACE_HOURS):
            log.info(f"email uid={uid}: late package for {oid or '?'} not decided yet ({why}) - will retry")
            return                                   # NOT flagged -> asked again next run
        alert(f"Order {oid or '?'}: a package for a REPLACED order - decide by hand",
              head + f"It could not be settled by itself: {why}.\n\nOpen the order's page and "
              f"compare its ICCID with this package's. If they are the SAME, the customer has "
              f"this package: fill row {late['row']} with it and make it the sale, and mark the "
              f"other row of {oid or 'the order'} as the spare. If they differ, this one is a "
              "spare: shelf or supplier refund. Nothing was sent, written or posted.")
        inbox.flag(uid)

    if not oid:
        spare("The row carries no order number, so no customer page can hold it.")
        return
    facts = order_facts(oid)
    if not facts:
        undecided("the site's record of the order could not be read")
        return
    if facts.get("replaced_by"):
        spare(f"The site says {oid} was replaced by {facts['replaced_by']}, which carries the "
              f"customer's eSIM.")
        return
    status = str(facts.get("status") or "").strip().lower()
    if status == "pending" and not facts.get("esim_ready"):
        undecided("the order has no eSIM on the site yet, and this purchase may still be the "
                  "one that completes it")
        return
    if facts.get("esim_ready") and status != "fulfilled":
        # An eSIM on a record that is not 'fulfilled' (settled after delivery,
        # say): the page may show it, and the site's status path would write
        # a report over it rather than compare. Not a call to make blind.
        undecided(f"the site holds an eSIM for {oid} but the order is '{status or '?'}', "
                  f"so the two cannot be compared safely")
        return
    if not facts.get("esim_ready"):
        spare(f"The site holds no eSIM for {oid} (status '{status or '?'}'), so no customer page "
              f"shows this package. If {oid} is still owed an eSIM, this package could be given "
              f"to it by hand instead of buying another.")
        return
    if "has_session" not in facts:
        undecided("the site predates the 2026-10-20 second-package guard, and reporting a "
                  "different eSIM to it would replace the one on the customer's page")
        return
    details = details_of()
    if not details.get("activation_code"):
        # The site compares by activation code (it always holds one when
        # esim_ready); without ours the comparison would prove nothing, and
        # an undecided report is written onto the customer's page.
        undecided("the supplier's page did not give this package's activation code")
        return
    try:
        answer = report_fulfilled(oid, d, details)
    except Exception as e:
        undecided(f"the site report failed ({_redact(str(e))})")
        return
    if site_parked(answer):
        # The site holds a DIFFERENT eSIM for the order — the one the customer
        # has — and kept this one as an extra: the shelf takes it, and its
        # alert is the owner's one mail about it.
        log.info(f"order {oid}: the package of replaced row {late['row']} is NOT the customer's; "
                 f"the site parked it as an extra - the stock shelf takes it from there")
        inbox.flag(uid)
        return

    # The site already held THIS package: it is the customer's eSIM. Row
    # late['row'] becomes the sale; the live row(s) of the same number hold a
    # package nobody shows, or nothing at all.
    n = late["row"]
    hdr = [h.strip() for h in ws.row_values(1)]
    notes = []
    complete_row(ws, n, d, details)
    sale = {}
    live = list(late.get("live_rows") or [])
    if live:
        src = ws.row_values(live[0])
        for name in (H_SELL, H_SALE, H_PURCHASE):
            if name in hdr and hdr.index(name) < len(src) and src[hdr.index(name)].strip():
                sale[name] = src[hdr.index(name)].strip()
    src_note = f"from row {live[0]}" if sale else ""
    if not sale:
        # No live row to take the sale from (G4, 2026-10-21): the second
        # package's delivery mail was processed FIRST, its row was marked as
        # the parked spare, and by the time this one's mail arrived there was
        # no row left reading as the sale. The site's record of the order
        # still says what the customer paid, how, and at what list price.
        try:
            sale = sale_cells_from_facts(facts, ws.get_all_values(), hdr)
        except Exception as e:                      # a label, never the swap
            log.warning(f"order {oid}: could not rebuild the sale from the site: {_redact(str(e))}")
            sale = {}
        sale = {k: v for k, v in sale.items() if k in hdr}
        src_note = "from the site's record of the order"
    if sale:
        ws.update_cells([gspread.Cell(n, hdr.index(k) + 1, v) for k, v in sale.items()],
                        value_input_option="USER_ENTERED")
        notes.append(f"row {n}: eSIM filled in, Status active, "
                     + ", ".join(f"{k} '{v}'" for k, v in sale.items()) + f" ({src_note})")
    else:
        notes.append(f"row {n}: eSIM filled in, Status active - its Sell is NOT restored "
                     f"(no live row to take it from): type what the customer paid")
    if str(sale.get(H_PURCHASE, "")).strip().lower() in PAID_RAILS and _paint(ws, hdr, n, PAID_BG):
        notes.append(f"row {n}: Mail green")
    before = ws.row_values(n)
    if H_BUY in hdr and _is_zero_money(before[hdr.index(H_BUY)] if hdr.index(H_BUY) < len(before) else ""):
        notes.append(f"row {n}: Buy says 0, but this package WAS charged - put its cost back")
    for m in live:
        try:
            ch = supersede_row(ws, m, hdr)
            notes.append(f"row {m} is now the spare: " +
                         ("; ".join(f"{h}: '{b}' -> '{a}'" for h, b, a in ch) or "nothing changed"))
        except Exception as e:
            notes.append(f"row {m} NOT marked ({_redact(str(e))}) - mark it as the spare by hand")
    alert(f"Order {oid}: the 'replaced' purchase IS the customer's eSIM - rows swapped",
          head + f"The site already shows THIS package on {oid}'s page (same activation code), "
          "so it is the eSIM the customer has, and the other purchase of the same number is "
          "the one nobody is using.\n\nChanged:\n  * " + "\n  * ".join(notes) +
          "\n\nThe customer was not mailed again (they have had this eSIM since the site "
          "completed the order). The spare row's package: shelf, or a supplier refund.")
    inbox.flag(uid)
    log.info(f"order {oid}: late package is the customer's - row {n} is the sale, "
             f"row(s) {live or '-'} the spare")


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
    book = None
    mail_error = None
    try:
        inbox = _open_inbox()
        try:
            deliveries = inbox.unprocessed()
            log.info(f"{len(deliveries)} unprocessed delivery email(s)")
            if deliveries:
                book = sheet_client().open_by_key(RECEIPTS_SHEET_ID)
                ws = book.sheet1
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
            book = sheet_client().open_by_key(RECEIPTS_SHEET_ID)
            ws = book.sheet1
        deliver_pending_emails(ws)
    except Exception as e:
        log.error(f"delivery ledger sweep failed: {_redact(str(e))}")

    # Second packages onto the stock shelf (2026-10-21). After the mail, never
    # before it: a customer waiting for their eSIM comes first, a spare can
    # wait five minutes. One GET when there is nothing to shelve.
    try:
        import stock_shelf
        if book is not None:
            stock_shelf.shelve_extras(book, receipts_ws=ws, by="cloud:fulfillment")
    except Exception as e:
        log.error(f"stock shelf step failed: {_redact(str(e))}")

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
