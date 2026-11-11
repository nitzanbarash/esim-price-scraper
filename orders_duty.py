#!/usr/bin/env python3
"""
Orders duty -- the network's turn at the two jobs GitHub Actions ran alone
until 2026-11-12: buying the paid s2 orders (stellar_buyer) and the delivery
mail bot (fulfillment_bot). Owner, 2026-11-12: "the computers buy and send;
GitHub is the backup".

One computer of the network holds this duty at a time. The network's main
hands it out like any other job (net/jobs.py 'orders', its own lane, so a
price sweep on the same computer does not make an order wait), the job is
fenced like any other, and a run lasts RUN_S and ends by itself; the next one
starts at once, on whichever computer is free. So "who buys" is never two
computers, and a computer that dies is replaced within a run.

How it hears of an order (owner's choice, 2026-11-12):

  * THE BELL. The site rings a notification topic the moment an order is paid
    (waverole-site api/_fulfil.js, env NET_BELL_URL); this job holds an
    OUTGOING connection to that topic (ORDERS_BELL_URL in the vault). Nothing
    listens on the computer and no port is opened. A ring carries one supplier
    code and nothing else - no order, no customer, no secret - and all it can
    make this job do is ASK THE SITE, with the same token as ever. A forged
    ring costs one look at the queue.
  * THE LOOK. Bell or no bell, the queue is read every POLL_S (150 s): a ring
    that was lost is an order that waits two and a half minutes, not for ever.

GitHub stays behind both: stellar-buy.yml is still woken by every payment,
gives this duty a short head start when the network says it is alive, and
buys whatever nobody has claimed (the site's claim is first come, first
served, and the two buyers claim under different names: stellar_buyer.WORKER).

State kept between runs on this computer (a small file in the temp folder):
when the queue was last read, when the mail bot last ran, how long the "an
order is on its way" quick rhythm lasts, and the time of the last ring heard
(so a ring that fell between two runs is replayed by the topic). Losing the
file costs one early look.

Secrecy: the logs stay on the computer, and still hold order numbers only -
no address, no code, no link, and never the bell's address (it is the only
thing that keeps strangers from ringing it).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time

try:
    import net_fence
    net_fence.install()
except ImportError:          # run outside the network (by hand)
    pass

import requests

log = logging.getLogger("orders-duty")

RUN_S = float(os.getenv("ORDERS_DUTY_RUN_S", "240"))        # one run; the network starts the next
POLL_S = float(os.getenv("ORDERS_POLL_S", "150"))           # the look, with or without a ring
FULFIL_S = float(os.getenv("ORDERS_FULFIL_S", "300"))       # the mail bot's quiet rhythm
HOT_FULFIL_S = float(os.getenv("ORDERS_HOT_FULFIL_S", "45"))  # ...and while an order is on its way
HOT_FOR_S = float(os.getenv("ORDERS_HOT_FOR_S", "600"))
# After a ring the queue is read at once, and again after each of these: the
# bell is rung beside the queue write, not after it, and may win the race.
RING_RELOOK_S = (3.0, 8.0)
RING_MIN_GAP_S = 5.0          # rings closer than this are one ring (a forged storm buys nothing)
BELL_URL = (os.getenv("ORDERS_BELL_URL") or "").strip()
S2 = "s2"                     # the ring's word for an order this duty buys itself
STATE = os.path.join(tempfile.gettempdir(), "wr_orders_duty.json")


# -- state between runs ----------------------------------------------------

def load_state(path: str = STATE) -> dict:
    try:
        with open(path, "r", encoding="ascii") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(d: dict, path: str = STATE):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="ascii") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except OSError:
        pass                  # a lost state file is one early look, never an error


def _num(d: dict, k: str) -> float:
    v = d.get(k)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


# -- the bell --------------------------------------------------------------

class Bell:
    """The outgoing connection. rings() hands back the supplier codes heard
    since the last call ('' for a ring that names none)."""

    def __init__(self, url: str, since: float = 0.0, get=None):
        self.url = url.rstrip("/")
        self.since = float(since or 0)
        self.get = get or requests.get
        self.event = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.heard: list = []
        self.thread = None

    def start(self):
        if not self.url:
            return self
        self.thread = threading.Thread(target=self._listen, daemon=True, name="orders-bell")
        self.thread.start()
        return self

    def _listen(self):
        while not self.stop.is_set():
            try:
                params = {"since": str(int(self.since))} if self.since > 0 else {}
                with self.get(self.url + "/json", params=params, stream=True, timeout=(10, 120)) as r:
                    if r.status_code != 200:
                        raise OSError(f"bell answered {r.status_code}")
                    for line in r.iter_lines():
                        if self.stop.is_set():
                            return
                        self.feed(line)
            except Exception as ex:                          # noqa: BLE001
                # The type only: a requests error text carries the address.
                log.info(f"bell connection ended ({type(ex).__name__}) -- reconnecting")
            self.stop.wait(5)

    def feed(self, line):
        """One line of the topic's stream. Anything that is not a message
        (open, keepalive, garbage) is nothing."""
        if not line:
            return False
        try:
            ev = json.loads(line if isinstance(line, str) else line.decode("utf-8", "replace"))
        except ValueError:
            return False
        if not isinstance(ev, dict) or ev.get("event") != "message":
            return False
        t = ev.get("time")
        if isinstance(t, (int, float)) and not isinstance(t, bool) and t >= self.since:
            self.since = float(t) + 1      # replay from after this one, not from it
        word = str(ev.get("message") or "").strip().lower()[:8]
        with self.lock:
            self.heard.append(word if word in ("s1", S2) else "")
        self.event.set()
        return True

    def rings(self) -> list:
        with self.lock:
            out, self.heard = self.heard, []
            self.event.clear()
        return out

    def wait(self, seconds: float) -> bool:
        return self.event.wait(max(0.0, seconds))

    def close(self):
        self.stop.set()


# -- the two jobs ----------------------------------------------------------

def buyer_tick() -> int:
    """One look at the s2 queue; buys what is there. Never raises."""
    import stellar_buyer as sb
    try:
        return int(sb.run() or 0)
    except Exception as ex:                                  # noqa: BLE001
        log.error(f"buyer tick failed: {type(ex).__name__}")
        try:
            sb.alert_hourly("buyer crashed (network)", f"{type(ex).__name__}: {ex}")
        except Exception:                                    # noqa: BLE001
            pass
        return 0


def ready_to_buy() -> bool:
    """Can THIS computer buy right now: a key the supplier accepts, a wallet
    with money in it, a receipts sheet it can open?

    Asked once at the start of every run, because the rule that keeps the
    backup useful is NEVER CLAIM WHAT YOU CANNOT BUY: a claim holds an order
    for an hour, and GitHub - whose key may be fine - is told "not yours". A
    computer that is not ready ends its run as FAILED instead; the network
    gives the duty to the other computer first, and an order nobody claims is
    bought by GitHub twenty seconds later. (stellar_buyer.preflight mails the
    owner about a missing or refused key, at most hourly.)"""
    import stellar_buyer as sb
    try:
        if not sb.preflight():
            return False
        sb.fb.sheet_client().open_by_key(sb.fb.RECEIPTS_SHEET_ID)
        return True
    except Exception as ex:                                  # noqa: BLE001
        log.error(f"not ready to buy: {type(ex).__name__}")
        return False


def fulfil_tick():
    """One run of the delivery-mail bot. Never raises: its own SystemExit(1)
    (a mailbox that needs a person - it has mailed the owner) ends the tick,
    not the duty."""
    import fulfillment_bot as fb
    try:
        fb.main()
    except SystemExit:
        log.error("mail bot: the mailbox needs a person (it has said so by mail)")
    except Exception as ex:                                  # noqa: BLE001
        log.error(f"mail bot tick failed: {type(ex).__name__}")


# -- the loop --------------------------------------------------------------

def duty(bell, state: dict, *, buyer=buyer_tick, fulfil=fulfil_tick, clock=time.time,
         mono=time.monotonic, save=save_state, run_s: float = None) -> dict:
    """Run for run_s, then return the state for the next run. `bell` needs
    wait(seconds) -> bool and rings() -> [codes]."""
    run_s = RUN_S if run_s is None else run_s
    t0 = mono()
    last_poll, last_fulfil = _num(state, "last_poll"), _num(state, "last_fulfil")
    hot_until, last_ring = _num(state, "hot_until"), 0.0
    relook: list = []            # wall times of the looks a ring still owes
    stats = {"polls": 0, "bought": 0, "rings": 0, "mail_runs": 0}

    def remember():
        save({"last_poll": last_poll, "last_fulfil": last_fulfil, "hot_until": hot_until,
              "bell_since": getattr(bell, "since", 0.0)})

    while mono() - t0 < run_s:
        now = clock()
        # A clock set back must not hold the duty still: a time "in the future" is no time.
        if last_poll > now + 5:
            last_poll = 0.0
        if last_fulfil > now + 5:
            last_fulfil = 0.0
        codes = bell.rings()
        look = False
        if codes and now - last_ring >= RING_MIN_GAP_S:
            last_ring = now
            stats["rings"] += 1
            hot_until = now + HOT_FOR_S          # an order is on its way: the mail bot keeps close
            if any(c in (S2, "") for c in codes):
                look = True
                relook = [now + d for d in RING_RELOOK_S]
        if relook and now >= relook[0]:
            relook.pop(0)
            look = True
        if now - last_poll >= POLL_S:
            look = True
        if look:
            last_poll = now
            remember()                            # before the work: a run killed mid-purchase still looked
            got = buyer()
            stats["polls"] += 1
            stats["bought"] += got
            if got:
                relook = []                       # the ring was answered
                hot_until = max(hot_until, clock() + HOT_FOR_S)
            continue                              # the clock moved: read it again before the mail bot
        every = HOT_FULFIL_S if now < hot_until else FULFIL_S
        if now - last_fulfil >= every:
            last_fulfil = now
            remember()
            fulfil()
            stats["mail_runs"] += 1
            continue
        due = [last_poll + POLL_S, last_fulfil + every] + relook[:1]
        left = run_s - (mono() - t0)
        bell.wait(max(0.2, min(min(due) - now, left)))
    remember()
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    if not ready_to_buy():
        # The mail bot needs neither the supplier's key nor its wallet: one
        # run of it before handing the duty back, so the eSIM mails keep
        # going out at the network's retry rhythm while this is sorted out.
        log.error("this computer cannot buy right now -- the duty is handed back (nothing was claimed)")
        fulfil_tick()
        return 1
    state = load_state()
    bell = Bell(BELL_URL, since=_num(state, "bell_since")).start()
    if not BELL_URL:
        log.warning(f"no bell address in the vault (ORDERS_BELL_URL) -- the queue is read every {POLL_S:.0f}s only")
    try:
        stats = duty(bell, state)
    finally:
        bell.close()
    log.info(f"duty done: {stats['polls']} look(s) at the queue, {stats['bought']} bought, "
             f"{stats['rings']} ring(s), {stats['mail_runs']} mail-bot run(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
