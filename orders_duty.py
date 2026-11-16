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
import math
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
# What a stranger who learned the bell's address can cost (review 2026-11-14):
# a run answers at most RING_BUDGET rings - after that only the POLL_S look
# reads the queue - and rings may quicken the mail bot HOT_RINGS_A_DAY times a
# day at most (a look that really bought always may).
RING_BUDGET = 8
HOT_RINGS_A_DAY = 60
BELL_LINE_MAX = 8192          # a longer line is not a ring: the connection is dropped
# After a look failed this many times in a row, or the mail bot twice, the run
# ends as FAILED: the network then tries the other computer, and GitHub's
# backup sees a duty without a success and does the work.
LOOK_FAILS_MAX = 3
MAIL_FAILS_MAX = 2
MAIL_RETRY_S = 30.0
QUICK_LOOKS_MAX = 5           # looks in a row "because the last one bought", then the usual rhythm
NO_BUY_S = 900.0              # after the supplier refused OUR key on an order
KEY_REFUSED, LOOK_FAILED = -2, -1     # buyer_tick's answers that are not a count
ORDER_FAILED = -3            # an order ended in an error and nothing was placed: the run fails at once
LATE_LOOK_S = 30.0            # past RUN_S + this, main() skips the forced look and hands the run back
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
            json.dump(d, f, allow_nan=False)
        os.replace(tmp, path)
    except (OSError, ValueError):
        pass                  # a lost state file is one early look, never an error


def _num(d: dict, k: str) -> float:
    v = d.get(k)
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    return float(v) if ok else 0.0


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
                    for line in self._lines(r):
                        if self.stop.is_set():
                            return
                        self.feed(line)
            except Exception as ex:                          # noqa: BLE001
                # The type only: a requests error text carries the address.
                log.info(f"bell connection ended ({type(ex).__name__}) -- reconnecting")
            self.stop.wait(5)

    @staticmethod
    def _lines(r):
        """The stream's lines; one longer than BELL_LINE_MAX is skipped (a
        sender that never ends its line would otherwise grow this process
        for ever)."""
        buf, skipping = b"", False
        for chunk in r.iter_content(chunk_size=1024):
            buf += chunk or b""
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if skipping:
                    skipping = False      # the tail of the over-long line
                else:
                    yield line
            if len(buf) > BELL_LINE_MAX:
                # Skipped, not a reason to reconnect: the topic would replay
                # the same line and the bell would stay deaf.
                buf, skipping = b"", True

    def feed(self, line):
        """One line of the topic's stream. Anything that is not a message
        (open, keepalive, garbage) is nothing."""
        if not line or len(line) > BELL_LINE_MAX:
            return False
        try:
            ev = json.loads(line if isinstance(line, str) else line.decode("utf-8", "replace"))
        except ValueError:
            return False
        if not isinstance(ev, dict) or ev.get("event") != "message":
            return False
        t = ev.get("time")
        # A time from the future (or one that is no number at all) would be
        # remembered as "replay from then" and silence the bell for good.
        if (isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
                and self.since <= t <= time.time() + 60):
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
    """One look at the s2 queue; buys what is there. Never raises. Returns
    how many were bought, or LOOK_FAILED (the queue could not be read, the
    buyer crashed, or an order ended in an error and nothing was placed) or
    KEY_REFUSED (the supplier refused our key on an order: the claim was
    released, and this computer must stop claiming)."""
    import stellar_buyer as sb
    try:
        n = int(sb.run() or 0)
    except Exception as ex:                                  # noqa: BLE001
        log.error(f"buyer tick failed: {type(ex).__name__}")
        try:
            sb.alert_hourly("buyer crashed (network)", f"{type(ex).__name__}")
        except Exception:                                    # noqa: BLE001
            pass
        return LOOK_FAILED
    if sb.TROUBLE.get("key"):
        return KEY_REFUSED
    placed = sb.TROUBLE.get("placed")       # really placed; n also counts the ones that could not be
    # An order that ended in an exception, in a look that placed nothing, is
    # a failed look: reported as success it would keep GitHub's backup out.
    if (sb.TROUBLE.get("errors") or 0) > 0 and not placed:
        log.error("buyer tick: an order ended in an error and nothing was placed")
        return ORDER_FAILED
    return n if placed is None else int(placed)


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
        sb.pending_orders()      # the site accepts this computer's token (a refusal raises)
        return True
    except Exception as ex:                                  # noqa: BLE001
        log.error(f"not ready to buy: {type(ex).__name__}")
        return False


