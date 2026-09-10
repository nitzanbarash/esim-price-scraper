#!/usr/bin/env python3
"""
Stellar consumption reader -- the second supplier's half of the usage sweep
(bot #5, usage_bot.py).

esim.dog answers one request with a LIST of ICCIDs. Stellar has no such
endpoint and no ICCID at all (stellar_buyer.site_payload leaves it empty by
design), so a Stellar package is found the only way it can be: through the
portal link the buyer wrote on its receipts row,

    https://wholesale.stellarsecurity.com/orders/<order_id>

which gives GET /orders/<id> -> data.esims[] -> GET /esims/<sim_id>/usage.

THE METER IS ITS OWN ENDPOINT. /esims/<sim_id> carries no figures at all --
only the flag `usage_available` and `lifecycle.last_usage_update_at`. The
'usage endpoint hunt' step of .github/workflows/stellar-smoke.yml asked the
API for five candidate paths on a live order (2026-09-10) and got exactly one:

    GET /esims/<sim_id>/usage  -> 200   data.{used_bytes,total_bytes,
                                        remaining_bytes,usage_percent},
                                        expires_at, status, lifecycle.*
    GET /esims/<sim_id>/data         -> 404
    GET /esims/<sim_id>/consumption  -> 404
    GET /orders/<order_id>/usage     -> 404

Note which payload holds the package's own `expires_at`: the meter's. The eSIM
record has none -- its only expiry is `vpn.expires_at`, which belongs to the
VPN Stellar bundles alongside the data, hence NOT_USAGE_SUBTREES below.

WHAT THIS MODULE REFUSES TO DO IS THE POINT OF IT.

The names above were read as NAMES, never as values, and no Stellar package
has yet been metered end to end -- so the tables below are still a list, not a
contract. A mapper that guesses would be worse than no mapper: a wrong reading
becomes a customer's meter, and a wrongly-zero 'total' becomes a package
RETIRED as used up while it still works. So the mapping is strict:

  * `used` AND `total` must both be found, or the answer is Unknown(keys=...)
    -- the sorted dotted key paths of what did come back, names only.
  * usage_bot leaves an Unknown row completely untouched: no status, no meter,
    no retirement timer. It stays exactly as it was until a person (helped by
    the key paths in the log, and by the 'esim fields' step of
    stellar-smoke.yml) adds the real name to the tables below.
  * A network error is a per-row None. Nothing in here may raise into the
    sweep: one silent Stellar order must never freeze every esim.dog
    customer's meter, which is the exact outage test_usage_bot.py exists for.

UNITS. Stellar states its plan size as `data.megabytes` (stellar_prices.
_gb_from_mb), so MEGABYTES are its house unit and a key that names no unit is
read as MB. A key that names one wins: *_bytes, *_kb, *_mb, *_gb.

Secrecy: this repo is PUBLIC and so are its Actions logs. This module logs key
NAMES and order ids, never a value, never an activation code, never a key.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Union

import requests

log = logging.getLogger("stellar-usage")

BASE = "https://wholesale.stellarsecurity.com/api/v1"
TIMEOUT = 30
GB_MB = 1024.0
GB_BYTES = float(1024 ** 3)

# Envelope keys Stellar wraps a payload in. Used to tell {"data": {...}} the
# envelope from an eSIM's own "data": {"megabytes": N} block -- unwrapping the
# second would throw away the whole object and report every shape as Unknown.
ENVELOPE = {"data", "meta", "links", "message", "success", "status", "errors", "error"}

# A path holding any of these is money or account state, never consumption.
# Without this, a 'total' on an invoice line reads as the package size -- and a
# package "1.2 of 2.57 GB used" is a meter built out of euros.
NOT_USAGE = ("price", "cost", "amount", "currency", "cents", "eur", "usd",
             "wallet", "balance", "fee", "tax", "discount", "quantity")

# Whole SUBTREES that describe something other than this package. Stellar sells
# a VPN alongside the data plan and carries it inside the same eSIM payload, so
# `data.vpn.expires_at` is the VPN's expiry -- read as the package's, it tells a
# customer their data dies on the wrong day, and the retirement rule agrees.
# This is matched on a path SEGMENT, not as a substring like NOT_USAGE: 'vpn'
# is three letters that a legitimate key could contain, and only a key that IS
# 'vpn' opens a subtree that is not ours.
NOT_USAGE_SUBTREES = ("vpn",)

# Leaf names, most specific first. The first one present wins, so adding a new
# name to the top of a list is how this module learns a shape.
USED_LEAVES = (
    "data_used_bytes", "data_used_mb", "used_bytes", "used_mb", "used_megabytes",
    "used_data", "data_used", "usage_used", "used_volume", "volume_used",
    "consumed_bytes", "consumed_mb", "data_consumed", "consumed", "used",
)
TOTAL_LEAVES = (
    "total_bytes", "total_mb", "total_megabytes", "data_total_bytes",
    "data_total_mb", "data_total", "total_data", "total_volume", "data_volume",
    "data_limit", "quota_bytes", "quota_mb", "quota", "megabytes", "total",
)
EXPIRY_LEAVES = (
    "expires_at", "expiry_date", "expiration_date", "expired_at", "expiration",
    "expiry", "expires", "valid_until", "ends_at", "end_date",
)
STATUS_LEAVES = ("esim_status", "sim_status", "usage_status", "state", "status")

# The three payloads a reading can come out of, written the way they are asked
# for and never with a real id in them: this name is logged, and the log is
# public. Which of them answered is the one fact that separates "the account
# has the meter endpoint" from "it has not, and every row degraded to Unknown".
EP_USAGE = "/esims/<sim_id>/usage"
EP_ESIM = "/esims/<sim_id>"
EP_ORDER = "/orders/<order_id>"

_logged_shape = False        # the key-path log line is worth exactly once a run
_logged_endpoint = False     # ...and so is the name of whichever one answered
_stale_rows = 0              # readings the supplier itself flagged as not fresh


@dataclass
class Unknown:
    """Stellar answered, but not in a shape this module dares read.

    Carries the key NAMES that did come back so the fix is a five-minute edit
    to the tables above rather than another live purchase.
    """
    keys: list = field(default_factory=list)


Result = Union[dict, Unknown, None]


# -- json shape helpers -------------------------------------------------------

def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def _segments(path: str) -> list[str]:
    return [_norm(p) for p in str(path).replace("[]", "").split(".")]


def _leaf(path: str) -> str:
    return _norm(path.replace("[]", "").rsplit(".", 1)[-1])


def _foreign_subtree(path: str) -> bool:
    """True if this path runs through a subtree that is not this data package."""
    return any(seg in NOT_USAGE_SUBTREES for seg in _segments(path))


def key_paths(obj, prefix: str = "") -> list[str]:
    """Every key in the payload as a dotted path -- names only, no values.

    A list contributes ONE path for all of its items ('esims[].sim_id'), so the
    output is a description of the shape and stays the same length whether the
    order holds one eSIM or ten.
    """
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.append(p)
            out.extend(key_paths(v, p))
    elif isinstance(obj, (list, tuple)):
        for v in obj[:3]:
            out.extend(key_paths(v, f"{prefix}[]"))
    return sorted(set(out))


def _scalars(obj, prefix: str = "", out: Optional[dict] = None) -> dict:
    """path -> scalar value, for the same paths key_paths() names."""
    out = {} if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _scalars(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, (list, tuple)):
        for v in obj[:3]:
            _scalars(v, f"{prefix}[]", out)
    elif prefix and not isinstance(obj, bool):
        out.setdefault(prefix, obj)
    return out


def _number(v) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", str(v or ""))
    return float(m.group(1)) if m else None


def _unwrap(body):
    """{"data": {...}} the envelope -> {...}; an eSIM's own 'data' block stays."""
    if isinstance(body, dict) and isinstance(body.get("data"), dict) \
            and set(body) <= ENVELOPE:
        return body["data"]
    return body if isinstance(body, dict) else {}


