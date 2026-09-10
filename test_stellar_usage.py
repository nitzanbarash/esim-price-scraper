#!/usr/bin/env python3
"""Pins for stellar_usage.map_usage -- what it must read, and what it must
refuse to read.

WHY THIS FILE EXISTS. Stellar sells a VPN alongside the data plan and hands it
back inside the same eSIM payload, under its own `vpn` block with its own
`expires_at`. Nothing in the leaf tables can tell those two dates apart by name
-- they are the same name -- so the shallowest-path tie-break would have handed
the VPN's date to the customer as the day their DATA dies. usage_bot turns that
date into a retirement, so a wrong one does not just mis-print a meter: it puts
a live package to bed early, or keeps a dead one on sale.

So `vpn` is excluded the way money paths are, but by SEGMENT rather than by
substring: only a key that IS 'vpn' opens a subtree that is not ours.

The rest of the file re-states the contract those exclusions live inside: used
AND total, or the answer is Unknown and usage_bot writes nothing at all.

Run:  python3 test_stellar_usage.py
"""

import sys
from unittest import mock

import stellar_usage
from stellar_usage import Unknown, map_usage

GB = 1024 ** 3
_fails: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


# -- the vpn subtree is not this package -------------------------------------
print("\n-- data.vpn is a different product wearing our field names --")

# A real-shaped payload: the package's own figures at the top, and the only
# `expires_at` anywhere in it belongs to the VPN.
VPN_EXPIRY_ONLY = {
    "sim_id": "sim_v",
    "status": "active",
    "used_mb": 1024,
    "total_mb": 5120,
    "vpn": {"expires_at": "2027-01-01T00:00:00+0000", "status": "active"},
}
got = map_usage(VPN_EXPIRY_ONLY)
check("the package still reads", isinstance(got, dict), True)
check("used", got["used_gb"], 1.0)
check("total", got["total_gb"], 5.0)
# The point of the whole file.
check("the VPN's date is NOT the package's expiry", got["expires"], "")
check("...and the package's own status is untouched", got["status"], "active")

# When the package has a date of its own, that is the one -- even though the
# VPN's sits at the same depth and would win alphabetically.
BOTH_EXPIRIES = dict(VPN_EXPIRY_ONLY, expires_at="2026-10-01T09:00:00+0000")
check("a real expiry beats the VPN's",
      map_usage(BOTH_EXPIRIES)["expires"], "2026-10-01T09:00:00+0000")

# Deeper, and with figures too: none of it may reach the meter.
NESTED_VPN = {
    "data": {"megabytes": 2048},
    "data_used": 512,
    "data_vpn_unrelated": 7,          # substring, not a segment: NOT excluded
    "vpn": {"usage": {"used_mb": 9999, "total_mb": 9999},
            "expiry_date": "2030-01-01"},
}
got = map_usage(NESTED_VPN)
check("a nested vpn subtree cannot supply the meter",
      (got["used_gb"], got["total_gb"]), (0.5, 2.0))
check("nor the expiry, however deep", got["expires"], "")
check("'vpn' is matched as a whole segment, never as three letters",
      stellar_usage._foreign_subtree("data.vpnx.used_mb"), False)
check("...while the real segment is caught",
      stellar_usage._foreign_subtree("data.vpn.expires_at"), True)
check("...at any depth, in a list too",
      stellar_usage._foreign_subtree("data.bundles[].vpn.total_mb"), True)
check("a package that only LOOKS like it is still read",
      stellar_usage._foreign_subtree("data.vpn_addon.used_mb"), False)

# A payload that is nothing but the VPN is a payload with no package in it.
VPN_ONLY = {"vpn": {"expires_at": "2027-01-01T00:00:00+0000",
                    "used_mb": 10, "total_mb": 100}}