def ready_to_mail() -> bool:
    """Can THIS computer read the mailbox and send a mail right now? Asked
    at the start of every run, beside ready_to_buy, for the other half of
    the rule: A DUTY THAT REPORTS SUCCESS SILENCES THE BACKUP. Before
    2026-11-14 a wrong Gmail password in one vault (or a line that blocks
    mail) left the run green, GitHub's mail bot stood back, and no customer
    got an eSIM from anywhere."""
    import smtplib
    import fulfillment_bot as fb
    try:
        fb._open_inbox(attempts=2).close()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20, local_hostname="waverole.com") as s:
            s.starttls()
            s.login(fb.GMAIL_USER, fb.env("GMAIL_APP_PASSWORD").replace(" ", ""))
        return True
    except Exception as ex:                                  # noqa: BLE001
        log.error(f"not ready to mail: {type(ex).__name__}")    # the type only: no address, no password
        return False


def _no_mail_went_out(fb) -> bool:
    """fb.LAST_RUN {due, sent, failed, hard_failed} (2026-11-16): True when a
    mail failed for a reason that is not one recipient's (connection, login,
    timeout) and none went out. Missing or unreadable = no verdict."""
    last = getattr(fb, "LAST_RUN", None)
    if not isinstance(last, dict):
        return False
    try:
        return int(last.get("hard_failed") or 0) > 0 and int(last.get("sent") or 0) == 0
    except (TypeError, ValueError):
        return False


def fulfil_tick() -> bool:
    """One run of the delivery-mail bot. Never raises. False when it could
    not do its work for a reason that will still be there next time (its
    own SystemExit(1): a mailbox that needs a person - it has mailed the
    owner; a crash; or a run whose due mails all failed to send for a reason
    of the line, not of one address); a passing hiccup it rode out itself is
    True."""
    import fulfillment_bot as fb
    try:
        fb.main()
    except SystemExit as ex:
        if ex.code:
            log.error("mail bot: the mailbox needs a person (it has said so by mail)")
            return False
    except Exception as ex:                                  # noqa: BLE001
        log.error(f"mail bot tick failed: {type(ex).__name__}")
        return False
    if _no_mail_went_out(fb):
        log.error("mail bot: no due mail went out (connection, login or timeout)")
        return False
    return True


# -- the loop --------------------------------------------------------------

def hold_until(state: dict, now: float) -> float:
    """The saved no_buy_until, never more than NO_BUY_S ahead: a clock set
    back must not stretch the hold (the same care as last_poll)."""
    return min(_num(state, "no_buy_until"), now + NO_BUY_S)


