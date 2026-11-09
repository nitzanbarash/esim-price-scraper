"""The 'last updated' stamp (owner, 2026-11-09): Israel time, then who wrote
it - git (GitHub), the network node's name (pc-a, pc-b), or mac by hand."""
import json
import os
import sys
import tempfile
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import esim_price_scraper as e     # noqa: E402

_fails = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        _fails.append(name)


T = datetime(2026, 11, 9, 20, 30)
clean = {k: v for k, v in os.environ.items() if k not in ("GITHUB_ACTIONS", "NET_FENCE_FILE")}
with mock.patch.dict(os.environ, clean, clear=True):
    check("by hand: mac", e.stamp(T), "2026-11-09 20:30 mac")
with mock.patch.dict(os.environ, dict(clean, GITHUB_ACTIONS="true"), clear=True):
    check("GitHub: git", e.stamp(T), "2026-11-09 20:30 git")
d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, "WaveroleNet"))
with open(os.path.join(d, "WaveroleNet", "config.json"), "w") as f:
    json.dump({"node_id": "pc-b"}, f)
with mock.patch.dict(os.environ, dict(clean, NET_FENCE_FILE="x", LOCALAPPDATA=d), clear=True):
    check("network: the node's name", e.stamp(T), "2026-11-09 20:30 pc-b")
with mock.patch.dict(os.environ, dict(clean, NET_FENCE_FILE="x", LOCALAPPDATA=tempfile.mkdtemp()), clear=True):
    check("network, no config: pc", e.stamp(T), "2026-11-09 20:30 pc")
with open(os.path.join(d, "WaveroleNet", "config.json"), "w") as f:
    json.dump({"node_id": "x" * 40}, f)
with mock.patch.dict(os.environ, dict(clean, NET_FENCE_FILE="x", LOCALAPPDATA=d), clear=True):
    check("a strange node name: pc", e.stamp(T), "2026-11-09 20:30 pc")
check("the date leads, so the stalest-first sort still works",
      sorted(["2026-11-09 20:30 git", "2026-11-08 23:59 pc-a"])[0], "2026-11-08 23:59 pc-a")

print()
if _fails:
    print(f"{len(_fails)} FAILED: {', '.join(_fails)}")
    sys.exit(1)
print("all passed")
