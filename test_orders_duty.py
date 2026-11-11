#!/usr/bin/env python3
"""Tests for orders_duty.py: the loop's rhythm (ring, look, mail bot) on a
fake clock, and the bell's reading of its stream. No network, no sleeping."""
import json
import sys

import orders_duty as od

fails = []


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


class World:
    """A clock that only moves when the duty waits, and a bell whose rings
    are scheduled on it."""
    def __init__(self, rings=(), bought_at=()):
        self.t = 1_000_000.0
        self.t0 = self.t
        self.rings_at = sorted((self.t0 + at, code) for at, code in rings)
        self.bought_at = set(bought_at)       # look numbers (1-based) that buy one
        self.looks, self.mails, self.saved = [], [], []
        self.since = 0.0

    # clock
    def clock(self):
        return self.t

    def mono(self):
        return self.t - self.t0

    # bell
    def rings(self):
        out = [c for at, c in self.rings_at if at <= self.t]
        self.rings_at = [(at, c) for at, c in self.rings_at if at > self.t]
        return out

    def wait(self, seconds):
        nxt = self.rings_at[0][0] if self.rings_at else None
        if nxt is not None and nxt <= self.t + seconds:
            self.t = max(self.t, nxt)
            return True
        self.t += seconds
        return False

    # jobs
    def buyer(self):
        self.looks.append(round(self.t - self.t0, 1))
        self.t += 1.0
        return 1 if len(self.looks) in self.bought_at else 0

    def fulfil(self):
        self.mails.append(round(self.t - self.t0, 1))
        self.t += 2.0

    def run(self, state=None, run_s=240):
        return od.duty(self, dict(state or {}), buyer=self.buyer, fulfil=self.fulfil, clock=self.clock,
                       mono=self.mono, save=self.saved.append, run_s=run_s)


print("\n1. no ring: one look and one mail-bot run at the start, the next look 150 s later")
w = World()
st = w.run()
check("looks at 0 and ~150", w.looks == [0.0, 150.0], str(w.looks))
check("mail bot at the start only (300 s rhythm)", len(w.mails) == 1 and w.mails[0] <= 2.0, str(w.mails))
check("the run ends by itself near 240 s", 239 <= w.mono() <= 246, str(w.mono()))
check("stats say so", st["polls"] == 2 and st["bought"] == 0 and st["rings"] == 0, str(st))

print("\n2. the state of the run before is honoured: nothing is due, nothing is asked")
w = World()
now = w.t
w.run(state={"last_poll": now - 10, "last_fulfil": now - 10}, run_s=100)
check("no look, no mail-bot run in 100 s", w.looks == [] and w.mails == [], f"{w.looks} {w.mails}")
w = World()
w.run(state={"last_poll": w.t + 9999, "last_fulfil": w.t + 9999}, run_s=20)
check("a time from the future (clock set back) does not hold the duty still",
      len(w.looks) == 1 and len(w.mails) == 1, f"{w.looks} {w.mails}")

print("\n3. a ring for s2: a look at once, two more looks if the queue was still empty")
w = World(rings=[(40, "s2")])
now = w.t
w.run(state={"last_poll": now - 10, "last_fulfil": now - 10}, run_s=120)
check("looks at 40, ~43, ~48", [round(x) for x in w.looks] == [40, 43, 48], str(w.looks))
check("the mail bot moves to its quick rhythm (45 s) for the order on its way",
      len(w.mails) >= 1 and w.mails[0] < 60, str(w.mails))

print("\n4. the look that buys answers the ring: no further looks")
w = World(rings=[(40, "s2")], bought_at={1})
now = w.t
st = w.run(state={"last_poll": now - 10, "last_fulfil": now - 10}, run_s=100)
check("one look", [round(x) for x in w.looks] == [40], str(w.looks))
check("counted as bought", st["bought"] == 1, str(st))

