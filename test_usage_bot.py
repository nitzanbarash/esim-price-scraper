#!/usr/bin/env python3
"""Tests for the daily usage sweep.

These exist because of a real outage: the supplier's providers disagree on a
date format, one of them sent a date with no timezone offset, and comparing it
raised TypeError out of the WHOLE run — every customer's meter froze for days
while the only symptom was an email among hundreds.

So the rule these tests enforce is not "the parser works". It is:

    NOTHING the supplier can put in a field may stop the sweep.

Every payload below is a shape the supplier has actually returned, kept
verbatim. When it invents a new one, add it here first and watch this fail.

Run:  python test_usage_bot.py
"""

import imaplib
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import usage_bot
from usage_bot import (ACTIVE, EXPIRED, USED_UP, _parse_expiry, _parse_usage_cell,
                       _plan_days, decide_status)
import fulfillment_bot

NOW = datetime(2026, 8, 2, 22, 0, tzinfo=timezone.utc)
GB = 1024 ** 3
_fails: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


def check_no_raise(name, fn):
    try:
        fn()
        print(f"  ok   {name}")
    except Exception as e:
        print(f"  FAIL {name} raised {type(e).__name__}: {e}")
        _fails.append(name)


# ── dates ────────────────────────────────────────────────────────────────────
# Every format seen in the wild. The second one caused the outage.
print("\n_parse_expiry — always aware, never raises")
REAL_DATES = [
    "2026-07-23T22:03:18+0000",      # provider 0
    "2026-08-31T09:43:27",           # provider 4 — NO OFFSET (the outage)
    "2026-07-30T17:27:53",           # provider 4
    "2026-07-29T15:36:59.223Z",      # lastUpdateTime style
    "2026-07-23T22:03:18.500+0000",
]
for s in REAL_DATES:
    got = _parse_expiry(s)
    check(f"{s} -> aware", got is not None and got.tzinfo is not None, True)

# Junk must be None, never an exception, and never a naive datetime.
for s in ["", None, "garbage", "0000-00-00", "2026-13-45T99:99:99", 12345, {}]:
    check_no_raise(f"junk {s!r} does not raise", lambda s=s: _parse_expiry(s))
    got = _parse_expiry(s) if not isinstance(s, (dict, int)) else None
    if got is not None:
        check(f"junk {s!r} is aware if parsed", got.tzinfo is not None, True)

# The exact comparison that blew up.
check_no_raise(
    "naive supplier date can be compared to now",
    lambda: NOW > _parse_expiry("2026-08-31T09:43:27") + timedelta(days=1),
)

# ── the retirement decision ──────────────────────────────────────────────────
print("\ndecide_status")
bought = NOW - timedelta(days=4)
old = NOW - timedelta(days=100)
cases = [
    ("no-offset expiry in the future -> active",
     {"used_gb": 0.0, "total_gb": 10, "expires": "2026-08-31T09:43:27", "status": "active"}, bought, 30, ACTIVE),
    ("no-offset expiry in the past -> finished",
     {"used_gb": 0.2, "total_gb": 1, "expires": "2026-07-30T17:27:53", "status": "active"}, bought, 1, EXPIRED),
    ("offset expiry in the past -> finished",
     {"used_gb": 0.076, "total_gb": 1, "expires": "2026-07-23T22:03:18+0000", "status": "used_expired"}, bought, 1, EXPIRED),
    ("consumed -> used up, whatever the date says",
     {"used_gb": 10.0, "total_gb": 10, "expires": "2026-08-31T09:43:27", "status": "active"}, bought, 30, USED_UP),
    ("no expiry at all, young -> active",
     {"used_gb": 0.0, "total_gb": 1, "expires": "", "status": "active"}, bought, 1, ACTIVE),
    ("no expiry at all, past the 90-day cap -> finished",
     {"used_gb": 0.2, "total_gb": 1, "expires": "", "status": "active"}, old, 1, EXPIRED),
    ("supplier says used_expired -> finished",
     {"used_gb": 0.2, "total_gb": 1, "expires": "", "status": "used_expired"}, bought, 1, EXPIRED),
    ("unknown to the supplier, still inside its validity -> active",
     None, bought, 30, ACTIVE),
    ("unknown to the supplier, past validity + grace -> finished",
     None, bought, 1, EXPIRED),
    ("unknown, no plan length, past the cap -> finished", None, old, None, EXPIRED),
    ("no purchase date -> never retired on age", None, None, None, ACTIVE),
]
for name, usage, at, days, want in cases:
    check(name, decide_status(usage, at, days, now=NOW), want)

# Whatever the supplier sends, deciding must not raise.
print("\ndecide_status — hostile input never raises")
for bad in [
    {"used_gb": 0, "total_gb": 1, "expires": "not-a-date", "status": "active"},
    {"used_gb": 0, "total_gb": 1, "expires": None, "status": None},
    {"used_gb": 0, "total_gb": 1},
]:
    check_no_raise(f"{bad}", lambda b=bad: decide_status(b, bought, 30, now=NOW))


# ── the provider sweep ───────────────────────────────────────────────────────
print("\nfetch_usage — asks every provider, keeps only real readings")

A, B, C = "8948010010076416899", "89852350225200102850", "8948010010076420065"


def fake_supplier(responses):
    """responses: {providerCode_or_None: [usage rows]}"""
    calls = []

    def _post(url, json=None, **kw):
        pc = (json or {}).get("providerCode")
        asked = list((json or {}).get("iccidList") or [])
        calls.append((pc, asked))
        rows = [r for r in responses.get(pc, []) if r["iccid"] in asked]
        return mock.Mock(status_code=200, raise_for_status=lambda: None,
                         json=lambda: {"success": True, "usage": rows})

    return _post, calls


def row(iccid, used_gb, total_gb, expires=None, status="active"):
    return {"iccid": iccid, "dataUsage": used_gb * GB, "totalData": total_gb * GB,
            "expiryDate": expires, "status": status, "remainingDays": None}


