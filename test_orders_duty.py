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
    def __init__(self, rings=(), bought_at=(), look_answers=None, mail_answers=None):
        self.look_answers = list(look_answers or [])     # forced answers of the first looks
        self.mail_answers = list(mail_answers or [])     # ...and of the first mail-bot runs
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
        if self.look_answers:
            return self.look_answers.pop(0)
        return 1 if len(self.looks) in self.bought_at else 0

    def fulfil(self):
        self.mails.append(round(self.t - self.t0, 1))
        self.t += 2.0
        return self.mail_answers.pop(0) if self.mail_answers else True

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

print("\n4. the look that buys looks once more at once (the queue may hold more), then rests")
w = World(rings=[(40, "s2")], bought_at={1})
now = w.t
st = w.run(state={"last_poll": now - 10, "last_fulfil": now - 10}, run_s=100)
check("two looks: the one that bought and the one that found nothing left", [round(x) for x in w.looks] == [40, 41], str(w.looks))
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
boom.TROUBLE = {"key": False}
sys.modules["stellar_buyer"] = boom
check("buyer_tick swallows it and says 'the look failed'", od.buyer_tick() == od.LOOK_FAILED)
boom.run = lambda: 2
check("a look that bought two says 2", od.buyer_tick() == 2)
boom.TROUBLE["key"] = True
check("a key the supplier refused on an order is said, whatever the count", od.buyer_tick() == od.KEY_REFUSED)
fbm = types.ModuleType("fulfillment_bot")
fbm.main = lambda: (_ for _ in ()).throw(SystemExit(1))
sys.modules["fulfillment_bot"] = fbm
try:
    check("fulfil_tick swallows the mail bot's SystemExit and says it failed", od.fulfil_tick() is False)
except SystemExit:
    check("fulfil_tick swallows the mail bot's SystemExit and says it failed", False)
fbm.main = lambda: None
check("a mail-bot run that ended by itself is a success", od.fulfil_tick() is True)
fbm.main = lambda: (_ for _ in ()).throw(ValueError("x"))
check("a crash of the mail bot is a failure, not an exception", od.fulfil_tick() is False)

print("\n10. a computer that cannot buy hands the duty back without touching the queue")
calls = []
notready = types.ModuleType("stellar_buyer")
notready.preflight = lambda: False
notready.run = lambda: calls.append("run") or 0
notready.alert_hourly = lambda *a, **k: None
notready.TROUBLE = {"key": False}
sys.modules["stellar_buyer"] = notready
mailruns = []
fbm.main = lambda: mailruns.append(1)
MEM = {}                                    # the state file, in memory
od.load_state = lambda: dict(MEM)
od.save_state = lambda d: (MEM.clear(), MEM.update(d))
od.ready_to_mail = lambda: True
check("ready_to_buy says no", od.ready_to_buy() is False)
check("main ends as failed", od.main() == 1)
check("...and never read the queue", calls == [], str(calls))
check("...but the mail bot still had its run", mailruns == [1], str(mailruns))
check("...and the next run knows this one failed", MEM.get("failed") is True, str(MEM))
ok = types.ModuleType("stellar_buyer")
ok.preflight = lambda: True
ok.fb = types.SimpleNamespace(RECEIPTS_SHEET_ID="x", sheet_client=lambda: types.SimpleNamespace(
    open_by_key=lambda k: (_ for _ in ()).throw(RuntimeError("sheets down"))))
sys.modules["stellar_buyer"] = ok
check("a receipts sheet that cannot be opened is 'not ready' too", od.ready_to_buy() is False)
ok.fb = types.SimpleNamespace(RECEIPTS_SHEET_ID="x", sheet_client=lambda: types.SimpleNamespace(open_by_key=lambda k: object()))
ok.pending_orders = lambda: (_ for _ in ()).throw(RuntimeError("401"))
check("a site that refuses this computer's token is 'not ready' too", od.ready_to_buy() is False)
ok.pending_orders = lambda: []
check("key, wallet, sheet and queue all there: ready", od.ready_to_buy() is True)

print("\n11. a computer that cannot MAIL claims nothing either (a green run would silence the backup)")
ok.run = lambda: calls.append("run") or 0
ok.TROUBLE = {"key": False}
ok.alert_hourly = lambda *a, **k: None
MEM.clear(); calls.clear(); mailruns.clear()
od.ready_to_mail = lambda: False
check("main ends as failed", od.main() == 1)
check("...never read the queue, never ran the mail bot", calls == [] and mailruns == [], f"{calls} {mailruns}")

print("\n12. after a failed run the mail bot must prove itself BEFORE anything is claimed")
od.ready_to_mail = lambda: True
MEM.clear(); MEM["failed"] = True; calls.clear()
fbm.main = lambda: (_ for _ in ()).throw(SystemExit(1))
check("the mail bot still fails: the run ends as failed", od.main() == 1)
check("...and nothing was claimed", calls == [], str(calls))

print("\n13. the mail bot failing inside a run: one retry soon, then the duty is handed back")
w = World(mail_answers=[False, False])
st = w.run(state={"last_poll": w.t - 10}, run_s=240)
check("two mail-bot runs, 30 s apart", len(w.mails) == 2 and 28 <= w.mails[1] - w.mails[0] <= 34, str(w.mails))
check("the run ended early, as failed", st["failed"] == "mail" and w.mono() < 60, f"{st} {w.mono()}")
check("no look at the queue while the mail half was in doubt", w.looks == [], str(w.looks))
check("the saved state says failed", w.saved[-1].get("failed") is True, str(w.saved[-1]))
w = World(mail_answers=[False, True])
st = w.run(state={"last_poll": w.t - 10}, run_s=240)
check("a mail bot that recovers on the retry: the run goes on and ends well",
      st["failed"] == "" and len(w.looks) >= 1 and w.saved[-1].get("failed") is False, f"{st} {w.looks}")

