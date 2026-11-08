#!/usr/bin/env python3
"""
Tests for usage_live.py, the 10-minute usage feed to the customer's order
page. Offline: the sheet, both suppliers and the site are faked.

What is pinned here:
  * only rows usage_bot still checks are read, and a row whose two supplier
    columns disagree is skipped (as in usage_bot);
  * no key for s2 = s2 skipped, s1 still read;
  * a supplier "could not ask" (None / Unknown) is never sent;
  * only a reading that moved is sent, plus every package once an hour;
  * the site's answer ({order_id, pct} objects) is what marks a reading sent;
  * nothing here writes the sheet (no update call exists in the module).

Run:  python test_usage_live.py
"""

import os
import sys
import tempfile
import types

import usage_bot as ub
import usage_live as ul

_fails: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n         got  {got!r}\n         want {want!r}")
        _fails.append(name)


HDR = [ub.COL_ORDER, ub.COL_STATUS, ub.COL_SOURCE, ub.COL_ROUTE, ub.COL_ICCID, ub.COL_LINK]


def row(oid, status, source, route, iccid="", link=""):
    return [oid, status, source, route, iccid, link]


S1_ROUTE = "Black"
S2_ROUTE = ub.SRC_STELLAR + " plan-1"


def rows_fixture():
    return [HDR,
            row("WR-A", ub.ACTIVE, ub.SRC_ESIMDOG, S1_ROUTE, iccid="8901 0000 0000 0001"),
            row("WR-B", "", ub.SRC_ESIMDOG, S1_ROUTE, iccid="89010000000000002"),
            row("WR-C", ub.USED_UP, ub.SRC_ESIMDOG, S1_ROUTE, iccid="89010000000000003"),   # finished
            row("WR-D", ub.ACTIVE, ub.SRC_STELLAR, S2_ROUTE, link="https://portal.example/orders/abc"),
            row("", ub.ACTIVE, ub.SRC_ESIMDOG, S1_ROUTE, iccid="89010000000000009")]       # no order id


print("\nlive_rows")
todo, mism = ul.live_rows(rows_fixture())
check("finished and id-less rows are left out", sorted(t["order_id"] for t in todo), ["WR-A", "WR-B", "WR-D"])
check("ICCID digits only", [t["iccid"] for t in todo if t["order_id"] == "WR-A"], ["8901000000000001"])
check("s2 row flagged", [t["s2"] for t in todo if t["order_id"] == "WR-D"], [True])
orig = ub.decide_source
ub.decide_source = lambda s, r: (s, "mismatch") if s == ub.SRC_STELLAR else (s, "ok")
todo2, mism2 = ul.live_rows(rows_fixture())
ub.decide_source = orig
check("a disagreeing row is skipped and counted", (len(todo2), mism2), (2, 1))
try:
    ul.live_rows([["something else"], ["x"]])
    check("missing column raises", False, True)
except RuntimeError:
    check("missing column raises", True, True)

print("\nreadings")
calls = {"s1": [], "s2": []}


def fake_s1(iccids):
    calls["s1"].append(list(iccids))
    return {"8901000000000001": {"used_gb": 1.234, "total_gb": 5.0, "expires": "2026-12-01"},
            "89010000000000002": {"used_gb": 0, "total_gb": 0, "expires": ""}}      # no total: not a reading


ul.fetch_usage = fake_s1
r = ul.readings(todo, "")
check("no s2 key: s1 only", sorted(r), ["WR-A"])
fake_st = types.ModuleType("stellar_usage")
fake_st.RATE_LIMIT = 55


class Unknown(dict):
    pass


def fake_s2(urls, key=None):
    calls["s2"].append((list(urls), key))
    return {urls[0]: {"used_gb": 3.5, "total_gb": 10.0, "expires": ""}}