def to_gb(value: float, leaf: str) -> float:
    """A figure plus the unit its own key names. No key, no unit: megabytes.

    'megabytes' contains the letters of 'bytes', so the named units are tested
    before the bare one -- read the other way round, every plan size on the
    account is a billion times too small and every package looks used up.
    """
    n = _norm(leaf)
    if "megabyte" in n or re.search(r"(^|_)mb(_|$)", n):
        return value / GB_MB
    if "gigabyte" in n or re.search(r"(^|_)gb(_|$)", n):
        return value
    if "kilobyte" in n or re.search(r"(^|_)kb(_|$)", n):
        return value / (GB_MB * GB_MB)
    if "byte" in n:
        return value / GB_BYTES
    return value / GB_MB


def _pick(scalars: dict, leaves: tuple, numeric: bool):
    """(path, value) for the first wanted leaf name present, else None.

    Ties are broken by the SHALLOWEST path and then alphabetically, so the same
    payload always maps the same way -- a mapper whose answer depends on dict
    order is a meter that changes when nothing changed.
    """
    for want in leaves:
        hits = []
        for path, value in scalars.items():
            if _leaf(path) != want:
                continue
            if any(w in _norm(path) for w in NOT_USAGE):
                continue
            if _foreign_subtree(path):
                continue
            v = _number(value) if numeric else (str(value).strip() or None)
            if v is None:
                continue
            hits.append((path.count("."), path, v))
        if hits:
            hits.sort()
            return hits[0][1], hits[0][2]
    return None