def duty(bell, state: dict, *, buyer=buyer_tick, fulfil=fulfil_tick, clock=time.time,
         mono=time.monotonic, save=save_state, run_s: float = None, min_looks: int = 0) -> dict:
    """Run for run_s, then return the state for the next run. `bell` needs
    wait(seconds) -> bool and rings() -> [codes]. min_looks: looks owed even
    when run_s is spent (main: 1, so a run never ends well without one)."""
    run_s = RUN_S if run_s is None else run_s
    t0 = mono()
    last_poll, last_fulfil = _num(state, "last_poll"), _num(state, "last_fulfil")
    hot_until, last_ring = _num(state, "hot_until"), 0.0
    hot_day, hot_n = int(_num(state, "hot_day")), int(_num(state, "hot_n"))
    relook: list = []            # wall times of the looks a ring still owes
    stats = {"polls": 0, "bought": 0, "rings": 0, "mail_runs": 0, "failed": ""}
    look_fails = mail_fails = quick = 0
    no_buy_until = hold_until(state, clock())

    def remember():
        save({"last_poll": last_poll, "last_fulfil": last_fulfil, "hot_until": hot_until,
              "bell_since": getattr(bell, "since", 0.0), "hot_day": hot_day, "hot_n": hot_n,
              "no_buy_until": no_buy_until, "failed": bool(stats["failed"])})

    while mono() - t0 < run_s:
        now = clock()
        # A clock set back must not hold the duty still: a time "in the future" is no time.
        if last_poll > now + 5:
            last_poll = 0.0
        if last_fulfil > now + 5:
            last_fulfil = 0.0
        codes = bell.rings()
        look = False
        if codes and now - last_ring >= RING_MIN_GAP_S and stats["rings"] < RING_BUDGET:
            last_ring = now
            stats["rings"] += 1
            # An order is on its way: the mail bot keeps close - for a ring
            # that names a supplier, and a bounded number of times a day.
            if int(now // 86400) != hot_day:
                hot_day, hot_n = int(now // 86400), 0
            if any(c for c in codes) and hot_n < HOT_RINGS_A_DAY:
                hot_n += 1
                hot_until = max(hot_until, now + HOT_FOR_S)
            if any(c in (S2, "") for c in codes):
                look = True
                relook = [now + d for d in RING_RELOOK_S]
        if relook and now >= relook[0]:
            relook.pop(0)
            look = True
        if now - last_poll >= POLL_S:
            look = True
        if look and mail_fails:
            look = False                          # nothing is claimed while the mail half is in doubt
        if look:
            last_poll = now
            remember()                            # before the work: a run killed mid-purchase still looked
            got = buyer()
            stats["polls"] += 1
            if got == KEY_REFUSED:
                no_buy_until = clock() + NO_BUY_S
                stats["failed"] = "key"
                break
            if got == ORDER_FAILED:
                # Not three strikes: a run has room for two periodic looks,
                # so a fault that fails every look would never fail the run
                # and GitHub would never be asked (2026-11-16 review).
                stats["failed"] = "order"
                break
            if got < 0:
                look_fails += 1
                if look_fails >= LOOK_FAILS_MAX:
                    stats["failed"] = "look"
                    break
                continue
            look_fails = 0
            stats["bought"] += got
            if got:
                # The queue may hold more than one look buys (the buyer hands
                # the rest back after its own budget): look again at once -
                # a bounded number of times in a row.
                quick += 1
                relook = [clock()] if quick <= QUICK_LOOKS_MAX else []
                hot_until = max(hot_until, clock() + HOT_FOR_S)
            else:
                quick = 0
            continue                              # the clock moved: read it again before the mail bot
        # 3. a mail bot that just failed is tried once more soon, whatever the rhythm
        every = MAIL_RETRY_S if mail_fails else (HOT_FULFIL_S if now < hot_until else FULFIL_S)
        if now - last_fulfil >= every:
            last_fulfil = now
            remember()
            stats["mail_runs"] += 1
            if fulfil() is False:
                mail_fails += 1
                if mail_fails >= MAIL_FAILS_MAX:
                    stats["failed"] = "mail"
                    break
            else:
                mail_fails = 0
            continue
        due = [last_poll + POLL_S, last_fulfil + every] + relook[:1]
        left = run_s - (mono() - t0)
        bell.wait(max(0.2, min(min(due) - now, left)))
    if mail_fails and not stats["failed"]:
        stats["failed"] = "mail"                  # the run ended before the retry: the next one proves it first
    if stats["polls"] < min_looks and not stats["failed"]:
        # The run's time went before the loop (main's checks): one look all
        # the same. A run that never looked must not end as a success, and a
        # single look that failed here is a failed run.
        last_poll = clock()
        remember()
        got = buyer()
        stats["polls"] += 1
        if got == KEY_REFUSED:
            no_buy_until = clock() + NO_BUY_S
            stats["failed"] = "key"
        elif got == ORDER_FAILED:
            stats["failed"] = "order"
        elif got < 0:
            stats["failed"] = "look"
        else:
            stats["bought"] += got
    remember()
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    began = time.monotonic()                    # the job's time limit counts from here, checks included
    state = load_state()
    mail_ok = ready_to_mail()
    now = time.time()
    state["no_buy_until"] = hold_until(state, now)      # saved clamped, or the hold never ends
    held = now < state["no_buy_until"]
    if held:
        log.error("the supplier refused this computer's key on an order a short while ago -- it does not "
                  "claim again yet (fix STELLAR_API_KEY: on the Mac, mac/wrnet-cli keys orders)")
    if held or not mail_ok or not ready_to_buy():
        # A run that cannot do the WHOLE duty claims nothing and ends as
        # failed: the network tries the other computer, and GitHub's backup
        # - which stands back only while this job succeeds - does the work.
        # The mail bot needs neither the supplier's key nor its wallet: one
        # run of it, so the eSIM mails keep going out meanwhile.
        log.error("this computer cannot do the whole duty right now -- handed back (nothing was claimed)")
        if mail_ok:
            fulfil_tick()
        state["failed"] = True
        save_state(state)
        return 1
    if state.get("failed"):
        # The last run ended badly: prove the mail half works before a
        # single order is claimed (a computer that keeps buying while it
        # keeps failing would share its claims with the backup).
        if not fulfil_tick():
            log.error("the mail bot still cannot do its work -- handed back (nothing was claimed)")
            return 1
        state["last_fulfil"] = time.time()
    spent = time.monotonic() - began
    if spent > RUN_S + LATE_LOOK_S:
        # The checks ate the run: a purchase started now could outrun the
        # job's time limit and be cut by the fence mid-way. No look; failed.
        log.error(f"the checks took {spent:.0f}s -- no look this run, handed back")
        state["failed"] = True
        save_state(state)
        return 1
    bell = Bell(BELL_URL, since=_num(state, "bell_since")).start()
    if not BELL_URL:
        log.warning(f"no bell address in the vault (ORDERS_BELL_URL) -- the queue is read every {POLL_S:.0f}s only")
    try:
        stats = duty(bell, state, run_s=max(0.0, RUN_S - (time.monotonic() - began)), min_looks=1)
    finally:
        bell.close()
    log.info(f"duty done: {stats['polls']} look(s) at the queue, {stats['bought']} bought, "
             f"{stats['rings']} ring(s), {stats['mail_runs']} mail-bot run(s)")
    if stats["failed"]:
        log.error(f"the duty is handed back as FAILED ({stats['failed']})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
