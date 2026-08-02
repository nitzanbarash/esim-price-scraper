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


print("\n" + ("=" * 60))
if _fails:
    print(f"{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("all usage-bot tests passed")
sys.exit(0)