got = map_usage(VPN_ONLY)
check("vpn-only is Unknown, not a reading", isinstance(got, Unknown), True)
check("...and never mistaken for one", isinstance(got, dict), False)
check("...but its key NAMES still come back, to fix the tables with",
      got.keys, ["vpn", "vpn.expires_at", "vpn.total_mb", "vpn.used_mb"])


# -- the contract the exclusions live inside ---------------------------------
print("\n-- used AND total, or nothing is written --")
check("both present is a reading",
      isinstance(map_usage({"used_mb": 512, "total_mb": 1024}), dict), True)
check("used alone is Unknown",
      isinstance(map_usage({"used_mb": 512}), Unknown), True)
check("total alone is Unknown",
      isinstance(map_usage({"total_mb": 1024}), Unknown), True)
check("neither is Unknown",
      isinstance(map_usage({"sim_id": "s", "plan_id": "p"}), Unknown), True)
check("an empty answer is Unknown", isinstance(map_usage({}), Unknown), True)
# A found shape carrying a zero size is not a reading: it would retire a live
# package as used up.
check("a zero total is Unknown, not a spent package",
      isinstance(map_usage({"used_mb": 0, "total_mb": 0}), Unknown), True)
# Money is not consumption, by the same mechanism the vpn block is excluded.
check("a priced 'total' cannot become the package size",
      map_usage({"price": {"total_mb": 99}, "data": {"megabytes": 2048},
                 "data_used": 512})["total_gb"], 2.0)


# -- the two real payloads, by name ------------------------------------------
# Both key lists below are verbatim from the 'usage endpoint hunt' and 'esim
# fields' steps of stellar-smoke.yml on a live order (2026-09-10). Only the
# NAMES were read there -- the values here are invented, and the point of the
# pins is which name is read out of which payload.
print("\n-- the shapes Stellar actually returns --")

# GET /esims/<sim_id>/usage -- the only one of five candidate paths that is not
# a 404, and the only payload with figures in it.
REAL_METER = {
    "sim_id": "s1",
    "status": "active",
    "activated_at": "2026-09-10T10:00:00+0000",
    "expires_at": "2026-10-10T10:00:00+0000",
    "data": {"used_bytes": 1 * GB, "total_bytes": 5 * GB,
             "remaining_bytes": 4 * GB, "usage_percent": 20},
    "lifecycle": {"status": "active", "profile_status": "enabled",
                  "installed": True,
                  "last_usage_update_at": "2026-09-10T16:00:00+0000",
                  "activation_detected_at": "2026-09-10T10:01:00+0000",
                  "installation_detected_at": "2026-09-10T09:59:00+0000"},
    "stale": False, "refreshed": True,
    "synced_at": "2026-09-10T16:00:00+0000",
}
got = map_usage(REAL_METER)
check("the meter endpoint reads", isinstance(got, dict), True)
check("used, in bytes", got["used_gb"], 1.0)
check("total, in bytes", got["total_gb"], 5.0)
check("percent", got["pct"], 20)
check("the package's own expiry", got["expires"], "2026-10-10T10:00:00+0000")
check("the package's own status, not the lifecycle's", got["status"], "active")
check("'remaining_bytes' is not a size", got["total_gb"], 5.0)

# GET /esims/<sim_id> -- no figures anywhere, and the ONLY date in it belongs
# to the VPN. This is the payload the vpn guard exists for.
REAL_ESIM = {
    "sim_id": "s1", "id": "e1", "status": "active", "usage_available": True,
    "unit": "GB", "duration_days": 30, "plan_id": "p1",
    "order_id": "o1", "order_item_id": "i1",
    "customer_link": "https://example.invalid/x",
    "fulfilled_at": "2026-09-10T09:00:00+0000",
    "updated_at": "2026-09-10T16:00:00+0000",
    "installation": {"activation_code": "LPA:1$smdp.example$SECRET",
                     "apn": "internet",
                     "qr_code_url": "https://example.invalid/qr.png"},
    "lifecycle": {"status": "active", "profile_status": "enabled",
                  "installed": True,
                  "last_usage_update_at": "2026-09-10T16:00:00+0000"},
    "vpn": {"account_number": "1234", "expires_at": "2027-01-01T00:00:00+0000",
            "included": True, "ready": True, "status": "active",
            "validity_days": 365},
}
got = map_usage(REAL_ESIM)
check("the esim record carries no meter, so it is Unknown",
      isinstance(got, Unknown), True)