print("\n13b. a mail failure is never forgotten")
w = World(rings=[(1, "s1")], mail_answers=[False, False])
st = w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 44, "hot_until": w.t + 20}, run_s=240)
check("the retry comes 30 s later even when the quick rhythm ended meanwhile",
      len(w.mails) == 2 and 28 <= w.mails[1] - w.mails[0] <= 34 and st["failed"] == "mail", f"{w.mails} {st}")
w = World(mail_answers=[False])
st = w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 290}, run_s=25)
check("a failure in the run's last seconds ends the run as failed (the next run proves the mail bot first)",
      st["failed"] == "mail" and w.saved[-1].get("failed") is True, f"{w.mails} {st}")

print("\n13c. only a REAL purchase earns an immediate second look, and not for ever")
w = World(bought_at=set(range(1, 100)))
st = w.run(state={"last_fulfil": w.t - 10}, run_s=100)
check(f"a buyer that buys every time: {od.QUICK_LOOKS_MAX + 1} looks in a row, then the usual rhythm",
      len(w.looks) == od.QUICK_LOOKS_MAX + 1, str(w.looks))
real = types.ModuleType("stellar_buyer")
real.run = lambda: 1                        # run() counted an order it could NOT place (wallet short, busy)
real.TROUBLE = {"key": False, "placed": 0}
real.alert_hourly = lambda *a, **k: None
sys.modules["stellar_buyer"] = real
check("an order that could not be placed is not 'bought' (no back-to-back looks at a busy supplier)", od.buyer_tick() == 0)
real.TROUBLE["placed"] = 1
check("a placed order is", od.buyer_tick() == 1)
sys.modules["stellar_buyer"] = ok

print("\n14. the supplier refused OUR key on an order: stop claiming, for a while")
w = World(look_answers=[od.KEY_REFUSED])
st = w.run(state={"last_fulfil": w.t - 10}, run_s=240)
check("one look, then the run ends as failed", len(w.looks) == 1 and st["failed"] == "key", f"{w.looks} {st}")
check("the state holds the buying back for 15 minutes",
      880 <= w.saved[-1]["no_buy_until"] - w.t <= 900, str(w.saved[-1]))
MEM.clear(); MEM.update(w.saved[-1]); MEM["no_buy_until"] = 4e9; calls.clear()
fbm.main = lambda: None
check("the next run does not claim (and ends as failed)", od.main() == 1 and calls == [], str(calls))

print("\n15. a queue that cannot be read three times in a row ends the run as failed")
w = World(rings=[(5, "s2")], look_answers=[od.LOOK_FAILED] * 3)
st = w.run(state={"last_fulfil": w.t - 10}, run_s=240)
check("three looks, failed", len(w.looks) == 3 and st["failed"] == "look", f"{w.looks} {st}")
w = World(rings=[(5, "s2")], look_answers=[od.LOOK_FAILED, 0])
st = w.run(state={"last_fulfil": w.t - 10}, run_s=60)
check("one bad look followed by a good one is nothing", st["failed"] == "", str(st))

print("\n16. a stranger with the bell's address: bounded work")
w = World(rings=[(1 + i * 6, "s2") for i in range(39)])
st = w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 10}, run_s=240)
check(f"at most {od.RING_BUDGET} rings are answered in a run", st["rings"] == od.RING_BUDGET, str(st))
check("...so the looks are bounded too", len(w.looks) <= od.RING_BUDGET * 3 + 2, str(len(w.looks)))
w = World(rings=[(5, "garbage that became ''"), (20, "")])
w.rings_at = [(at, "") for at, _ in w.rings_at]
w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 10}, run_s=200)
check("a ring that names no supplier does not quicken the mail bot", w.mails == [], str(w.mails))
w = World(rings=[(5, "s1")])
day = int(w.t // 86400)
w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 10, "hot_day": day, "hot_n": od.HOT_RINGS_A_DAY}, run_s=200)
check("...and after the day's share of rings, neither does a named one", w.mails == [], str(w.mails))

print("\n17. the bell cannot be silenced or blown up by what it is sent")
import time as _time
b = od.Bell("https://bell.invalid/topic", since=100)
b.feed(json.dumps({"event": "message", "time": 1e308 * 10, "message": "s2"}))
b.feed(json.dumps({"event": "message", "time": _time.time() + 10**7, "message": "s2"}))
check("a time from the far future (or infinity) is a ring but is not remembered", b.rings() == ["s2", "s2"] and b.since == 100, str(b.since))
check("a line longer than the limit is nothing", b.feed(b"x" * (od.BELL_LINE_MAX + 1)) is False)
stream = types.SimpleNamespace(iter_content=lambda chunk_size=0: iter([b'{"a":1}\n{"b"', b':2}\n', b"tail"]))
check("the stream is cut into its lines", list(od.Bell._lines(stream)) == [b'{"a":1}', b'{"b":2}'])
endless = types.SimpleNamespace(iter_content=lambda chunk_size=0: iter([b"y" * 4096] * 5 + [b'yy\n{"c":3}\n']))
check("an over-long line is skipped and the next one is read (no reconnect, no replay of it)",
      list(od.Bell._lines(endless)) == [b'{"c":3}'], str(list(od.Bell._lines(endless)))[:80])
check("a state with a number that is no number reads as 0", od._num({"k": float("inf")}, "k") == 0.0)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("all checks passed")