print("\n5. a ring for s1 (the other buyer's order): no look at the s2 queue, quick mail rhythm")
w = World(rings=[(40, "s1")])
now = w.t
w.run(state={"last_poll": now - 10, "last_fulfil": now - 10}, run_s=120)
check("no look", w.looks == [], str(w.looks))
check("mail bot runs soon after", len(w.mails) >= 1 and 40 <= w.mails[0] <= 50, str(w.mails))

print("\n6. a storm of rings is one ring")
w = World(rings=[(40 + i * 0.05, "s2") for i in range(60)])
now = w.t
st = w.run(state={"last_poll": now - 10, "last_fulfil": now - 10}, run_s=70)
check("at most three looks (the ring and its two re-looks)", len(w.looks) <= 3, str(w.looks))

print("\n7. the state is written BEFORE the work")
w = World()
w.run(run_s=5)
check("first save carries the look's time", bool(w.saved) and w.saved[0]["last_poll"] == w.t0, str(w.saved[:1]))

print("\n8. the bell reads its stream")
b = od.Bell("https://bell.invalid/topic", since=100)
check("an open line is nothing", b.feed(json.dumps({"event": "open", "time": 500})) is False)
check("a keepalive is nothing", b.feed(json.dumps({"event": "keepalive", "time": 600})) is False)
check("garbage is nothing", b.feed(b"\xff\xfe not json") is False and b.feed(b"") is False)
check("a message is a ring", b.feed(json.dumps({"event": "message", "time": 700, "message": "s2"}).encode()) is True)
check("the ring names its supplier code", b.rings() == ["s2"])
check("the next connection replays from after it", b.since == 701, str(b.since))
b.feed(json.dumps({"event": "message", "time": 800, "message": "buy WR-ABC123 now for evil@example.com"}))
check("any other text is a ring with no code - nothing in it is used", b.rings() == [""])
check("rings() empties the list", b.rings() == [])
check("no address, no bell thread", od.Bell("").start().thread is None)

print("\n9. a failing job never ends the duty")
import types
boom = types.ModuleType("stellar_buyer")
boom.run = lambda: (_ for _ in ()).throw(RuntimeError("x"))
boom.alert_hourly = lambda *a, **k: None
sys.modules["stellar_buyer"] = boom
check("buyer_tick swallows it and says 0", od.buyer_tick() == 0)
fbm = types.ModuleType("fulfillment_bot")
fbm.main = lambda: (_ for _ in ()).throw(SystemExit(1))
sys.modules["fulfillment_bot"] = fbm
try:
    od.fulfil_tick()
    check("fulfil_tick swallows the mail bot's SystemExit", True)
except SystemExit:
    check("fulfil_tick swallows the mail bot's SystemExit", False)

print("\n10. a computer that cannot buy hands the duty back without touching the queue")
calls = []
notready = types.ModuleType("stellar_buyer")
notready.preflight = lambda: False
notready.run = lambda: calls.append("run") or 0
notready.alert_hourly = lambda *a, **k: None
sys.modules["stellar_buyer"] = notready
mailruns = []
fbm.main = lambda: mailruns.append(1)
check("ready_to_buy says no", od.ready_to_buy() is False)
check("main ends as failed", od.main() == 1)
check("...and never read the queue", calls == [], str(calls))
check("...but the mail bot still had its run", mailruns == [1], str(mailruns))
ok = types.ModuleType("stellar_buyer")
ok.preflight = lambda: True
ok.fb = types.SimpleNamespace(RECEIPTS_SHEET_ID="x", sheet_client=lambda: types.SimpleNamespace(
    open_by_key=lambda k: (_ for _ in ()).throw(RuntimeError("sheets down"))))
sys.modules["stellar_buyer"] = ok
check("a receipts sheet that cannot be opened is 'not ready' too", od.ready_to_buy() is False)
ok.fb = types.SimpleNamespace(RECEIPTS_SHEET_ID="x", sheet_client=lambda: types.SimpleNamespace(open_by_key=lambda k: object()))
check("key, wallet and sheet all there: ready", od.ready_to_buy() is True)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("all checks passed")
