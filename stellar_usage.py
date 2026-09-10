#!/usr/bin/env python3
"""
Stellar consumption reader -- the second supplier's half of the usage sweep
(bot #5, usage_bot.py).

esim.dog answers one request with a LIST of ICCIDs. Stellar has no such
endpoint and no ICCID at all (stellar_buyer.site_payload leaves it empty by
design), so a Stellar package is found the only way it can be: through the
portal link the buyer wrote on its receipts row,

    https://wholesale.stellarsecurity.com/orders/<order_id>

which gives GET /orders/<id> -> data.esims[] -> GET /esims/<sim_id>.

WHAT THIS MODULE REFUSES TO DO IS THE POINT OF IT.

We have never read a live Stellar usage payload -- nothing has been sold
through Stellar yet -- so the field names below are an educated list, not a
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

_logged_shape = False        # the key-path log line is worth exactly once a run


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


def _leaf(path: str) -> str:
    return _norm(path.replace("[]", "").rsplit(".", 1)[-1])


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
    if not _logged_shape:
        _logged_shape = True
        log.info("stellar esim fields (names only): " + ", ".join(key_paths(blob)))
        log.info(f"stellar usage mapped from: used={used[0] if used else None} "
                 f"total={total[0] if total else None}")
    if used is None or total is None:
        return Unknown(keys=key_paths(blob))
    used_gb = to_gb(used[1], used[0])
    total_gb = to_gb(total[1], total[0])
    if total_gb <= 0 or used_gb < 0:
        # A found shape carrying a zero size is not a reading. Reported as
        # Unknown on purpose: usage_bot leaves those rows alone, where a
        # 'total 0' reading would retire a live package as used up.
        return Unknown(keys=key_paths(blob))
    expiry = _pick(scalars, EXPIRY_LEAVES, numeric=False)
    status = _pick(scalars, STATUS_LEAVES, numeric=False)
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
            try:
                detail = _get(s, f"{BASE}/esims/{sim_id}")
            except Exception as ex:
                log.warning(f"stellar esim of order {oid}: {type(ex).__name__} "
                            "-- left untouched")
                return None
        for blob in (detail, e):
            if not blob:
                continue
            got = map_usage(blob)
            if isinstance(got, dict):
                got["sim_id"] = sim_id
                return got
            seen.extend(got.keys)
    return Unknown(keys=sorted(set(seen)))


def fetch_usage(order_urls, session=None, key: Optional[str] = None) -> dict:
    """{portal url -> reading | Unknown | None}, one entry per url given.

    Keyed by the url the caller passed, not by order id: the receipts row holds
    the url, and handing back anything else would make the caller re-derive the
    id and re-introduce every parsing trap this module already handles.
    """
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
             + (f" - {unknown} in an unrecognised shape (left untouched)" if unknown else ""))
    return out