def map_usage(blob) -> Result:
    """One eSIM payload -> a usage reading, or Unknown(keys=...).

    The reading is deliberately the same shape fulfillment_bot.fetch_usage
    returns, so usage_bot's write-back, its retirement rule and its push to
    waverole.com need to know nothing about which supplier answered.
    """
    global _logged_shape
    blob = _unwrap(blob)
    if not isinstance(blob, dict) or not blob:
        return Unknown(keys=[])
    scalars = _scalars(blob)
    used = _pick(scalars, USED_LEAVES, numeric=True)
    total = _pick(scalars, TOTAL_LEAVES, numeric=True)
    expiry = _pick(scalars, EXPIRY_LEAVES, numeric=False)
    status = _pick(scalars, STATUS_LEAVES, numeric=False)
    if not _logged_shape:
        _logged_shape = True
        log.info("stellar esim fields (names only): " + ", ".join(key_paths(blob)))
        # All four PATHS, never their values. The expiry path is the one worth
        # reading twice: 'vpn.expires_at' appearing here would mean a customer
        # is being told their data dies on the VPN's day.
        log.info(f"stellar usage mapped from: used={used[0] if used else None} "
                 f"total={total[0] if total else None} "
                 f"expires={expiry[0] if expiry else None} "
                 f"status={status[0] if status else None}")
    if used is None or total is None:
        return Unknown(keys=key_paths(blob))
    used_gb = to_gb(used[1], used[0])
    total_gb = to_gb(total[1], total[0])
    if total_gb <= 0 or used_gb < 0:
        # A found shape carrying a zero size is not a reading. Reported as
        # Unknown on purpose: usage_bot leaves those rows alone, where a
        # 'total 0' reading would retire a live package as used up.
        return Unknown(keys=key_paths(blob))
    return {
        "used_gb": round(used_gb, 3),
        "total_gb": round(total_gb, 3),
        "pct": max(0, min(100, round(used_gb / total_gb * 100))),
        "expires": expiry[1] if expiry else "",
        "status": status[1] if status else "",
        "supplier": "Stellar",
    }


# -- the network --------------------------------------------------------------

def order_id_from_url(url) -> str:
    """'https://wholesale.stellarsecurity.com/orders/<id>?tab=x' -> '<id>'.

    Not anchored, and a bare id is accepted: the owner pastes portal links by
    hand and a copied url carries a trailing slash, a query or a fragment
    (stellar_buyer.order_rows learned this the expensive way).
    """
    s = str(url or "").strip()
    m = re.search(r"/orders/([A-Za-z0-9_-]+)", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9_-]{6,}", s) else ""


def _session(key: str, session=None):
    s = session or requests.Session()
    s.headers.update({"Authorization": f"Bearer {key}",
                      "Accept": "application/json"})
    return s


def _get(s, url) -> dict:
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    try:
        return _unwrap(r.json())
    except ValueError:
        return {}


def _freshness(blob) -> list[str]:
    """The freshness flags the payload states about ITSELF -- relayed, never
    acted on.

    The meter answers with `stale` and `refreshed` alongside the figures. This
    module has no live sample of a stale one, so it invents no rule out of
    them: a flagged reading is still returned, still written, still pushed to
    the customer's page. All that happens is that the run says so, so that a
    fleet whose meters have quietly stopped refreshing is a visible number in
    the log rather than a set of figures that simply stopped moving.

    `status` is deliberately NOT judged here. Whether a status word means the
    figure is out of date is a policy, and the only place a policy about
    status belongs is usage_bot.decide_status, which already reads the word
    this module passes through untouched.
    """
    blob = _unwrap(blob)
    if not isinstance(blob, dict):
        return []
    notes = []
    if blob.get("stale") is True:
        notes.append("stale")
    if blob.get("refreshed") is False:
        notes.append("not refreshed")
    return notes


