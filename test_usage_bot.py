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

# And the buyer's own email must not carry the row at all.
html = fulfillment_bot._esim_copy_html(got, heb=True)
check("no APN row in the email", "APN" in html, False)
check("the codes the buyer needs are still there", "smdp.io" in html, True)

# Same guard when the record comes back from the SITE, already poisoned.
poisoned = {"activation_code": "LPA:1$smdp.io$K2-XXXXXX", "smdp": "smdp.io",
            "iccid": "8948010010087222062", "apn": "None"}
check("a stored 'None' is not mailed either",
      "APN" in fulfillment_bot._esim_copy_html(poisoned, heb=True), False)


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


print("\n" + ("=" * 60))
if _fails:
    print(f"{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("all usage-bot tests passed")
sys.exit(0)
