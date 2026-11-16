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

    def run(self, state=None, run_s=240, **kw):
        return od.duty(self, dict(state or {}), buyer=self.buyer, fulfil=self.fulfil, clock=self.clock,
                       mono=self.mono, save=self.saved.append, run_s=run_s, **kw)


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

print("\n15b. an order that errors fails the run at ONCE - a run has room for two looks only")
w = World(look_answers=[od.ORDER_FAILED])
st = w.run(state={"last_fulfil": w.t - 10}, run_s=240)
check("one look, failed 'order'", len(w.looks) == 1 and st["failed"] == "order", f"{w.looks} {st}")
w = World(look_answers=[od.ORDER_FAILED])
st = w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 10}, run_s=0, min_looks=1)
check("...also the forced look", st["failed"] == "order", str(st))

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

print("\n18. a look whose order ended in an error and placed nothing is a FAILED look (review 2026-11-16)")
errs = types.ModuleType("stellar_buyer")
errs.run = lambda: 1
errs.alert_hourly = lambda *a, **k: None
errs.TROUBLE = {"key": False, "placed": 0, "errors": 1}
sys.modules["stellar_buyer"] = errs
check("errors and nothing placed -> ORDER_FAILED (a success would keep GitHub's backup out)",
      od.buyer_tick() == od.ORDER_FAILED)
errs.TROUBLE = {"key": False, "placed": 1, "errors": 1}
check("errors beside a real purchase -> the purchase counts", od.buyer_tick() == 1)
errs.TROUBLE = {"key": True, "placed": 0, "errors": 1}
check("a refused key is still said first", od.buyer_tick() == od.KEY_REFUSED)
errs.TROUBLE = {"key": False, "placed": 0}
check("a buyer without the count (older copy): as before", od.buyer_tick() == 0)
sys.modules["stellar_buyer"] = ok

print("\n19. a mail-bot run in which no due mail could be sent (the line, not an address) is a failure")
fbm.main = lambda: None
for last, want, label in (({"due": 2, "sent": 0, "failed": 2, "hard_failed": 2}, False, "nothing sent, line failures"),
                          ({"due": 3, "sent": 1, "failed": 2, "hard_failed": 2}, True, "one went out"),
                          ({"due": 2, "sent": 0, "failed": 2, "hard_failed": 0}, True, "only one address's failures"),
                          ({"due": 0, "sent": 0, "failed": 0, "hard_failed": 0}, True, "nothing due"),
                          ({"hard_failed": "x"}, True, "an unreadable count is no verdict"),
                          ("garbage", True, "not a dict is no verdict")):
    fbm.LAST_RUN = last
    check(f"{label} -> {want}", od.fulfil_tick() is want)
del fbm.LAST_RUN
check("no LAST_RUN at all (an older mail bot) -> True", od.fulfil_tick() is True)
fbm.main = lambda: (_ for _ in ()).throw(SystemExit(0))
fbm.LAST_RUN = {"due": 1, "sent": 0, "failed": 1, "hard_failed": 1}
check("a clean SystemExit(0) whose mails all failed on the line -> False", od.fulfil_tick() is False)
del fbm.LAST_RUN
fbm.main = lambda: None

print("\n20. a run whose time went to the checks before it still looks once")
w = World()
st = w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 10}, run_s=0, min_looks=1)
check("run_s 0: one look all the same, and the run ends well", len(w.looks) == 1 and st["polls"] == 1
      and st["failed"] == "", f"{w.looks} {st}")
check("...and the look is saved", w.saved and w.saved[-1]["last_poll"] >= w.t0, str(w.saved[-1:]))
w = World(look_answers=[od.LOOK_FAILED])
st = w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 10}, run_s=0, min_looks=1)
check("...that look failing ends the run as failed (it proved nothing)", st["failed"] == "look", str(st))
w = World(look_answers=[od.KEY_REFUSED])
st = w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 10}, run_s=0, min_looks=1)
check("...a refused key there holds the buying back", st["failed"] == "key"
      and w.saved[-1]["no_buy_until"] > w.t, str(w.saved[-1]))
w = World()
w.run(state={"last_fulfil": w.t - 10}, run_s=240, min_looks=1)
check("a normal run that looked owes nothing extra", w.looks == [0.0, 150.0], str(w.looks))
w = World()
w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 10}, run_s=0)
check("without min_looks (duty's default) nothing changes", w.looks == [], str(w.looks))
w = World(mail_answers=[False, False])
st = w.run(state={"last_poll": w.t - 10}, run_s=60, min_looks=1)
check("a run that ended on the mail half does not look", w.looks == [] and st["failed"] == "mail", f"{w.looks} {st}")

calls.clear(); MEM.clear()
ok.run = lambda: calls.append("run") or 0
ok.TROUBLE = {"key": False}
od.ready_to_mail = lambda: True
_real_duty, _run_s = od.duty, od.RUN_S
od.duty = lambda bell, state, **kw: _real_duty(bell, state, save=lambda d: None, **kw)   # no state file on this machine
od.RUN_S = 0.0
try:
    rc = od.main()
finally:
    od.duty, od.RUN_S = _real_duty, _run_s
check("main: the checks took the whole run, the queue is still read once and the run ends well",
      rc == 0 and calls == ["run"], f"{rc} {calls}")

