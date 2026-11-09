"""
s1 load guard (owner, 2026-11-09): how hard this computer may lean on the
first supplier's site, and what it does the moment the site pushes back.

PER COMPUTER, on purpose. The owner's rule: the safe load is the supplier's
limit times the number of computers, because each computer will sit on its
own internet line ("build it as if they are already split"). So every limit
here is one computer's own:

  * a pace: at most MAX_PER_MIN page loads a minute from this process
    (ESIMDOG_MAX_PER_MIN, default 12 - about what one browser has done for
    weeks without a single refusal, 2026-11-09 audit);
  * a pause: the first refusal (HTTP 403/429, or a challenge / rate-limit
    page) stops EVERY s1 job on this computer for an hour, the next within
    six hours for two, then four, at most six. The pause lives in a file in
    the job's working folder (the network's work\\ folder is the same for
    all jobs on one computer), so the price sweep, the usage sweep and the
    live usage all see it;
  * one mail to the owner when a pause starts (at most three a day), when
    the job holds GMAIL_APP_PASSWORD.

While two computers still share one line, the second one simply meets its own
refusal and pauses itself too - one extra request, then silence.

Stdlib only: the price sweep imports this without the sheet libraries.
This file is public: it names the supplier by code, never by name.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

log = logging.getLogger("s1-guard")

PAUSE_FILE = "esimdog_pause.json"
PAUSE_S = 3600
MAX_PAUSE_S = 6 * 3600
STRIKE_WINDOW_S = 6 * 3600       # a refusal within this of the last one doubles the pause
MAILS_PER_DAY = 3

REFUSAL_STATUS = (403, 429)
REFUSAL_WORDS = (
    "too many requests", "rate limit", "rate-limit", "access denied",
    "request blocked", "you have been blocked", "attention required",
    "just a moment", "verify you are human", "are you a robot",
    "unusual traffic",
)


def _max_per_min() -> int:
    try:
        return max(1, min(int(os.getenv("ESIMDOG_MAX_PER_MIN", "") or 12), 60))
    except ValueError:
        return 12


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict, path: str) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f"s1 pause not saved ({type(e).__name__})")


def paused(now: float | None = None, path: str = PAUSE_FILE) -> float:
    """Seconds this computer must still leave s1 alone (0 = free)."""
    now = time.time() if now is None else now
    try:
        until = float(_load(path).get("until") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, until - now)


def is_refusal(status=None, text: str = "") -> bool:
    """A 403/429, or a page whose title/text is a block or challenge page."""
    try:
        if status is not None and int(status) in REFUSAL_STATUS:
            return True
    except (TypeError, ValueError):
        pass
    t = str(text or "").lower()
    return any(w in t for w in REFUSAL_WORDS)


def refused(reason: str, now: float | None = None, path: str = PAUSE_FILE) -> dict:
    """Record a refusal: start (or lengthen) this computer's pause.

    Returns {"until", "strikes", "pause_s", "new"}; new = it was not paused.
    """
    now = time.time() if now is None else now
    d = _load(path)
    try:
        last = float(d.get("last") or 0)
        strikes = int(d.get("strikes") or 0)
        until = float(d.get("until") or 0)
    except (TypeError, ValueError):
        last, strikes, until = 0.0, 0, 0.0
    new = until <= now
    if new:
        strikes = strikes + 1 if now - last < STRIKE_WINDOW_S else 1
    pause = min(PAUSE_S * 2 ** (strikes - 1), MAX_PAUSE_S)
    until = max(until, now + pause)
    d.update({"until": until, "last": now, "strikes": strikes,
              "reason": str(reason or "")[:120]})
    _save(d, path)
    log.warning(f"s1 refused this computer ({str(reason or '')[:80]}): no s1 calls "
                f"for {pause // 60:.0f} min (strike {strikes})")
    return {"until": until, "strikes": strikes, "pause_s": pause, "new": new}


def notify(info: dict, where: str, now: float | None = None, path: str = PAUSE_FILE) -> bool:
    """One mail per new pause, at most MAILS_PER_DAY a day. True if sent."""
    if not info.get("new") or not os.getenv("GMAIL_APP_PASSWORD", "").strip():
        return False
    now = time.time() if now is None else now
    d = _load(path)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    sent = d.get("mails") if d.get("mail_day") == day else 0
    if (sent or 0) >= MAILS_PER_DAY:
        return False
    try:
        from bot_common import alert
    except Exception:
        return False
    host = os.getenv("COMPUTERNAME", "") or os.getenv("HOSTNAME", "") or "this computer"
    alert("s1 refused one of our computers",
          f"{host}: the s1 site refused a request during {where} "
          f"({d.get('reason', '')}).\n\nThis computer makes no s1 calls for "
          f"{info['pause_s'] // 60:.0f} minutes (refusal {info['strikes']} in a row). "
          "Prices and usage from s1 wait on this computer; the others go on.\n\n"
          "If this repeats, the load from this internet line is too high.")
    d.update({"mail_day": day, "mails": (sent or 0) + 1})
    _save(d, path)
    return True


class Pacer:
    """Spaces this process's s1 page loads to MAX_PER_MIN a minute."""

    def __init__(self, per_min: int | None = None, clock=time.monotonic):
        self.gap = 60.0 / (per_min or _max_per_min())
        self.clock = clock
        self.next = 0.0
        self._lock = threading.Lock()

    def _take(self) -> float:
        """Reserve the next slot; returns how long to wait for it."""
        with self._lock:
            now = self.clock()
            at = max(now, self.next)
            self.next = at + self.gap
            return at - now

    def wait(self, sleep=time.sleep) -> None:
        w = self._take()
        if w > 0:
            sleep(w)

    async def wait_async(self) -> None:
        w = self._take()
        if w > 0:
            await asyncio.sleep(w)
