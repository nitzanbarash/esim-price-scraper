"""The s1 load guard (2026-11-09): one computer's pace and pause, and that a
pause really stops every s1 call - prices, usage and eSIM lookups - and never
lets a usage run retire or re-meter an s1 row it could not ask about."""
import asyncio
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())          # the pause file lives in the working folder

import esimdog_guard as g             # noqa: E402

_fails = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        _fails.append(name)


print("refusal detection")
check("429", g.is_refusal(429), True)
check("403", g.is_refusal("403"), True)
check("200 is fine", g.is_refusal(200, "eSIM Germany - prices"), False)
check("a challenge page title", g.is_refusal(200, "Just a moment..."), True)
check("a rate-limit page", g.is_refusal(None, "Too Many Requests"), True)
check("no status, no text", g.is_refusal(None, ""), False)
check("junk status", g.is_refusal("x", ""), False)

print("\npause")
T = 1_000_000.0
check("free at first", g.paused(T), 0.0)
i1 = g.refused("HTTP 429", T)
check("first refusal: one hour", (i1["pause_s"], i1["strikes"], i1["new"]), (3600, 1, True))
check("paused for that hour", g.paused(T + 60), 3540.0)
i2 = g.refused("HTTP 429", T + 120)
check("a refusal during a pause does not stack a strike", (i2["strikes"], i2["new"]), (1, False))
check("...but its end moves out to an hour after it", g.paused(T + 3601), 119.0)
check("free after that", g.paused(T + 120 + 3601), 0.0)
i3 = g.refused("again", T + 4000 + 120)
check("again within six hours: two hours", (i3["pause_s"], i3["strikes"]), (7200, 2))
i4 = g.refused("again", T + 4120 + 7201)
i5 = g.refused("again", T + 4120 + 7201 + 14401)
check("doubling stops at six hours", (i4["pause_s"], i5["pause_s"]), (14400, 21600))
later = T + 10 * 24 * 3600
i6 = g.refused("a week later", later)
check("a refusal long after starts over at one hour", (i6["pause_s"], i6["strikes"]), (3600, 1))
check("a broken file means free, never stuck", g.paused(T, path=os.path.join(tempfile.mkdtemp(), "x.json")), 0.0)
with open("broken.json", "w") as f:
    f.write("{not json")
check("an unreadable file means free", g.paused(T, path="broken.json"), 0.0)

print("\nmail: one per new pause, three a day, only with the mail secret")
sent = []
fake_bc = type(sys)("bot_common")
fake_bc.alert = lambda su, bo: sent.append((su, bo))
with mock.patch.dict(sys.modules, {"bot_common": fake_bc}):
    p = os.path.join(tempfile.mkdtemp(), "p.json")
    with mock.patch.dict(os.environ, {"GMAIL_APP_PASSWORD": ""}):
        check("no secret: no mail", g.notify(g.refused("r", T, p), "x", T, p), False)
    with mock.patch.dict(os.environ, {"GMAIL_APP_PASSWORD": "pw"}):
        p = os.path.join(tempfile.mkdtemp(), "p.json")
        check("a new pause mails", g.notify(g.refused("r", T, p), "the price sweep", T, p), True)
        check("the same pause does not mail again", g.notify(g.refused("r", T + 5, p), "x", T + 5, p), False)
        sent.clear()
        p = os.path.join(tempfile.mkdtemp(), "p.json")
        n = 86400 * 20 + 60                        # early on one UTC day
        for gap in (0, 3601, 7201, 14401):         # four new pauses, ~7 hours
            n += gap
            g.notify(g.refused("r", n, p), "x", n, p)
        check("at most three a day", len(sent), 3)
        check("the mail names the supplier by code only", any("esim.dog" in b.lower() for _, b in sent), False)

print("\npace")
clock = [0.0]
waits = []
pc = g.Pacer(per_min=12, clock=lambda: clock[0])
for _ in range(3):
    pc.wait(sleep=waits.append)
check("12 a minute: 5 s apart", waits, [5.0, 10.0])
clock[0] = 100.0
waits.clear()
pc.wait(sleep=waits.append)
check("after a quiet spell: no wait", waits, [])
with mock.patch.dict(os.environ, {"ESIMDOG_MAX_PER_MIN": "6"}):
    check("ESIMDOG_MAX_PER_MIN sets the pace", g.Pacer().gap, 10.0)
with mock.patch.dict(os.environ, {"ESIMDOG_MAX_PER_MIN": "999"}):
    check("...never above one a second", g.Pacer().gap, 1.0)
apc = g.Pacer(per_min=600)
asyncio.run(apc.wait_async())
check("the async form runs", apc.next > 0, True)

print("\nusage reads stop on a pause and on a refusal")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON", "{}")
import bot_common as bc               # noqa: E402
os.chdir(tempfile.mkdtemp())
calls = []


class R:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._b


def post_429(url, json=None, headers=None, timeout=None):
    calls.append(json)
    return R(429)


with mock.patch.object(bc.requests, "post", post_429), \
     mock.patch.dict(os.environ, {"GMAIL_APP_PASSWORD": ""}):
    out = bc.fetch_usage([str(8900000000000000000 + i) for i in range(25)])
check("a 429: no reading", out, {})
check("...and no second call after it", len(calls), 1)
check("...and this computer is paused", g.paused() > 3000, True)
calls.clear()
with mock.patch.object(bc.requests, "post", post_429):
    check("while paused: usage not asked", bc.fetch_usage(["8901"]), {})
    check("...eSIM lookups not asked", bc._fetch_one_esim("https://x/success?session_id=cs_abc"), {})
check("...not one call", calls, [])

print()
if _fails:
    print(f"{len(_fails)} FAILED: {', '.join(_fails)}")
    sys.exit(1)
print("all passed")