print("\n21. a hold from a clock set back is never longer than NO_BUY_S")
check("hold_until: nothing saved -> 0", od.hold_until({}, 1000.0) == 0.0)
check("hold_until: a hold inside the window stands", od.hold_until({"no_buy_until": 1500.0}, 1000.0) == 1500.0)
check("hold_until: a far-future hold is cut to now + NO_BUY_S",
      od.hold_until({"no_buy_until": 1e12}, 1000.0) == 1000.0 + od.NO_BUY_S)
w = World()
w.run(state={"last_poll": w.t - 10, "last_fulfil": w.t - 10, "no_buy_until": w.t + 10**7}, run_s=20)
check("duty saves the cut hold", w.t0 < w.saved[-1]["no_buy_until"] <= w.t0 + od.NO_BUY_S, str(w.saved[-1]))
MEM.clear(); MEM["no_buy_until"] = _time.time() + 10**7; calls.clear()
check("main: still held now (nothing claimed)", od.main() == 1 and calls == [], str(calls))
check("...but saved cut, so the hold ends within NO_BUY_S", MEM["no_buy_until"] <= _time.time() + od.NO_BUY_S + 1,
      str(MEM.get("no_buy_until")))

print("\n22. checks that ate the whole run: no late look, handed back")
MEM.clear(); calls.clear()
_rs, _late = od.RUN_S, od.LATE_LOOK_S
od.RUN_S, od.LATE_LOOK_S = -100.0, 0.0
try:
    rc = od.main()
finally:
    od.RUN_S, od.LATE_LOOK_S = _rs, _late
check("main ends failed without a look", rc == 1 and MEM.get("failed") is True, f"{rc} {MEM}")

print("\n23. the keeper keeps an order's things warm while the duty waits - reads only (2026-11-16)")
kb = types.ModuleType("stellar_buyer")
touched = []


class _KWs:
    def row_values(self, i):
        touched.append("sheet")
        return []


class _KSt:
    def wallet_cents(self):
        touched.append("stellar")
        return 1


class _KSite:
    def get(self, url, timeout=None):
        touched.append(("site", url))


kb.WARM = {"ws": _KWs(), "st": _KSt()}
kb.refresh_rows = lambda: touched.append("rows")
sys.modules["stellar_buyer"] = kb
k = od.Keeper()
k.site = _KSite()
k.last = {"rows": 0.0, "site": 0.0, "sheet": 0.0, "stellar": 0.0}
done = k.tick(now=1000.0)
check("everything due is touched once", sorted(done) == ["rows", "sheet", "site", "stellar"], str(done))
check("the site is kept with a static file (the edge answers, no function runs)",
      ("site", od.SITE_PING_URL) in touched and od.SITE_PING_URL.endswith("/robots.txt"), str(touched))
check("5 s later nothing is due", k.tick(now=1005.0) == [])
check("Stellar's connection (it drops an idle one within 30 s) every 12 s", k.tick(now=1013.0) == ["stellar"])
k.busy.set()
touched.clear()
check("nothing at all while an order is in hand", k.tick(now=5000.0) == [] and touched == [], str(touched))
k.busy.clear()
kb.refresh_rows = lambda: (_ for _ in ()).throw(RuntimeError("sheets down"))
check("a failure is quiet, the rest still kept", "rows" not in k.tick(now=9000.0))
k2 = od.Keeper().start(None)
first = k2.thread
k2.start(None)
check("a keeper started again replaces its thread (never two)", first is not k2.thread and k2.stop is not None)
k2.close()

print("\n24. ready_to_buy keeps what it opened, for the run's first order")
wb = types.ModuleType("stellar_buyer")
wb.WARM = {"st": None, "ws": None, "rows": None, "rows_at": 0.0}


class _FakeStellar:
    def __init__(self, key):
        self.key = key


wb.Stellar = _FakeStellar
wb.fb = types.SimpleNamespace(RECEIPTS_SHEET_ID="x", env=lambda n: "k",
                              sheet_client=lambda: types.SimpleNamespace(
                                  open_by_key=lambda k: types.SimpleNamespace(sheet1="WS")))
seen_st = []
wb.preflight = lambda st=None: seen_st.append(st) or True
wb.pending_orders = lambda: []
wb.refresh_rows = lambda: None
sys.modules["stellar_buyer"] = wb
check("ready: key, sheet, queue", od.ready_to_buy() is True)
check("the key was proven on the very client that is kept",
      seen_st and isinstance(seen_st[0], _FakeStellar) and wb.WARM["st"] is seen_st[0], str(seen_st))
check("the receipts sheet is kept", wb.WARM["ws"] == "WS", str(wb.WARM["ws"]))
wb.refresh_rows = lambda: (_ for _ in ()).throw(RuntimeError("x"))
check("a price sheet that will not read ahead is no reason to stop (the buyer reads it at the order)",
      od.ready_to_buy() is True)
wb.preflight = lambda st=None: False
check("a refused key is still 'not ready'", od.ready_to_buy() is False)

print("\n25. warm_up: one kept-alive site connection for the buyer and the mail lease")
hb = types.ModuleType("stellar_buyer")
hb._http = "requests"
hf = types.ModuleType("fulfillment_bot")
hf._http = "requests"
sys.modules["stellar_buyer"], sys.modules["fulfillment_bot"] = hb, hf
site = od.warm_up()
check("both modules' site calls go through the same session",
      site is not None and hb._http is site and hf._http is site, f"{hb._http} {hf._http}")
sys.modules["stellar_buyer"] = ok

print()
if fails:
    print(f"FAILED: {len(fails)} check(s)")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("all checks passed")
