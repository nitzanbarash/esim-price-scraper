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

print("\nshadow runs keep their would-be writes")
os.chdir(tempfile.mkdtemp())
u1 = [{"range": "G12", "values": [["$2.30"]]}, {"range": "I12", "values": [["2026-11-09 20:30 pc-a"]]}]
p = e.save_dry_writes(u1, groups={"gb"}, ts="t1")
check("one file per group", os.path.basename(p or ""), "gb.json")
e.save_dry_writes([{"range": "G13", "values": [["$4.22"]]}], groups={"gb"}, ts="t1")
with open(p) as f:
    d = json.load(f)
check("a run's batches add up", sorted(d["writes"]), ["G12", "G13", "I12"])
e.save_dry_writes([{"range": "G14", "values": [["$1"]]}], groups={"gb"}, ts="t2")
with open(p) as f:
    d = json.load(f)
check("a new run starts over", (d["ts"], sorted(d["writes"])), ("t2", ["G14"]))
check("no group: all.json", os.path.basename(e.save_dry_writes([], groups=set(), ts="x") or ""), "all.json")
check("a strange group name is made safe", os.path.basename(e.save_dry_writes([], groups={"../x"}, ts="x") or ""), "___x.json")

print()
if _fails:
    print(f"{len(_fails)} FAILED: {', '.join(_fails)}")
    sys.exit(1)
print("all passed")