# The real split measured 2026-07-29: default answers for one, provider 4 the rest.
post, calls = fake_supplier({None: [row(B, 0.076, 1, "2026-07-23T22:03:18+0000")],
                             4: [row(A, 0, 1), row(C, 0, 1)]})
with mock.patch.object(fulfillment_bot.requests, "post", post):
    got = fulfillment_bot.fetch_usage([A, B, C])
check("all three resolved across providers", sorted(got), sorted([A, B, C]))
check("reading kept from the default provider", got[B]["used_gb"], 0.076)
check("provider recorded on the reading", got[A]["provider"], 4)
check("the default provider is asked first", calls[0][0], None)
check("only the unanswered are re-asked", sorted(calls[1][1]), sorted([A, C]))

# A provider that owns everything ends the sweep — no pointless extra calls.
post, calls = fake_supplier({None: [row(A, 0, 1), row(B, 0, 1), row(C, 0, 1)]})
with mock.patch.object(fulfillment_bot.requests, "post", post):
    fulfillment_bot.fetch_usage([A, B, C])
check("sweep stops once everyone is accounted for", len(calls), 1)

# Provider 3 answers with a zero-filled placeholder for ids it does not own.
# Recording it would show a customer "0 of 0 GB".
post, _ = fake_supplier({3: [row(A, 0, 0, status="unknown")], 4: [row(A, 0.5, 1)]})
with mock.patch.object(fulfillment_bot.requests, "post", post):
    got = fulfillment_bot.fetch_usage([A])
check("zero-total placeholder is discarded", got[A]["total_gb"], 1.0)

# A provider erroring must not lose the answers other providers gave.
def flaky(url, json=None, **kw):
    if (json or {}).get("providerCode") is None:
        raise RuntimeError("supplier down")
    return mock.Mock(status_code=200, raise_for_status=lambda: None,
                     json=lambda: {"usage": [row(A, 0.25, 1)]})


with mock.patch.object(fulfillment_bot.requests, "post", flaky):
    got = fulfillment_bot.fetch_usage([A])
check("one provider failing does not lose the others", got[A]["used_gb"], 0.25)

check("no eSIMs means no requests at all", fulfillment_bot.fetch_usage([]), {})


# ── reading the sheet's own cells ────────────────────────────────────────────
print("\nsheet parsing")
check("usage cell", _parse_usage_cell("0.44 / 1"), {"used_gb": 0.44, "total_gb": 1.0, "expires": None})
check("usage cell, no spaces", _parse_usage_cell("1.824/10"), {"used_gb": 1.824, "total_gb": 10.0, "expires": None})
for bad in ["", "  ", "nonsense", "1 / 0", "-1 / 5", "1 / "]:
    check(f"usage cell rejects {bad!r}", _parse_usage_cell(bad), None)
check("plan days", _plan_days("10GB - 30 days — LTE + 5G • Movistar Spain"), 30)
check("plan days, hebrew", _plan_days("1GB - 7 ימים"), 7)
check("plan days, absent", _plan_days("Cellcom"), None)


# ── the mailbox connection ───────────────────────────────────────────────────
# Also from a real outage: imaplib was built with no timeout, Gmail accepted
# the socket and then went quiet, and the run blocked until the 10-minute job
# limit killed it. Holding the concurrency slot that long got the dispatch
# queued behind it cancelled, so ONE stuck socket produced a stream of
# "all jobs have failed" mail. A hang must be impossible, not merely unlikely.
print("\nInbox — a silent server can never hang the run")

# getattr, not attribute access: if the constant is gone this must read as one
# clean FAIL, not an AttributeError that aborts the file and hides every check
# below it — the failure we are guarding against deserves a full picture.
_timeout = getattr(fulfillment_bot, "IMAP_TIMEOUT", None)
check("a timeout is set at all",
      isinstance(_timeout, (int, float)) and 0 < _timeout < 300, True)

os.environ.setdefault("GMAIL_APP_PASSWORD", "test-not-a-real-password")

seen = {}


def fake_imap(host, **kw):
    seen.update(kw)
    return mock.MagicMock()


with mock.patch.object(fulfillment_bot.imaplib, "IMAP4_SSL", fake_imap):
    fulfillment_bot.Inbox()
# The timeout rides on the socket, so login/search/fetch inherit it — which is
# only true if it is passed to the CONSTRUCTOR, not set afterwards. Assert it
# is a real positive number rather than comparing it to the constant: with the
# timeout missing, both sides would read None and the check would pass on a
# bot that hangs exactly as before.
_passed = seen.get("timeout")
check("the timeout reaches imaplib",
      isinstance(_passed, (int, float)) and _passed == _timeout, True)

# A blip is ridden out...
tries = []


def flaky_imap(host, **kw):
    tries.append(1)
    if len(tries) < 3:
        raise TimeoutError("gmail went quiet")
    return mock.MagicMock()


_open = getattr(fulfillment_bot, "_open_inbox", None)
check("main opens the inbox through the retrying helper",
      callable(_open) and "_open_inbox()" in open("fulfillment_bot.py").read(), True)

with mock.patch.object(fulfillment_bot.imaplib, "IMAP4_SSL", flaky_imap), \
     mock.patch.object(fulfillment_bot.time, "sleep", lambda s: None):
    if _open:
        _open()
check("a brief refusal is retried, not alerted", len(tries), 3)

# ...but a real outage still fails loudly rather than pretending it ran.
tries.clear()


def dead_imap(host, **kw):
    tries.append(1)
    raise TimeoutError("gmail is down")


raised = False
with mock.patch.object(fulfillment_bot.imaplib, "IMAP4_SSL", dead_imap), \
     mock.patch.object(fulfillment_bot.time, "sleep", lambda s: None):
    try:
        if _open:
            _open()
    except TimeoutError:
        raised = True
check("a sustained outage is not swallowed", raised, True)
check("retries are bounded", len(tries), 3)


