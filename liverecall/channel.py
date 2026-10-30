"""Per-client outbound channel.

Every connected client gets one ClientChannel: a bounded queue plus one
sender task. The source handler calls offer(), which never awaits, so one slow
client can never delay the source or any other client.

Overload cap (hopeless clients): a client that reads slower than finals arrive
accumulates undelivered finals forever, and coalescing cannot shrink finals. So
the channel closes it when its oldest queued caption is older than `hopeless_s`,
or when the queue exceeds `max_pending` even after dropping superseded
provisionals. It reconnects and recovers the finals it missed from the server's
bounded HISTORY (90 s), which is why `hopeless_s` stays well inside that window.
Finals and control messages (HISTORY) are never dropped while the channel is open.

Coalescing mode (Day 2, opt-in via `coalescing`, default off): while on, a new
caption replaces the still-unsent queued provisional of the same segment in
place (keeping its queue position) instead of being appended. Finals and
control messages are never replaced. It is meant to be on only while the client
is lagging.

Lag detection (Day 2, needs `ping`): ws.send() returns as soon as bytes are in
socket buffers, so a slow reader's backlog never shows in our queue. But the
websockets client stops parsing incoming frames (pings too) once its read queue
is full, so a pong later than `lag_s` means "this client hasn't processed what we
sent". We keep at most one ping outstanding and send at most one per
`ping_every_s` (Day 4 ping budget; Day 2 pinged after every send, which on a slow
link cost more bandwidth than the captions). If the pong is overdue the channel
enters lag mode: coalescing turns on, queued superseded provisionals are dropped,
and only finals / control messages are sent, as they arrive (Day 4: no
stop-and-wait; one final per round trip is slower than the source on a slow link).
Provisionals stay queued and keep being coalesced. A ping unanswered for
`hopeless_s` closes the client like the queue-age cap (finals now go straight to
the socket, so a stalled reader's backlog is no longer in our queue).

Cause of the lag (Day 4, needs `rtt`): the kernel's smoothed TCP RTT is measured
from ACKs, which the client's kernel sends whether or not its app reads. High RTT
at lag entry = slow *link*; low RTT with a late pong = slow *reader*. The cause
only changes how soon recovery is tried (below).

Recovery: the server cannot see the client's read-ahead, so "caught up" is only
confirmed by trying full delivery. In lag mode pongs keep being timed (pings at
most every `probe_s`); once they have all been fast for the recovery wait and no
final/control message is queued, lag mode ends (coalescing off; the queue then
holds at most the newest provisional per segment, so nothing stale is sent). The
wait is `recover_s` (60 s) for a slow reader, because a failed try refills its
16-message read-ahead (~8 s of backlog at T3's 2 msg/s), and `link_recover_s`
(10 s) for a slow link, where a failed try only congests the link for a moment.
Each wait gets jitter so clients that lagged together do not all retry
together: +-50% for a link, +0..50% for a reader (60 s is a floor, see RECOVER_S). Entering lag within REARM_S of leaving doubles the next wait (up to
RECOVER_MAX_S); a longer calm period resets it.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .state import Caption

# Queued caption older than this -> hopeless (close; reconnect recovers via HISTORY,
# well inside its 90 s window). Our queue only sees the client's pauses, which last
# ~13 reads (read-ahead high 16 -> low 4): ~6.5 s at 2 msg/s (T3, can keep up), but
# > 10 s once a reader is slower than finals arrive (~1.33/s). 30 s never fired.
HOPELESS_S = 10.0
# Memory safety net (e.g. bursty sources); the age cap normally triggers first.
DEFAULT_MAX_PENDING = 64
# A pong later than this means the client stopped reading; loopback RTT is ~ms and
# the T5 profile adds ~0.25 s RTT, so 1 s avoids false positives.
LAG_S = 1.0
# Ping budget: at most one ping per client this often. Day 2 pinged after every send;
# on a 2 Mbit/s link with 500 clients ping + pong + ACKs cost more than the captions
# (Day 3). A stalled reader's pong is late by seconds, so 1 s still detects T3.
PING_EVERY_S = 1.0
# Ping budget in lag mode (after sends and when idle): pongs then only time the calm
# period before recovery, which needs no 1 s resolution. At 500 lagging clients on
# 2 Mbit/s, 1 ping/s (ping + pong + their ACKs) is about half of finals-only traffic.
LAG_PROBE_S = 3.0
# Pongs must stay fast this long before lag mode ends and full delivery is tried,
# for a stalled *reader*. A small probe cannot tell a fast reader from a 2 msg/s one:
# the client library's 16-message read-ahead absorbs it and the pong stays fast, so
# the only real test is full delivery itself, and a failed try refills that
# read-ahead (~8 s of backlog for a T3 reader). Measured (1 run each): 10 s -> T3
# 99.86% finals at 60 s, 99.67% at 120 s (backlog unread when the run ends); 60 s ->
# 100% at both. Finals completeness is capped in grading, coverage is not, so 60 s.
RECOVER_S = 60.0
# The same for a slow *link* (kernel RTT >= LINK_RTT_S at lag entry). Here a failed
# try only congests the link briefly (no read-ahead to refill), so try much sooner.
LINK_RECOVER_S = 10.0
# Kernel-smoothed TCP RTT (TCP_INFO, measured from ACKs, so unaffected by how fast the
# client *app* reads) at or above this when a pong is late: the link is slow, not the
# reader. Loopback is ~0.1 ms; a stalled reader does not raise it.
LINK_RTT_S = 0.05
# A slow link must be confirmed: LINK_STRIKES overdue pongs in a row, or one pong
# LINK_CONFIRM_S overdue. Congestion delays every pong; a lost segment's retransmit
# delays one. Measured on public T5 (delay_loss: 120+-30 ms each way, 3% loss), entering
# at the first late pong: ~35 false "slow link" entries per run, coverage 93.5% vs the
# FIFO's 99.97%, no freshness gain (P95 736 vs 691 ms). Readers still enter at once.
LINK_STRIKES = 2
LINK_CONFIRM_S = 3.0
# A slow-link lag entry waits link_recover_s x uniform(1 - J, 1 + J), a slow-reader
# one recover_s x uniform(1, 1 + J): with +-50% a T3 reader retried after ~30 s while
# still slow and lost tail finals (2 of 3 60 s runs, Day 4 part 2). On a slow link all clients
# enter lag together; without jitter they also all retry full delivery together and
# flood the link at once (measured: P50 caption age 80 ms between retries, 900 ms
# during them). Spread out, each retry adds a little load, and the backoff settles on
# as many full-delivery clients as the link can carry.
RECOVER_JITTER = 0.5
# Backoff cap: a client that keeps relapsing is retried at most this rarely.
RECOVER_MAX_S = 120.0
# Re-entering lag within this long of leaving it counts as a relapse (doubles the
# wait); a longer calm period resets the wait to the base.
REARM_S = 120.0


@dataclass(slots=True)
class Outgoing:
    wire: str
    caption: Caption | None  # None for control messages such as HISTORY


class ClientChannel:
    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        client_id: object = None,
        max_pending: int = DEFAULT_MAX_PENDING,
        on_overflow: Callable[["ClientChannel"], None] | None = None,
        ping: Callable[[], Awaitable[Awaitable[object]]] | None = None,
        lag_s: float = LAG_S,
        recover_s: float = RECOVER_S,
        hopeless_s: float = HOPELESS_S,
        ping_every_s: float = PING_EVERY_S,
        probe_s: float = LAG_PROBE_S,
        jitter: float = RECOVER_JITTER,
        rtt: Callable[[], float | None] | None = None,
        link_recover_s: float = LINK_RECOVER_S,
        link_strikes: int = LINK_STRIKES,
        link_confirm_s: float = LINK_CONFIRM_S,
        clock: Callable[[], float] = time.time,  # same clock as Caption.recv_ts
    ) -> None:
        self._send = send
        self.client_id = client_id
        self.max_pending = max_pending
        self.hopeless_s = hopeless_s
        self._clock = clock
        self._on_overflow = on_overflow
        self._queue: deque[Outgoing] = deque()
        # segment -> its most recent still-queued entry (for coalescing).
        self._latest: dict[int, Outgoing] = {}
        self.coalescing = False
        self._ping = ping
        self.lag_s = lag_s
        self.lagging = False
        self.lag_entries = 0
        self.lag_exits = 0
        self._base_recover_s = recover_s
        self._link_recover_s = link_recover_s
        self._rtt = rtt  # kernel RTT in seconds (None: unknown -> treat lag as a slow reader)
        self.link_lag = False  # cause of the current lag: slow link (True) or slow reader
        self.link_lag_entries = 0
        self.link_strikes = link_strikes
        self.link_confirm_s = link_confirm_s
        self._strikes = 0  # overdue pongs in a row on a slow link, not yet in lag
        self._strike_ping_at: float | None = None  # ping whose overdue pong was counted
        self._recover_s = recover_s  # current wait; doubles on relapse
        self.jitter = jitter
        self._recover_wait = recover_s  # _recover_s with this lag entry's jitter
        self._calm_since: float | None = None  # when pongs last turned fast
        self._left_lag_at: float | None = None
        self.ping_every_s = ping_every_s
        self.probe_s = probe_s
        self._pong: asyncio.Future[object] | None = None
        self._ping_at = -1e9  # loop time of the latest ping (lag timing)
        self._ping_wall = 0.0  # self._clock() of the latest ping (hopeless timing)
        self._wakeup = asyncio.Event()
        self._closed_evt = asyncio.Event()
        self.closed = False
        # Counters for logs, tests, and the report.
        self.sent = 0
        self.coalesced = 0
        self.overflowed = False

    # ----------------------------------------------------------- producers
    def offer_control(self, wire: str) -> None:
        """Queue a control message (e.g. HISTORY). Never dropped."""
        if self.closed:
            return
        self._queue.append(Outgoing(wire, None))
        self._wakeup.set()

    def offer(self, caption: Caption) -> None:
        """Queue a caption update. Must not await (called from the source loop)."""
        if self.closed:
            return
        if self.coalescing:
            entry = self._latest.get(caption.segment)
            if entry is not None and entry.caption is not None and not entry.caption.final:
                entry.wire = caption.wire
                entry.caption = caption
                self.coalesced += 1
                self._wakeup.set()
                self._check_hopeless()
                return
        entry = Outgoing(caption.wire, caption)
        self._queue.append(entry)
        self._latest[caption.segment] = entry
        if len(self._queue) > self.max_pending:
            self._compact()
            if len(self._queue) > self.max_pending:
                self._overflow()
                return
        self._wakeup.set()
        self._check_hopeless()

    def _check_hopeless(self) -> None:
        if self._pong_hopeless():
            self._overflow()
            return
        # The oldest caption entry is at (or near) the left; skip control items.
        for item in self._queue:
            if item.caption is not None:
                if self._clock() - item.caption.recv_ts > self.hopeless_s:
                    self._overflow()
                return

    def _slow_link(self) -> bool:
        if self._rtt is None:
            return False
        try:
            rtt = self._rtt()
        except OSError:
            return False
        return rtt is not None and rtt >= LINK_RTT_S

    def _pong_hopeless(self) -> bool:
        """A ping unanswered for hopeless_s: the client has not read what we sent
        that long ago. Lag mode sends finals straight through (no stop-and-wait),
        so for a stalled reader they pile up in socket buffers, not in our queue,
        and the queue-age check alone would never fire."""
        pong = self._pong
        if pong is None or pong.done():
            return False
        if self._clock() - self._ping_wall <= self.hopeless_s:
            return False
        # On a slow link the pong is late because the link is, and finals still
        # arrive (RTO backoff can hold a pong for seconds); queue age judges that case.
        return not self._slow_link()

    def _forget(self, item: Outgoing) -> None:
        cap = item.caption
        if cap is not None and self._latest.get(cap.segment) is item:
            del self._latest[cap.segment]

    def _overflow(self) -> None:
        self.overflowed = True
        self.close()
        if self._on_overflow is not None:
            self._on_overflow(self)

    # ------------------------------------------------------------ consumer
    async def run(self) -> None:
        """Sender loop: one in-flight send at a time, in queue order."""
        try:
            while not self.closed:
                self._check_lag()
                if self.closed:  # hopeless by pong age
                    break
                if self.lagging and self._can_leave_lag():
                    self._leave_lag()
                    continue
                if self.lagging:
                    item = self._take_sendable()
                else:
                    item = self._queue.popleft() if self._queue else None
                    if item is not None:
                        self._forget(item)
                if item is None:
                    await self._idle()
                    continue
                await self._send(item.wire)
                self.sent += 1
                await self._maybe_ping()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Connection closed or send failed: this client is gone.
            self.close()

    async def _idle(self) -> None:
        """Nothing sendable: wait for an offer, a pong, or the next check."""
        timeout: float | None = None
        if self._ping is not None:
            now = asyncio.get_running_loop().time()
            pong = self._pong
            if pong is not None and not pong.done():
                # Wake when it becomes overdue, so lag is noticed without an offer
                # (its done callback wakes us if it arrives first).
                overdue_in = self._ping_at + self.lag_s - now
                if overdue_in <= 0:  # overdue, maybe an unconfirmed slow-link strike
                    overdue_in = self._ping_at + self.link_confirm_s - now
                if not self.lagging and overdue_in > 0:
                    timeout = overdue_in
            elif self.lagging:
                # Probe now and then so pong speed keeps being measured while idle.
                due = self._ping_at + self.probe_s - now
                if due <= 0:
                    await self._start_ping()
                    return
                timeout = due
        self._wakeup.clear()
        if timeout is None:
            await self._wakeup.wait()
            return
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout + 0.001)
        except asyncio.TimeoutError:
            pass

    async def _maybe_ping(self) -> None:
        """Keep at most one ping outstanding, and send at most one per ping_every_s
        (per probe_s while lagging)."""
        if self._ping is None or (self._pong is not None and not self._pong.done()):
            return
        every = self.probe_s if self.lagging else self.ping_every_s
        if asyncio.get_running_loop().time() - self._ping_at < every:
            return
        await self._start_ping()

    async def _start_ping(self) -> None:
        assert self._ping is not None
        loop = asyncio.get_running_loop()
        pong = asyncio.ensure_future(await self._ping())
        self._pong = pong
        self._ping_at = loop.time()
        self._ping_wall = self._clock()

        def done(f: asyncio.Future[object]) -> None:
            if f.cancelled():
                return
            if f.exception() is not None:  # retrieved, so asyncio doesn't log it
                self.close()  # connection gone
                return
            pong_at = loop.time()
            if pong_at - self._ping_at > self.lag_s:
                self._calm_since = None
            else:
                self._strikes = 0  # on time: an earlier late pong was a one-off
                if self._calm_since is None:
                    self._calm_since = pong_at
            self._wakeup.set()

        pong.add_done_callback(done)

    def _can_leave_lag(self) -> bool:
        if self._calm_since is None:
            return False
        if asyncio.get_running_loop().time() - self._calm_since < self._recover_wait:
            return False
        return not any(i.caption is None or i.caption.final for i in self._queue)

    def _leave_lag(self) -> None:
        # The queue holds at most one provisional per segment (the newest, thanks
        # to _compact + in-place coalescing), and new offers append after it, so
        # normal delivery from here never sends an older revision after a newer one.
        self.lagging = False
        self.coalescing = False
        self.lag_exits += 1
        self._left_lag_at = asyncio.get_running_loop().time()
        self._calm_since = None

    def _check_lag(self) -> None:
        pong = self._pong
        if pong is None or pong.done():
            return
        now = asyncio.get_running_loop().time()
        if now - self._ping_at <= self.lag_s:
            return
        if self._pong_hopeless():
            self._overflow()
            return
        if self.lagging:
            self._calm_since = None  # an overdue pong is not calm
            return
        link = self._slow_link()
        if link and not self._link_confirmed(now):
            return
        self._strikes = 0
        self.link_lag = link
        self.link_lag_entries += self.link_lag
        base = self._link_recover_s if self.link_lag else self._base_recover_s
        if self._left_lag_at is not None and now - self._left_lag_at < REARM_S:
            # Relapse: double, but never below this cause's base (a link relapse
            # followed by a reader stall must not retry the reader after 20 s).
            self._recover_s = max(base, min(self._recover_s * 2, RECOVER_MAX_S))
        else:
            self._recover_s = base
        # A slow reader's wait is a floor (an early retry refills its read-ahead and
        # loses tail finals: Day 4 part 2, T3 60 s 99.83%), so jitter only upwards.
        low = 1 - self.jitter if self.link_lag else 1.0
        self._recover_wait = self._recover_s * random.uniform(low, 1 + self.jitter)
        self._calm_since = None
        self.lagging = True
        self.coalescing = True
        self.lag_entries += 1
        self._compact()

    def _link_confirmed(self, now: float) -> bool:
        if self.link_lag and self._left_lag_at is not None and now - self._left_lag_at < REARM_S:
            # Relapse on a link already confirmed slow: believe the first late pong.
            # Waiting for a second one let every 10 s retry flood the 2 Mbit/s link
            # for ~2 s (500 clients: fresh finals 53% -> 16%, up to 14 failed handshakes).
            return True
        if self._strike_ping_at != self._ping_at:  # this ping's pong just went overdue
            self._strike_ping_at = self._ping_at
            self._strikes += 1
        return self._strikes >= self.link_strikes or now - self._ping_at > self.link_confirm_s

    def _compact(self) -> None:
        """Drop provisionals queued before lag mode that a newer entry of the
        same segment supersedes; otherwise they could be sent after their final."""
        seen: set[int] = set()
        keep: deque[Outgoing] = deque()
        for item in reversed(self._queue):
            cap = item.caption
            if cap is not None and not cap.final and cap.segment in seen:
                self.coalesced += 1
                continue
            if cap is not None:
                seen.add(cap.segment)
            keep.appendleft(item)
        self._queue = keep

    def _take_sendable(self) -> Outgoing | None:
        """Lag mode: remove and return the oldest control message or final."""
        for i, item in enumerate(self._queue):
            if item.caption is None or item.caption.final:
                del self._queue[i]
                self._forget(item)
                return item
        return None

    def close(self) -> None:
        self.closed = True
        self._queue.clear()
        self._latest.clear()
        self._wakeup.set()
        self._closed_evt.set()

    @property
    def pending(self) -> int:
        return len(self._queue)