check("and the VPN's date never leaves it",
      "expires_at" in [k.rsplit(".", 1)[-1] for k in got.keys], True)


# -- and through the network, where usage_bot sees it ------------------------
print("\n-- what the sweep is handed --")
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
    """Just enough of requests.Session that the module's own auth-header code
    runs. The key never leaves this object."""

    def __init__(self, handler):
        self.headers, self.get = {}, handler


ORDER = {"esims": [{"sim_id": "s1"}]}


def _api(meter=None, meter_code=200, esim=None, seen=None):
    """The three URLs the module walks, answered separately.

    /orders/<id> -> /esims/<sim_id>/usage -> /esims/<sim_id>, in that order:
    the meter is asked FIRST, because it is the only payload with figures.
    """
    def handler(url, **kw):
        if seen is not None:
            seen.append(url.rsplit("/api/v1", 1)[-1])
        if url.endswith("/usage"):
            return _Resp({"data": meter or {}}, meter_code)
        if "/esims/" in url:
            return _Resp({"data": esim or {}})
        return _Resp({"data": ORDER})
    return handler


seen: list[str] = []
res = stellar_usage.fetch_usage(
    [URL], session=_Session(_api(meter=REAL_METER, esim=REAL_ESIM, seen=seen)),
    key="k")
check("the real pair reads through the network", isinstance(res[URL], dict), True)
check("used", res[URL]["used_gb"], 1.0)
check("expiry comes from the meter, not the VPN",
      res[URL]["expires"], "2026-10-10T10:00:00+0000")
check("and the sim_id is carried back", res[URL]["sim_id"], "s1")
check("the meter is asked before the esim record",
      seen, ["/orders/o1", "/esims/s1/usage"])

# A 404 there (an eSIM with no meter yet) is not an outage: fall through.
seen = []
res = stellar_usage.fetch_usage(
    [URL], session=_Session(_api(meter_code=404, esim=REAL_ESIM, seen=seen)),
    key="k")
check("no meter yet falls back to the esim record", seen,
      ["/orders/o1", "/esims/s1/usage", "/esims/s1"])
check("...which has no figures, so the row is left untouched",
      isinstance(res[URL], Unknown), True)
check("...and the VPN's date is not smuggled out as a reading",
      isinstance(res[URL], dict), False)

res = stellar_usage.fetch_usage(
    [URL], session=_Session(_api(meter=VPN_EXPIRY_ONLY)), key="k")
check("a vpn-carrying meter is read", isinstance(res[URL], dict), True)
check("...with no borrowed expiry", res[URL]["expires"], "")

res = stellar_usage.fetch_usage(
    [URL], session=_Session(_api(meter=VPN_ONLY, esim=VPN_ONLY)), key="k")
check("a vpn-only package is Unknown, so the row is left untouched",
      isinstance(res[URL], Unknown), True)

# Nothing in the module may raise into the sweep.
res = stellar_usage.fetch_usage(
    [URL], session=_Session(mock.Mock(side_effect=OSError("no route"))), key="k")
check("a dead network is None, never a reading", res, {URL: None})


# -- the dict usage_bot is handed, key for key -------------------------------
# The write-back, the retirement rule and the push to waverole.com all read
# this dict without asking who answered, so its key names are a contract with
# fulfillment_bot.fetch_usage, not an internal detail. A renamed key here is a
# KeyError in the middle of the sweep, after some rows have already been
# written.
print("\n-- the shape the sweep consumes --")