# ── what reaches the buyer ───────────────────────────────────────────────────
# From a real complaint: "the package doesn't work". The supplier sent
# `"apn": null` — a plan that needs the APN field left BLANK — and .get(k,"")
# did not catch it, because the key IS there. str(None) made the literal
# "None", which is truthy, so it survived the "skip empty rows" filter and was
# mailed to the buyer as `APN  None`, reading like something to type in.
# Typing anything into an APN that must stay empty is what stops the data.
print("\nnothing the supplier omits may reach the buyer as a word")

_sup = getattr(fulfillment_bot, "_sup", None)
check("the parser has a null-safe reader", callable(_sup), True)
if _sup:
    for junk in [None, "None", "null", "NULL", "undefined", "n/a", "  none  ", ""]:
        check(f"{junk!r} reads as empty", _sup(junk), "")
    check("a real APN survives", _sup("internet.provider.com"), "internet.provider.com")
    check("a real ICCID survives", _sup(8948010010087222062), "8948010010087222062")

# The exact payload that caused the complaint, verbatim.
REAL = {"session": {"country_name": "Greece", "plan_data": "20GB", "plan_validity": "30",
                    "coverage": "LTE + 5G", "networks": "Vodafone Greece"},
        "esim": {"qr_code": "LPA:1$smdp.io$K2-XXXXXX", "iccid": "8948010010087222062",
                 "activation_code": "LPA:1$smdp.io$K2-XXXXXX", "apn": None,
                 "smdp_address": "smdp.io"}}

with mock.patch.object(fulfillment_bot.requests, "get",
                       lambda *a, **k: mock.Mock(status_code=200, raise_for_status=lambda: None,
                                                 json=lambda: REAL)):
    got = fulfillment_bot.fetch_esim_details("https://esim.dog/success?session_id=cs_live_x")
check("a null APN becomes empty, not 'None'", got.get("apn"), "")
check("the rest of the eSIM is still read", got.get("iccid"), "8948010010087222062")

# And the buyer's own email carries NO credentials at all since 2026-09-10 —
# only the order link (memory: esim-out-of-email). Catch the message at SMTP.
sent = []
class _SMTP:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self): pass
    def login(self, *a): pass
    def send_message(self, m): sent.append(m)
poisoned = {"activation_code": "LPA:1$smdp.io$K2-XXXXXX", "smdp": "smdp.io",
            "iccid": "8948010010087222062", "apn": "None"}
with mock.patch.object(fulfillment_bot.smtplib, "SMTP", _SMTP), \
     mock.patch.dict(fulfillment_bot.os.environ, {"GMAIL_APP_PASSWORD": "x"}):
    fulfillment_bot.send_customer_email("a@b.c", "WR-TEST01", "https://www.waverole.com/?order=abc",
                                        {"gb": 10, "days": 30, "location": "Greece"}, esim=poisoned)
    # The HTML part travels base64-encoded; decode it, or every "not in" check passes for free.
    raw = sent[-1].as_string() if sent else ""
    body = sent[-1].get_payload()[0].get_payload(decode=True).decode("utf-8") if sent else ""
    check("the email was sent", bool(sent), True)
    check("no activation code in the email", "LPA:1" in body or "K2-XXXXXX" in body, False)
    check("no SM-DP+, ICCID or APN in the email",
          "smdp.io" in body or "8948010010087222062" in body or "APN" in body, False)
    check("no QR attachment", "esim-qr-" in raw or "image/png" in raw, False)
    check("the order link is there", "order=abc" in body, True)
    try:
        fulfillment_bot.send_customer_email("a@b.c", "WR-TEST02", "", {"gb": 1, "days": 7}, esim=poisoned)
        check("no link → refused, not mailed empty", False, True)
    except ValueError:
        check("no link → refused, not mailed empty", True, True)


# ── the supplier's batch cap ─────────────────────────────────────────────────
# The eleventh live package broke the sweep for everyone. The supplier answers
#     {"error":"Maximum 10 eSIMs can be queried at once."}
# with a 400 that rejects the WHOLE request, not the excess — so the day a
# eleventh eSIM was sold, every meter froze and stayed frozen. A customer
# burned a full 10GB during the outage and got no 90%, 98% or "finished"
# email, because all of them hang off this one call. This is a growth cliff:
# it cannot be allowed to depend on how many customers we happen to have.
print("\nfetch_usage — never asks for more than the supplier allows")

check("the cap is declared", getattr(fulfillment_bot, "USAGE_BATCH", None), 10)

MANY = [f"894801001008{n:07d}" for n in range(23)]


def capped_supplier(cap=10):
    """The real endpoint: over the cap it 400s the entire request."""
    sizes = []

    def _post(url, json=None, **kw):
        asked = list((json or {}).get("iccidList") or [])
        sizes.append(len(asked))
        if len(asked) > cap:
            raise RuntimeError("400 Client Error: Bad Request")
        return mock.Mock(status_code=200, raise_for_status=lambda: None,
                         json=lambda: {"usage": [row(i, 0.5, 10) for i in asked]})

    return _post, sizes


post, sizes = capped_supplier()
with mock.patch.object(fulfillment_bot.requests, "post", post):
    got = fulfillment_bot.fetch_usage(MANY)
check("every one of 23 is resolved", len(got), 23)
check("no request exceeded the cap", max(sizes) <= 10, True)
check("split into the fewest calls", sizes[:3], [10, 10, 3])

# A fleet at exactly the cap must still be one call, not two.
post, sizes = capped_supplier()
with mock.patch.object(fulfillment_bot.requests, "post", post):
    fulfillment_bot.fetch_usage(MANY[:10])
check("exactly ten is a single call", sizes, [10])

# One rejected chunk must not cost the others their readings.
def one_bad_chunk(url, json=None, **kw):
    asked = list((json or {}).get("iccidList") or [])
    if MANY[0] in asked:
        raise RuntimeError("supplier hiccup")
    return mock.Mock(status_code=200, raise_for_status=lambda: None,
                     json=lambda: {"usage": [row(i, 0.5, 10) for i in asked]})