fake_st.fetch_usage = fake_s2
sys.modules["stellar_usage"] = fake_st
os.environ["STELLAR_RATE_LIMIT"] = "90"
r = ul.readings(todo, "k-test")
check("with a key: s2 read too", sorted(r), ["WR-A", "WR-D"])
check("key handed to the s2 reader", calls["s2"][-1][1], "k-test")
check("rate capped under the supplier's 60", fake_st.RATE_LIMIT, 55)
fake_st.fetch_usage = lambda urls, key=None: {u: None for u in urls}
check("s2 'could not ask' is never sent", sorted(ul.readings(todo, "k")), ["WR-A"])
fake_st.fetch_usage = lambda urls, key=None: {u: Unknown() for u in urls}
check("s2 unknown shape is never sent", sorted(ul.readings(todo, "k")), ["WR-A"])

print("\ndue")
read = {"WR-A": {"used_gb": 1.234, "total_gb": 5.0, "expires": "x"}}
check("first time: sent", [i["order_id"] for i in ul.due(read, {}, 1000)], ["WR-A"])
check("rounded to the site's 2 places", ul.due(read, {}, 1000)[0]["used_gb"], 1.23)
cache = {"WR-A": [1.23, 5.0, 1000]}
check("unchanged within the hour: not sent", ul.due(read, cache, 1000 + 600), [])
check("unchanged after an hour: sent", len(ul.due(read, cache, 1000 + ul.REFRESH_S)), 1)
moved = {"WR-A": {"used_gb": 1.5, "total_gb": 5.0}}
check("moved: sent at once", len(ul.due(moved, cache, 1000 + 60)), 1)
check("a broken cache entry: sent", len(ul.due(read, {"WR-A": "junk"}, 1001)), 1)

print("\npush")


class Resp:
    def __init__(self, body, code=200):
        self.body, self.code = body, code

    def raise_for_status(self):
        if self.code >= 400:
            raise RuntimeError(f"HTTP {self.code}")

    def json(self):
        return self.body


posts = []


def post_ok(url, json=None, headers=None, timeout=None):
    posts.append((url, json, headers))
    return Resp({"ok": True, "updated": [{"order_id": "WR-A", "pct": 25}], "not_found": ["WR-OLD"]})


items = [{"order_id": "WR-A", "used_gb": 1.23, "total_gb": 5.0, "expires": ""},
         {"order_id": "WR-OLD", "used_gb": 1, "total_gb": 2, "expires": ""}]
done, ok = ul.push(items, "tok", post=post_ok)
check("updated objects and not_found ids both count as sent", (sorted(done), ok), (["WR-A", "WR-OLD"], True))
check("usage_batch with a bearer", (posts[0][1]["action"], posts[0][2]["Authorization"]), ("usage_batch", "Bearer tok"))
done, ok = ul.push(items, "tok", post=lambda *a, **k: Resp({}, 502))
check("a failed send: nothing marked, run not ok", (done, ok), ([], False))

print("\nmain (end to end, faked)")


class WS:
    def get_all_values(self):
        return rows_fixture()

    def __getattr__(self, name):
        raise AssertionError(f"usage_live touched the sheet: {name}")


ul.sheet_client = lambda: types.SimpleNamespace(open_by_key=lambda k: types.SimpleNamespace(sheet1=WS()))
ul.env = lambda k: "tok"
fake_st.fetch_usage = fake_s2
os.environ["STELLAR_USAGE_KEY"] = "k-test"
d = tempfile.mkdtemp()
os.chdir(d)
posts.clear()
ul.requests = types.SimpleNamespace(post=post_ok)
ul.push.__defaults__ = (post_ok,)
check("first run ok", ul.main(), 0)
check("one site call", len(posts), 1)
check("both live readings sent", sorted(i["order_id"] for i in posts[0][1]["items"]), ["WR-A", "WR-D"])
posts.clear()
check("second run ok", ul.main(), 0)
check("WR-A cached: only WR-D (the site did not confirm it) is sent again",
      sorted(i["order_id"] for i in posts[0][1]["items"]) if posts else [], ["WR-D"])
check("the module has no sheet write", any(w in open(os.path.join(os.path.dirname(os.path.abspath(ub.__file__)), "usage_live.py")).read()
                                           for w in ("update_cells", "sheet1.update", "append_row", "batch_update", "update_acell")), False)

print()
if _fails:
    print(f"{len(_fails)} FAILED: {', '.join(_fails)}")
    sys.exit(1)
print("all passed")