def _note_endpoint(endpoint: str, blob) -> list[str]:
    """Say once a run which endpoint the meter came out of. Names only."""
    global _logged_endpoint, _stale_rows
    notes = _freshness(blob)
    if notes:
        _stale_rows += 1
    if not _logged_endpoint:
        _logged_endpoint = True
        log.info(f"stellar meter read from {endpoint}"
                 + (f" (the supplier flags this one: {', '.join(notes)})" if notes else ""))
    return notes


def _one(s, url) -> Result:
    oid = order_id_from_url(url)
    if not oid:
        log.warning("stellar row has no readable portal link -- left untouched")
        return None
    try:
        data = _get(s, f"{BASE}/orders/{oid}")
    except Exception as e:
        log.warning(f"stellar order {oid}: {type(e).__name__} -- left untouched")
        return None
    esims = data.get("esims") or []
    if not esims:
        # Stellar knows the order but names no eSIM. Same standing as an
        # esim.dog ICCID the supplier does not report on: no reading.
        return None
    seen: list[str] = []
    for e in esims:
        if not isinstance(e, dict):
            continue
        detail = None
        sim_id = str(e.get("sim_id") or e.get("id") or "").strip()
        if sim_id:
            # The meter is its OWN endpoint, and only this one exists: the
            # 'usage endpoint hunt' step of stellar-smoke.yml asked for five
            # and got 200 here and 404 for /data, /consumption and
            # /orders/<id>/usage. /esims/<sim_id> carries no figures at all.
            try:
                meter = _get(s, f"{BASE}/esims/{sim_id}/usage")
            except Exception as ex:
                # Not an outage on its own: an eSIM whose usage_available is
                # false may simply have no meter yet. The eSIM record below is
                # tried next, and a real outage fails there too.
                log.info(f"stellar meter for an esim of order {oid}: "
                         f"{type(ex).__name__} -- trying the esim record")
            else:
                got = map_usage(meter)
                if isinstance(got, dict):
                    got["sim_id"] = sim_id
                    _note_endpoint(EP_USAGE, meter)
                    return got
                seen.extend(got.keys)
            try:
                detail = _get(s, f"{BASE}/esims/{sim_id}")
            except Exception as ex:
                log.warning(f"stellar esim of order {oid}: {type(ex).__name__} "
                            "-- left untouched")
                return None
        # Only if the meter did not answer. The eSIM record carries no figures
        # on our account, so this pair is the degrade path: on an account
        # without the endpoint it produces Unknown, which usage_bot leaves
        # alone -- never a retirement.
        for endpoint, blob in ((EP_ESIM, detail), (EP_ORDER, e)):
            if not blob:
                continue
            got = map_usage(blob)
            if isinstance(got, dict):
                got["sim_id"] = sim_id
                _note_endpoint(endpoint, blob)
                return got
            seen.extend(got.keys)
    return Unknown(keys=sorted(set(seen)))


def fetch_usage(order_urls, session=None, key: Optional[str] = None) -> dict:
    """{portal url -> reading | Unknown | None}, one entry per url given.

    Keyed by the url the caller passed, not by order id: the receipts row holds
    the url, and handing back anything else would make the caller re-derive the
    id and re-introduce every parsing trap this module already handles.
    """
    global _logged_endpoint, _stale_rows
    _logged_endpoint, _stale_rows = False, 0     # one sweep, one endpoint line
    urls = [u for u in (order_urls or [])]
    if not urls:
        return {}
    key = (key or os.getenv("STELLAR_API_KEY", "")).strip()
    if not key:
        # Not an outage and not an error: the PC copy of this repo has no
        # Stellar key by design (memory: stellar-key-placement). Every Stellar
        # row is simply left as it was.
        log.info("STELLAR_API_KEY is not set -- Stellar rows are left untouched")
        return {u: None for u in urls}
    s = _session(key, session)
    out = {}
    for u in urls:
        if u in out:
            continue
        try:
            out[u] = _one(s, u)
        except Exception as e:
            # Belt and braces. Nothing in this module may raise into the sweep.
            log.warning(f"stellar usage: {type(e).__name__} -- row left untouched")
            out[u] = None
    ok = sum(1 for v in out.values() if isinstance(v, dict))
    unknown = sum(1 for v in out.values() if isinstance(v, Unknown))
    log.info(f"stellar: {ok} of {len(out)} package(s) read"
             + (f" - {unknown} in an unrecognised shape (left untouched)" if unknown else "")
             # Written, pushed and counted like any other -- see _freshness.
             + (f" - {_stale_rows} the supplier flagged as not fresh (still written)"
                if _stale_rows else ""))
    return out