with mock.patch.object(fulfillment_bot.requests, "post", one_bad_chunk):
    got = fulfillment_bot.fetch_usage(MANY)
check("a failed chunk only costs its own rows", len(got), 13)


# ── the mailbox read ─────────────────────────────────────────────────────────
# The fulfillment job runs every 5 minutes forever. It used to spend one IMAP
# round-trip per message per run on a mailbox nobody prunes, which is why a job
# with seconds of work in it drifted to half a minute and, on 2026-08-11,
# finally crossed the 60s socket timeout and failed the run.
print("\nmailbox — one round-trip per batch, matched by UID")


class FakeBox:
    """Enough IMAP to answer unprocessed(). Sequence numbers deliberately do
    NOT equal UIDs and headers come back in a different order from the one
    asked for — both are true of the real server, and both are invisible until
    a bot pairs one buyer's email with another's."""

    def __init__(self, msgs, fail_first_chunk=False):
        self.msgs = msgs                    # {uid: (subject, from_addr)}
        self.header_calls: list[list] = []
        self.full_fetches: list[str] = []
        self.fail_first_chunk = fail_first_chunk

    def uid(self, cmd, *args):
        if cmd == "search":
            return "OK", [" ".join(self.msgs).encode()]
        asked, items = args[0], args[1]
        if "HEADER.FIELDS" in items:
            uids = asked.split(",")
            self.header_calls.append(uids)
            if self.fail_first_chunk and len(self.header_calls) == 1:
                return "NO", [None]
            out = []
            for seq, uid in enumerate(reversed(uids), start=1000):
                subject, frm = self.msgs[uid]
                head = f"Subject: {subject}\r\nFrom: {frm}\r\n\r\n".encode()
                out.append((f"{seq} FETCH (UID {uid} BODY[HEADER.FIELDS "
                            f"(SUBJECT FROM)] {{{len(head)}}}".encode(), head))
                out.append(b")")
            return "OK", out
        self.full_fetches.append(asked)
        subject, frm = self.msgs[asked]
        return "OK", [(b"1 FETCH (RFC822 {0}",
                       f"Subject: {subject}\r\nFrom: {frm}\r\n\r\nbody\r\n".encode())]


def make_inbox(msgs, **kw):
    inbox = object.__new__(fulfillment_bot.Inbox)     # no socket, no login
    inbox.box = FakeBox(msgs, **kw)
    return inbox


DELIVERY = fulfillment_bot.DELIVERY_SUBJECT
# 250 messages: 248 unrelated (the mailbox nobody prunes) and 2 real ones.
NOISE = {str(1000 + i): (f"GitHub Actions run {i} failed", "notify@github.com")
         for i in range(248)}
REAL = {"1500": (f"{DELIVERY} to use!", "orders@updates.esim.dog"),
        # Forwarded by hand from the owner's own address — a FROM filter in the
        # IMAP query would drop this, which is why the filtering is in Python.
        "1501": (f"Fwd: {DELIVERY} to use!", fulfillment_bot.ALERTS_EMAIL)}
# The bot's OWN outgoing customer email: same subject, sitting in All Mail.
MINE = {"1502": (f"{DELIVERY} to use!", fulfillment_bot.GMAIL_USER)}
ALL = {**NOISE, **REAL, **MINE}

with mock.patch.object(fulfillment_bot, "parse_delivery",
                       lambda uid, msg: {"uid": uid}):
    inbox = make_inbox(ALL)
    got = inbox.unprocessed()
    calls = inbox.box.header_calls
    check("251 messages cost 2 header round-trips, not 251", len(calls), 2)
    check("batches respect HEADER_BATCH", [len(c) for c in calls], [200, 51])
    check("every message was asked about", sum(len(c) for c in calls), 251)
    # The forwarded copy comes FROM the owner, so a From-based skip would eat a
    # real delivery; only the bot's own outgoing mail may be skipped.
    check("both real deliveries found, self-sent skipped",
          sorted(d["uid"] for d in got), ["1500", "1501"])
    check("only the real ones cost a full fetch",
          sorted(inbox.box.full_fetches), ["1500", "1501"])

    # If headers were paired by position the reversed order above would hand
    # message 1500's subject to some unrelated GitHub notification.
    inbox = make_inbox({**NOISE, **REAL})
    check("headers matched by UID, not arrival order",
          sorted(d["uid"] for d in inbox.unprocessed()), ["1500", "1501"])

    # A refused chunk must cost only its own messages — nothing is flagged
    # until it is processed, so the next run picks them up untouched.
    inbox = make_inbox(ALL, fail_first_chunk=True)
    got = inbox.unprocessed()
    check("a refused header chunk still returns the rest",
          sorted(d["uid"] for d in got), ["1500", "1501"])
    check_no_raise("a refused header chunk does not raise",
                   lambda: make_inbox(ALL, fail_first_chunk=True).unprocessed())

    check_no_raise("an empty mailbox is fine", lambda: make_inbox({}).unprocessed())


# ── what a failure is allowed to cost ────────────────────────────────────────
# The ledger sweep is the promise that every buyer eventually gets their eSIM.
# It used to sit downstream of the mailbox read, so a Gmail blip cancelled the
# retry for customers whose email had nothing to do with the mailbox.
print("\nmain() — a blip must not cancel the delivery safety net")


def run_main(mail_error):
    swept, alerts = [], []
    with mock.patch.object(fulfillment_bot, "_open_inbox",
                           mock.Mock(side_effect=mail_error)), \
         mock.patch.object(fulfillment_bot, "sheet_client", mock.Mock()), \
         mock.patch.object(fulfillment_bot, "deliver_pending_emails",
                           lambda ws: swept.append(True)), \
         mock.patch.object(fulfillment_bot, "alert",
                           lambda s, b: alerts.append(s)):
        try:
            fulfillment_bot.main()
            code = 0
        except SystemExit as e:
            code = e.code
    return code, swept, alerts


