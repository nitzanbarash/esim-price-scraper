"""What the job scripts share with the cloud buying bot, without the buying.

usage_bot.py and receipts_colors.py used to import these from
fulfillment_bot.py. That file also reaches the buyers (stellar_buyer,
stock_shelf), so a release of the update network for the PCs could not ship
the two jobs without dragging buying code along, and its builder refuses that
(waverole-net net/build.py, NEVER list). The helpers moved here unchanged on
2026-10-22; fulfillment_bot re-exports every name, so `fb.<name>` in
stellar_buyer and stock_shelf, and the tests, keep working.

Nothing here may import fulfillment_bot, stellar_buyer, stock_shelf,
create_order or the finance bot.
"""

import logging
import os
import re
import smtplib
import tempfile
from datetime import datetime
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import gspread
import requests

import esimdog_guard

# The same logger name as before the move, so the lines these helpers write
# read exactly as they did in every job's log.
log = logging.getLogger("fulfillment")

# ── constants ──────────────────────────────────────────────────────────────────

GMAIL_USER = os.getenv("GMAIL_USER", "waverolesupply@gmail.com")
ALERTS_EMAIL = os.getenv("ALERTS_EMAIL", "uper.request@gmail.com")
ALERT_PREFIX = "[fulfillment bot] "
RECEIPTS_SHEET_ID = "1bWH_Zef0aNwZjLOR07hjJRZRXkrY73mX0aMLGPH6uao"
ORDERS_URL = "https://www.waverole.com/api/orders"
TZ = ZoneInfo("Asia/Jerusalem")


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


# ── alerts ─────────────────────────────────────────────────────────────────

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
        if esimdog_guard.paused():
            # s1 refused this computer (esimdog_guard): not one more call.
            break
        chunk = iccids[start:start + USAGE_BATCH]
        payload = {"iccidList": chunk}
        if provider is not None:
            payload["providerCode"] = provider
        try:
            r = requests.post(SUPPLIER_USAGE_URL, json=payload,
                              headers={"Accept": "application/json",
                                       "User-Agent": "Mozilla/5.0"}, timeout=60)
            if esimdog_guard.is_refusal(r.status_code):
                info = esimdog_guard.refused(f"HTTP {r.status_code} on the usage endpoint")
                esimdog_guard.notify(info, "a usage read")
                break
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
    if left := esimdog_guard.paused():
        log.info(f"s1 paused on this computer for {left / 60:.0f} more min - usage not asked")
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
    if esimdog_guard.paused():
        return {}
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


def is_replaced_status(status) -> bool:
    """A receipts Status that says this row was superseded by another one."""
    return str(status or "").strip().startswith(REPLACED_STATUS)


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
