#!/usr/bin/env python3
"""
Live usage (owner, 2026-11-08): the customer's order page shows how much of
the package is used within about 10-15 minutes, not 4-7 hours.

Runs on the update network's computers (waverole-net job `usage_live`, every
10-15 minutes). It does ONE thing: reads the live packages, asks each
supplier for their meter, and sends the readings to the site (usage_batch).

What it never does, on purpose:
  * write the receipts sheet: the 4-hourly sweep (usage_bot.py, GitHub
    Actions) stays the sheet's only writer, with its Status, activation,
    discount and refund columns. Two writers on one sheet would overwrite
    each other's cells.
  * mail anyone. The 90/98/100% notices are sent by the site, once per
    order (alertNN_sent on the order), whoever brings the reading.
  * retire a package or judge one "finished": a package that does not
    answer is simply not sent (usage_bot decides that, with the sheet).

Which packages: the receipts rows usage_bot still checks (Status blank or
active), with the supplier read the same way (usage_bot.decide_source; a row
whose two supplier columns disagree is skipped, as there).

Suppliers (codes, not names: this file is public):
  s1  by ICCID, ten per request (bot_common.fetch_usage)
  s2  by the order link on the row, two calls per package, paced
      (stellar_usage.fetch_usage). Needs STELLAR_USAGE_KEY: a key with read
      rights only (orders:read + esims:read). Without it s2 rows are skipped
      and s1 still runs. STELLAR_RATE_LIMIT (default 30 a minute here) keeps
      room under the supplier's 60 a minute for the cloud jobs that share it.

Cost on the site: only a reading that MOVED is sent, plus every package once
an hour (so the page's "updated" time never goes stale by more than that).
The memory of what was sent lives in usage_live_cache.json in the job's
working folder; losing it only means one full send.

Environment:
  GOOGLE_CREDENTIALS_JSON  read access to the receipts sheet
  ORDERS_TOKEN             bearer token of waverole.com/api/orders
  STELLAR_USAGE_KEY        optional, see s2
Exit 0 when the run finished (even with some packages unread), 1 when the
sheet or the site could not be reached at all.
"""

import json
import logging
import os
import re
import sys
import time

import requests

from bot_common import ORDERS_URL, RECEIPTS_SHEET_ID, env, fetch_usage, sheet_client, _redact

log = logging.getLogger("usage_live")

CACHE = "usage_live_cache.json"
REFRESH_S = 3600          # every package is sent at least this often
SITE_CHUNK = 100
S1_CHUNK = 50             # fetch_usage splits further into the supplier's own batch size
DEFAULT_S2_RATE = 30


def _load_cache(path=CACHE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(d: dict, path=CACHE) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f"cache not saved ({type(e).__name__}): the next run sends everything once")


def live_rows(rows: list[list[str]]) -> tuple[list[dict], int]:
    """(packages to read, rows skipped for disagreeing supplier columns)."""
    import usage_bot as ub
    if not rows:
        return [], 0
    hdr = [h.strip() for h in rows[0]]
    idx = {c: hdr.index(c) for c in hdr}
    for c in (ub.COL_ORDER, ub.COL_STATUS):
        if c not in idx:
            raise RuntimeError(f"receipts sheet: column {c!r} is missing")
    cell = lambda r, c: (r[idx[c]].strip() if c in idx and len(r) > idx[c] else "")
    route_known = ub.COL_ROUTE in idx
    out, mismatched = [], 0
    for r in rows[1:]:
        oid = cell(r, ub.COL_ORDER)
        if not oid or cell(r, ub.COL_STATUS) not in ub.STILL_CHECK:
            continue
        if route_known:
            source, verdict = ub.decide_source(cell(r, ub.COL_SOURCE), cell(r, ub.COL_ROUTE))
        else:
            source, verdict = (ub.SRC_STELLAR if cell(r, ub.COL_SOURCE).strip().lower() == ub.SRC_STELLAR.lower()
                               else ub.SRC_ESIMDOG), "ok"
        if verdict == "mismatch":
            mismatched += 1
            continue
        out.append({"order_id": oid, "s2": source == ub.SRC_STELLAR,
                    "iccid": re.sub(r"\D", "", cell(r, ub.COL_ICCID)), "link": cell(r, ub.COL_LINK)})
    return out, mismatched