got = map_usage(REAL_METER)
check("nothing more, nothing less", sorted(got),
      ["expires", "pct", "status", "supplier", "total_gb", "used_gb"])
check("...and it names its supplier for the log", got["supplier"], "Stellar")
seen = []
res = stellar_usage.fetch_usage(
    [URL], session=_Session(_api(meter=REAL_METER, esim=REAL_ESIM, seen=seen)),
    key="k")
check("through the network it also carries the sim it was read from",
      sorted(res[URL]),
      ["expires", "pct", "sim_id", "status", "supplier", "total_gb", "used_gb"])

# Both endpoints gone is a network answer, not a shape answer: None, and
# usage_bot writes nothing for None either -- neither may reach decide_status,
# which would read the silence as "the supplier never heard of this eSIM" and
# retire a live package.
res = stellar_usage.fetch_usage(
    [URL], session=_Session(mock.Mock(side_effect=RuntimeError("HTTP 500"))),
    key="k")
check("a dead order call leaves the row untouched", res[URL], None)


# -- the run says which endpoint answered, in names only ---------------------
# Whether the meter endpoint or the fallback answered is the single fact that
# separates "this account has the meter" from "it has not, and every row
# degraded to Unknown". It has to be in the log, and this repo's Actions logs
# are PUBLIC -- so it is the endpoint's NAME, with its placeholder, never a
# real sim id.
print("\n-- one endpoint line a run, with no id in it --")

with mock.patch.object(stellar_usage, "log") as spy:
    stellar_usage.fetch_usage(
        [URL, URL.replace("o1", "o2")],
        session=_Session(_api(meter=REAL_METER, esim=REAL_ESIM)), key="k")
    lines = [str(c.args[0]) for c in spy.info.call_args_list]
named = [l for l in lines if "read from" in l]
check("named exactly once, however many packages", len(named), 1)
check("...and it is the meter endpoint", stellar_usage.EP_USAGE in named[0], True)
check("...written with the placeholder, never the sim id",
      any("s1" in l.replace("esims", "") for l in named), False)

# The same line when only the fallback could answer, so a silently degraded
# account is visible in the log instead of looking like a fleet of dead eSIMs.
FALLBACK_ESIM = dict(REAL_ESIM, used_mb=1024, total_mb=5120)
with mock.patch.object(stellar_usage, "log") as spy:
    res = stellar_usage.fetch_usage(
        [URL], session=_Session(_api(meter_code=404, esim=FALLBACK_ESIM)), key="k")
    lines = [str(c.args[0]) for c in spy.info.call_args_list]
check("a fallback reading is still a reading", isinstance(res[URL], dict), True)
check("...and the log names the endpoint it came from",
      any(stellar_usage.EP_ESIM in l for l in lines if "read from" in l), True)


# -- a stale reading is still a reading --------------------------------------
# The meter states `stale` and `refreshed` about itself. This module invents no
# rule out of them: no live stale sample has ever been seen, and a guess here
# would either freeze a good meter or hide a dead one. The reading is written,
# pushed and counted exactly as any other -- the run simply says so.
print("\n-- stale is reported, not acted on --")

STALE = dict(REAL_METER, stale=True, refreshed=False)
with mock.patch.object(stellar_usage, "log") as spy:
    res = stellar_usage.fetch_usage(
        [URL], session=_Session(_api(meter=STALE, esim=REAL_ESIM)), key="k")
    lines = [str(c.args[0]) for c in spy.info.call_args_list]
check("still a reading", isinstance(res[URL], dict), True)
check("...with its figures untouched",
      (res[URL]["used_gb"], res[URL]["total_gb"]), (1.0, 5.0))
check("...and the run says so", any("not fresh" in l for l in lines), True)
check("...naming the flag the supplier set", any("stale" in l for l in lines), True)


print()
if _fails:
    print(f"{len(_fails)} FAILED: " + ", ".join(_fails))
    sys.exit(1)
print("all stellar-usage tests passed")
sys.exit(0)