for label, err in [("a socket timeout", TimeoutError("timed out")),
                   ("a refused connection", ConnectionResetError("reset by peer")),
                   ("an IMAP abort", imaplib.IMAP4.abort("server said no"))]:
    code, swept, alerts = run_main(err)
    check(f"{label}: the sweep still runs", swept, [True])
    check(f"{label}: the run stays green", code, 0)
    check(f"{label}: no email is sent", alerts, [])

for label, err in [
        ("a rejected password",
         imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")),
        ("a missing secret", RuntimeError("Missing env/secret: GMAIL_APP_PASSWORD"))]:
    code, swept, alerts = run_main(err)
    check(f"{label}: fails the run loudly", code, 1)
    check(f"{label}: emails the owner", len(alerts), 1)
    check(f"{label}: the sweep STILL runs first", swept, [True])


print("\n" + ("=" * 60))
# ── the receipt columns the owner added 2026-08-29 ──────────────────────────
# A discount, an activation flag and a status colour all describe the same
# customer, and each of them was getting written from a source that could lie:
# today's list price for a sale made weeks ago, a supplier that goes quiet on
# spent packages, and a colour painted by a bot that runs once a day.

print("\n-- discount: only a giveaway is unambiguous --")
_prices = {"2.49.20": 6.99, "3.52.10": 16.00}
check("a free package is 100%, with the figure",
      usage_bot.discount_text("2.49.20", "0", _prices), "100% ($6.99 הנחה)")
check("'0$' is the same as '0'",
      usage_bot.discount_text("2.49.20", "0$", _prices), "100% ($6.99 הנחה)")
check("free, but the SKU has no list price: no invented figure",
      usage_bot.discount_text("GLOBAL-1GB", "0", _prices), "100%")
# 'מחיר סופי' is rewritten daily. Eleven real rows sold at $6.00 against a
# $6.49 list would otherwise have been stamped "8% off" — drift, not a discount.
check("sold slightly under today's list: left alone",
      usage_bot.discount_text("2.49.20", "6.00", _prices), "")
# The case the column was missing: an ordinary full-price sale. "-" is a
# statement ("we looked, no discount"); blank is the absence of one.
check("sold at full price: says so",
      usage_bot.discount_text("2.49.20", "6.99", _prices), "-")
check("sold above today's list (scraper marked it down since): still no discount",
      usage_bot.discount_text("2.49.20", "7.50", _prices), "-")
check("SKU absent from the price sheet: nothing to measure, stays blank",
      usage_bot.discount_text("NOSUCH-SKU", "6.99", _prices), "")
check("no price recorded yet: left alone",
      usage_bot.discount_text("2.49.20", "", _prices), "")

print("\n-- activation is one-way --")
check("data moved, so it was installed",
      usage_bot.activation_text({"used_gb": 0.5}, "no"), "Activated")
check("bought but never touched",
      usage_bot.activation_text({"used_gb": 0}, "no"), "no")
# The supplier stops answering for spent packages, which is exactly when a
# naive reading would rewrite a real customer's history back to 'no'.
check("supplier went quiet: stays Activated",
      usage_bot.activation_text(None, "Activated"), "Activated")
check("meter reads zero again: stays Activated",
      usage_bot.activation_text({"used_gb": 0}, "Activated"), "Activated")
check("never seen and no reading: still no",
      usage_bot.activation_text(None, "no"), "no")
# A spent package: the supplier stops answering, but the sheet still holds the
# last real reading. Ignoring it stamped 'no' on packages that plainly ran.
check("supplier quiet, but the sheet remembers data moved",
      usage_bot.activation_text(None, "", {"used_gb": 18.0, "total_gb": 20.0}),
      "Activated")
check("supplier quiet and the sheet reads zero: no",
      usage_bot.activation_text(None, "", {"used_gb": 0.0, "total_gb": 20.0}),
      "no")

print("\n-- a fault the owner wrote must survive the sweep --")
# STILL_CHECK is what protects it: the row is never revisited, so a package
# that later finishes cannot overwrite the reason a person put there.
check("FAULT is not swept", usage_bot.FAULT in usage_bot.STILL_CHECK, False)
check("finished is not swept", usage_bot.EXPIRED in usage_bot.STILL_CHECK, False)
check("active still is", usage_bot.ACTIVE in usage_bot.STILL_CHECK, True)
check("unchecked still is", "" in usage_bot.STILL_CHECK, True)

print("\n-- colour rules address the right column past Z --")
check("column letters", [usage_bot._a1_col(i) for i in (0, 18, 25, 26, 27)],
      ["A", "S", "Z", "AA", "AB"])


# ═════════════════════════════════════════════════════════════════════════════
# Two suppliers in one sweep
#
# The danger is asymmetric and worth naming. Reading a Stellar package WRONG
# gives a customer a wrong meter. Failing to read one, and letting that count
# as "the supplier never heard of it", RETIRES a package the customer is still
# using — the meter freezes, the order page says finished, and nobody is told.
# So every test below is really the same test: an answer we do not understand
# must leave the row exactly as it was.
# ═════════════════════════════════════════════════════════════════════════════

import stellar_usage
from stellar_usage import Unknown, map_usage, to_gb

print("\n-- which supplier a row belongs to --")
check("Route proves Stellar",
      usage_bot.decide_source("Stellar", "Stellar JC059"), ("Stellar", "ok"))
check("Route proves esim.dog",
      usage_bot.decide_source("esim.dog", "Cellcom"), ("esim.dog", "ok"))
check("no Route at all is an esim.dog row",
      usage_bot.decide_source("esim.dog", ""), ("esim.dog", "ok"))
# The column is being back-filled by hand right now: most rows are still blank.
check("blank source is healed from Route (Stellar)",
      usage_bot.decide_source("", "Stellar JC059"), ("Stellar", "heal"))
check("blank source is healed from Route (esim.dog)",
      usage_bot.decide_source("", "Pelephone"), ("esim.dog", "heal"))
check("blank source, blank Route: esim.dog, the only supplier we had",
      usage_bot.decide_source("", ""), ("esim.dog", "heal"))
check("a row typed before the dropdown existed still agrees",
      usage_bot.decide_source("  ESIM.DOG  ", "Cellcom"), ("esim.dog", "ok"))
# The mis-click. Neither column is believed — asking esim.dog about a Stellar
# package gets silence, and silence retires the package.
check("source says esim.dog, Route says Stellar",
      usage_bot.decide_source("esim.dog", "Stellar JC059"), ("Stellar", "mismatch"))
check("source says Stellar, Route says otherwise",
      usage_bot.decide_source("Stellar", "Cellcom"), ("esim.dog", "mismatch"))
check("'Stellarium' is not the prefix",
      usage_bot.decide_source("esim.dog", "Stellarium")[1], "mismatch")

# Until today this column held the payment RAIL. Hundreds of rows still do.
# Such a word names no supplier, so it cannot disagree with one: it is an
# un-migrated row, healed from Route exactly like a blank. Read as a mismatch
# it would have skipped — and mailed — every package sold before 2026-09-10.
for legacy in ("bot - manually", "PayPal", "payme", "iCount", "Manual",
               "  BOT  -  MANUALLY  "):
    check(f"legacy payment word {legacy!r} is healed, not a mismatch",
          usage_bot.decide_source(legacy, "Cellcom"), ("esim.dog", "heal"))
check("a legacy word on a Stellar row is healed to Stellar",
      usage_bot.decide_source("paypal", "Stellar JC059"), ("Stellar", "heal"))
check("a supplier name that disagrees is still a mismatch",
      usage_bot.decide_source("esim.dog", "Stellar JC059")[1], "mismatch")

print("\n-- units: the megabyte/byte trap --")
# 'megabytes' contains the letters of 'bytes'. Read as bytes, a 5GB package is
# 5 millionths of a gigabyte — instantly "used up", instantly retired.
check("megabytes", to_gb(5120, "data.megabytes"), 5.0)
check("used_mb", to_gb(1024, "used_mb"), 1.0)
check("used_bytes", to_gb(3 * GB, "usage.used_bytes"), 3.0)
check("total_gb stays as it is", to_gb(5, "total_gb"), 5.0)
check("a key naming no unit is megabytes", to_gb(2048, "data_used"), 2.0)

print("\n-- the mapper: two shapes it knows --")
SHAPE_MB = {
    "sim_id": "sim_a", "status": "active",
    "data": {"megabytes": 5120},
    "data_used": 1024,
    "expires_at": "2026-10-01T09:00:00+0000",
    "installation": {"activation_code": "LPA:1$smdp.example$SECRET"},
}
got = map_usage(SHAPE_MB)
check("megabyte shape reads", isinstance(got, dict), True)
check("used", got["used_gb"], 1.0)
check("total", got["total_gb"], 5.0)
check("expiry", got["expires"], "2026-10-01T09:00:00+0000")
check("status", got["status"], "active")

SHAPE_BYTES = {
    "sim_id": "sim_b", "esim_status": "expired",
    "usage": {"used_bytes": 3 * GB, "total_bytes": 5 * GB},
    "expiry_date": "2026-09-01T00:00:00Z",
}
got = map_usage(SHAPE_BYTES)
check("byte shape reads", isinstance(got, dict), True)
check("used", got["used_gb"], 3.0)
check("total", got["total_gb"], 5.0)
check("expiry", got["expires"], "2026-09-01T00:00:00Z")
check("status", got["status"], "expired")

# Straight through decide_status, which is the only reason any of this is read.
check("a spent Stellar package retires",
      decide_status(map_usage({"data": {"megabytes": 1024}, "data_used": 1024}),
                    None, 30, now=NOW), USED_UP)
check("a half-used one keeps being checked",
      decide_status(map_usage(SHAPE_MB), None, 30,
                    now=datetime(2026, 9, 10, tzinfo=timezone.utc)), ACTIVE)

print("\n-- the mapper: a shape it does NOT know --")
# The real /esims payload before an order has moved any data — this is what
# stellar_buyer sees today, and it says nothing about consumption.
SHAPE_UNKNOWN = {"sim_id": "sim_c", "plan_id": "p1",
                 "installation": {"activation_code": "LPA:1$x$y", "apn": "internet"}}
got = map_usage(SHAPE_UNKNOWN)
check("unknown shape is Unknown, not a reading", isinstance(got, Unknown), True)
check("and it hands back the key NAMES to fix it with", got.keys,
      ["installation", "installation.activation_code", "installation.apn",
       "plan_id", "sim_id"])
check("an Unknown is never mistaken for a reading", isinstance(got, dict), False)
check("half a shape is still unknown",
      isinstance(map_usage({"data_used": 500}), Unknown), True)
check("a size of zero is not a reading (it would retire a live package)",
      isinstance(map_usage({"data": {"megabytes": 0}, "data_used": 0}), Unknown), True)

# Money is not consumption. An invoice line called 'total_mb' outranks
# 'megabytes' by name alone, and a meter measured in euros is worse than none.
PRICED = {"price": {"total_mb": 99, "used_mb": 12},
          "data": {"megabytes": 2048}, "data_used": 512}
got = map_usage(PRICED)
check("the priced fields are ignored", (got["used_gb"], got["total_gb"]), (0.5, 2.0))

# The envelope. An eSIM carries its OWN 'data' block, so unwrapping every dict
# with a 'data' key would throw the whole payload away.
check("the {data: ...} envelope is unwrapped",
      map_usage({"data": {"sim_id": "s", "data": {"megabytes": 1024},
                          "data_used": 512}})["total_gb"], 1.0)
check("an eSIM's own data block is NOT unwrapped",
      map_usage(SHAPE_MB)["total_gb"], 5.0)

print("\n-- the portal link on the row --")
for raw, want in [
    ("https://wholesale.stellarsecurity.com/orders/71fa53ce-6dad", "71fa53ce-6dad"),
    ("https://wholesale.stellarsecurity.com/orders/71fa53ce-6dad/", "71fa53ce-6dad"),
    ("https://wholesale.stellarsecurity.com/orders/71fa/?tab=esims", "71fa"),
    ("https://wholesale.stellarsecurity.com/orders/71fa#top", "71fa"),
    ("71fa53ce-6dad-435f", "71fa53ce-6dad-435f"),
    ("", ""), ("https://esim.dog/success?session_id=abc", ""),
]:
    check(f"order id from {raw!r}", stellar_usage.order_id_from_url(raw), want)

print("\n-- nothing Stellar does may reach the sweep as an exception --")
URL = "https://wholesale.stellarsecurity.com/orders/o1"


class _Resp:
    def __init__(self, body, code=200):
        self._b, self.status_code = body, code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._b


class _Session:
    """Just enough of requests.Session, so the module's own auth header code
    runs. The key never leaves this object."""

    def __init__(self, handler):
        self.headers, self.get = {}, handler


check_no_raise("the whole call dies", lambda: stellar_usage.fetch_usage(
    [URL], session=_Session(mock.Mock(side_effect=OSError("no route to host"))), key="k"))
check("a dead network is None, never a reading",
      stellar_usage.fetch_usage(
          [URL], session=_Session(mock.Mock(side_effect=OSError("boom"))), key="k"),
      {URL: None})
check("a 500 on the order is None",
      stellar_usage.fetch_usage(
          [URL], session=_Session(lambda u, **k: _Resp({}, 502)), key="k"),
      {URL: None})
check("junk instead of JSON is None, not a crash",
      stellar_usage.fetch_usage(
          [URL], session=_Session(lambda u, **k: _Resp("<html>maintenance</html>")),
          key="k"),
      {URL: None})


def _two_step(order_body, esim_body, esim_code=200):
    def get(url, **kw):
        if "/orders/" in url:
            return _Resp({"data": order_body})
        return _Resp({"data": esim_body}, esim_code)
    return _Session(get)


check("a Stellar package is read end to end",
      stellar_usage.fetch_usage(
          [URL], session=_two_step({"esims": [{"sim_id": "sim_a"}]}, SHAPE_MB),
          key="k")[URL]["total_gb"], 5.0)
check("an order Stellar names no eSIM for is None",
      stellar_usage.fetch_usage([URL], session=_two_step({"esims": []}, {}),
                                key="k"), {URL: None})
check("a 500 on the eSIM leaves the row untouched, not retired",
      stellar_usage.fetch_usage(
          [URL], session=_two_step({"esims": [{"sim_id": "s"}]}, {}, 500),
          key="k"), {URL: None})
res = stellar_usage.fetch_usage(
    [URL], session=_two_step({"esims": [{"sim_id": "sim_c"}]}, SHAPE_UNKNOWN),
    key="k")[URL]
check("an unreadable shape comes back as Unknown", isinstance(res, Unknown), True)
# The PC copy of this repo has no Stellar key, on purpose (memory:
# stellar-key-placement). That is a quiet no-op, not a failure.
with mock.patch.dict(os.environ, {"STELLAR_API_KEY": ""}):
    check("no key: every row untouched, nothing raised",
          stellar_usage.fetch_usage([URL]), {URL: None})
check("nothing asked, nothing answered", stellar_usage.fetch_usage([]), {})


print("\n-- one sweep, two suppliers: what actually lands in the sheet --")

U_HEAD = ["תאריך - Date", 'מק"ט - SUK', "מס׳ הזמנה", "מס סידורי -ICCID", "QR",
          "Link - esim.dog", "Link - waverole", "חבילה - Plan", "Route",
          "סטטוס - Status", "GB (0/X) - ניצול", "מקור - source",
          "הופעל - Activated", "הנחה - Sale", "מכירה - Sell"]
UC = {name: i for i, name in enumerate(U_HEAD)}
O2 = "https://wholesale.stellarsecurity.com/orders/o2"
O3 = "https://wholesale.stellarsecurity.com/orders/o3"
O4 = "https://wholesale.stellarsecurity.com/orders/o4"


OLD_DATE = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%d/%m/%Y %H:%M")


def u_row(order, iccid="", link="", route="", source="", date="01/09/2026 10:00",
          status=""):
    r = [""] * len(U_HEAD)
    r[UC["תאריך - Date"]] = date
    r[UC["סטטוס - Status"]] = status
    r[UC['מק"ט - SUK']] = "2.49.10"
    r[UC["מס׳ הזמנה"]] = order
    r[UC["מס סידורי -ICCID"]] = iccid
    r[UC["Link - esim.dog"]] = link
    r[UC["Link - waverole"]] = "https://www.waverole.com/o/" + order
    r[UC["חבילה - Plan"]] = "5GB - 30 days"
    r[UC["Route"]] = route
    r[UC["מקור - source"]] = source
    r[UC["הופעל - Activated"]] = "no"
    r[UC["הנחה - Sale"]] = "-"
    r[UC["מכירה - Sell"]] = "6.99"
    return r


U_ROWS = [
    U_HEAD,
    u_row("WR-1", iccid="8972011", link="https://esim.dog/x", route="Cellcom",
          source="esim.dog"),                                   # row 2
    u_row("WR-2", link=O2, route="Stellar JC059", source="Stellar"),   # row 3
    u_row("WR-3", link=O3, route="Stellar JC060", source=""),          # row 4
    u_row("WR-4", link="", route="Stellar JC061", source="esim.dog"),  # row 5
    u_row("WR-5", link="", route="Stellar JC062", source="Stellar"),   # row 6
    # The one that used to go dark. A live 30-day package sold 35 days ago,
    # still running, on a run where Stellar could not be reached at all — no
    # key, a timeout, a 502, an order it names no eSIM for. All of those are a
    # per-row None, and None used to fall into decide_status, whose "the
    # supplier never heard of it" timer expired four days ago.
    u_row("WR-6", link=O4, route="Stellar JC063", source="Stellar",
          date=OLD_DATE, status=usage_bot.ACTIVE),                     # row 7
    # An un-migrated row: 'מקור' still holds the payment rail it held for a
    # year. Not a mismatch — a row waiting to be told which supplier it is.
    u_row("WR-7", iccid="8972099", link="https://esim.dog/y", route="Pelephone",
          source="bot - manually"),                                    # row 8
]


class _WS:
    def __init__(self, rows):
        self._rows, self.written, self.id = rows, [], 0
        self.sheet1, self.spreadsheet = self, mock.MagicMock()

    def open_by_key(self, key):
        return self

    def get_all_values(self):
        return self._rows

    def update_cells(self, cells, **kw):
        self.written.extend(cells)


ws = _WS(U_ROWS)
alerts, pushed = [], []
with mock.patch.object(usage_bot, "sheet_client", lambda: ws), \
     mock.patch.object(usage_bot, "ensure_status_colours", lambda *a: None), \
     mock.patch.object(usage_bot, "list_prices", dict), \
     mock.patch.object(usage_bot, "push_to_site",
                       lambda items: pushed.extend(items) or len(items)), \
     mock.patch.object(usage_bot, "alert", lambda su, bo: alerts.append((su, bo))), \
     mock.patch.object(usage_bot, "fetch_usage", lambda ic: {
         "8972011": {"used_gb": 0.5, "total_gb": 5.0, "expires": "", "status": ""},
         "8972099": {"used_gb": 2.0, "total_gb": 5.0, "expires": "", "status": ""}}), \
     mock.patch.object(usage_bot.stellar_usage, "fetch_usage", lambda urls: {
         O2: Unknown(keys=["installation.activation_code", "sim_id"]),
         O3: {"used_gb": 1.0, "total_gb": 5.0, "expires": "", "status": ""},
         O4: None}):
    rc = usage_bot.main()

written = {(c.row, c.col): c.value for c in ws.written}
check("the run finished", rc, 0)

# The mismatch: one mail, not one per row, and the row itself is untouched.
check("exactly one alert", len(alerts), 1)
check("and it names the two columns", "מקור vs Route" in alerts[0][0], True)
check("naming the order", "WR-4" in alerts[0][1], True)
check("the mismatched row is not written to",
      [k for k in written if k[0] == 5], [])

# The Unknown row. THIS is the one that matters: not retired, not metered,
# not stamped — a live package Stellar described in words we do not know yet.
check("the unreadable Stellar row is not written to",
      [k for k in written if k[0] == 3], [])
check("and its customer's meter is not touched either",
      [i for i in pushed if i["order_id"] == "WR-2"], [])

# The readable one, whose source column was blank and is now filled from Route.
check("the blank source cell is healed",
      written.get((4, UC["מקור - source"] + 1)), "Stellar")
check("its meter is written", written.get((4, UC["GB (0/X) - ניצול"] + 1)), "1 / 5")
check("it is still running", written.get((4, UC["סטטוס - Status"] + 1)),
      usage_bot.ACTIVE)
check("and the customer's page is told",
      [i["used_gb"] for i in pushed if i["order_id"] == "WR-3"], [1.0])

# No portal link, and no ICCID exists for a Stellar package. "We cannot ask"
# is not "it is finished": the row keeps its place in the sweep.
check("a Stellar row with no link is not written to",
      [k for k in written if k[0] == 6], [])

# The unreachable Stellar row, 35 days into a 30-day package. Nothing is
# written to it AT ALL — and the status it keeps is the live one.
check("a Stellar row Stellar did not answer for is not written to",
      [k for k in written if k[0] == 7], [])
check("so it is still running, four days past a timer that never applied to it",
      written.get((7, UC["סטטוס - Status"] + 1), U_ROWS[6][UC["סטטוס - Status"]]),
      usage_bot.ACTIVE)
check("and its meter is not touched either",
      [i for i in pushed if i["order_id"] == "WR-6"], [])

# The un-migrated row: healed to the supplier its Route proves, and metered
# normally in the same run.
check("the payment word is replaced by the supplier",
      written.get((8, UC["מקור - source"] + 1)), "esim.dog")
check("and the row was swept, not skipped",
      written.get((8, UC["GB (0/X) - ניצול"] + 1)), "2 / 5")
check("still exactly one alert — a legacy word is not a fault", len(alerts), 1)

# esim.dog is unaffected by any of it.
check("the esim.dog row still reads normally",
      written.get((2, UC["GB (0/X) - ניצול"] + 1)), "0.5 / 5")
check("activation followed the data",
      written.get((2, UC["הופעל - Activated"] + 1)), "Activated")


print("\n-- a mis-edited dropdown must not arrive as a wall of order numbers --")
# One bad drag down the 'מקור' column mismatches the whole sheet. The COUNT is
# what a person acts on; a mail naming 300 orders is a mail nobody opens.
MANY = [U_HEAD] + [u_row(f"WR-M{i}", link=O2, route="Stellar JC0%02d" % i,
                         source="esim.dog") for i in range(33)]
ws2 = _WS(MANY)
alerts2 = []
with mock.patch.object(usage_bot, "sheet_client", lambda: ws2), \
     mock.patch.object(usage_bot, "ensure_status_colours", lambda *a: None), \
     mock.patch.object(usage_bot, "list_prices", dict), \
     mock.patch.object(usage_bot, "push_to_site", lambda items: 0), \
     mock.patch.object(usage_bot, "alert", lambda su, bo: alerts2.append((su, bo))), \
     mock.patch.object(usage_bot, "fetch_usage", lambda ic: {}), \
     mock.patch.object(usage_bot.stellar_usage, "fetch_usage", lambda urls: {}):
    check("the run finished", usage_bot.main(), 0)
body = alerts2[0][1]
check("one mail for the lot", len(alerts2), 1)
check("it states the true count", "33 receipts row(s)" in body, True)
check("but names at most 20", body.count("  · WR-M"), 20)
check("and says how many it did not name", "and 13 more" in body, True)
check("nothing was written to any of them", ws2.written, [])


if _fails:
    print(f"{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("all usage-bot tests passed")
sys.exit(0)