def readings(todo: list[dict], s2_key: str) -> dict:
    """{order_id: {used_gb, total_gb, expires}} for every package that answered."""
    out = {}
    s1 = [t for t in todo if not t["s2"] and t["iccid"]]
    got = {}
    iccids = [t["iccid"] for t in s1]
    for i in range(0, len(iccids), S1_CHUNK):
        got.update(fetch_usage(iccids[i:i + S1_CHUNK]))
    for t in s1:
        u = got.get(t["iccid"])
        if isinstance(u, dict) and u.get("total_gb", 0) > 0:
            out[t["order_id"]] = u
    log.info(f"s1: {sum(1 for t in s1 if t['order_id'] in out)} of {len(s1)} package(s) read")
    s2 = [t for t in todo if t["s2"] and t["link"]]
    if s2 and not s2_key:
        log.info(f"s2: {len(s2)} package(s) skipped - no STELLAR_USAGE_KEY on this computer")
    elif s2:
        import stellar_usage
        try:
            rate = int(os.getenv("STELLAR_RATE_LIMIT", str(DEFAULT_S2_RATE)))
        except ValueError:
            rate = DEFAULT_S2_RATE
        stellar_usage.RATE_LIMIT = max(1, min(rate, 55))
        st = stellar_usage.fetch_usage([t["link"] for t in s2], key=s2_key)
        for t in s2:
            u = st.get(t["link"])
            # Only a real reading: None / Unknown mean "we could not ask".
            if isinstance(u, dict) and (u.get("total_gb") or 0) > 0:
                out[t["order_id"]] = u
    return out


def due(read: dict, cache: dict, now: float) -> list[dict]:
    """The readings worth sending: moved since the last send, or not sent for an hour."""
    items = []
    for oid, u in sorted(read.items()):
        used = round(float(u.get("used_gb") or 0), 2)
        total = round(float(u.get("total_gb") or 0), 2)
        prev = cache.get(oid)
        if (isinstance(prev, list) and len(prev) == 3 and prev[0] == used and prev[1] == total
                and now - float(prev[2] or 0) < REFRESH_S):
            continue
        items.append({"order_id": oid, "used_gb": used, "total_gb": total, "expires": str(u.get("expires") or "")})
    return items


def push(items: list[dict], token: str, post=requests.post) -> tuple[list[str], bool]:
    """(order ids the site took or no longer knows, every request answered)."""
    done, ok = [], True
    for i in range(0, len(items), SITE_CHUNK):
        chunk = items[i:i + SITE_CHUNK]
        try:
            r = post(ORDERS_URL, json={"action": "usage_batch", "items": chunk},
                     headers={"Authorization": f"Bearer {token}"}, timeout=60)
            r.raise_for_status()
            body = r.json()
            # updated: [{order_id, pct}]; not_found: [order_id]
            done += [str(x.get("order_id") if isinstance(x, dict) else x) for x in (body.get("updated") or [])]
            # An order past the site's record: remember it as sent, or it is retried every run.
            done += [str(x) for x in (body.get("not_found") or [])]
        except Exception as e:
            ok = False
            log.warning(f"site: {len(chunk)} reading(s) not sent - {_redact(str(e))}")
    return done, ok


def main() -> int:
    t0 = time.time()
    rows = sheet_client().open_by_key(RECEIPTS_SHEET_ID).sheet1.get_all_values()
    todo, mismatched = live_rows(rows)
    log.info(f"{len(todo)} live package(s)" + (f", {mismatched} skipped (supplier columns disagree)" if mismatched else ""))
    if not todo:
        return 0
    read = readings(todo, os.getenv("STELLAR_USAGE_KEY", "").strip())
    cache = _load_cache()
    now = time.time()
    items = due(read, cache, now)
    if not items:
        log.info(f"nothing moved; {len(read)} reading(s), {time.time() - t0:.0f}s")
        return 0
    sent, ok = push(items, env("ORDERS_TOKEN"))
    by_id = {it["order_id"]: it for it in items}
    for oid in sent:
        it = by_id.get(oid)
        if it:
            cache[oid] = [it["used_gb"], it["total_gb"], now]
    live = {t["order_id"] for t in todo}
    cache = {k: v for k, v in cache.items() if k in live}      # finished packages drop out
    _save_cache(cache)
    log.info(f"site: {len(sent)} of {len(items)} reading(s) sent ({len(read)} read), {time.time() - t0:.0f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    # Inside the update network: every HTTP request checks the job's fence
    # first (see usage_bot.py). Absent anywhere else.
    try:
        import net_fence
        net_fence.install()
    except ImportError:
        pass
    try:
        sys.exit(main())
    except Exception as e:
        log.error(f"usage_live failed: {type(e).__name__}: {_redact(str(e))}")
        sys.exit(1)
