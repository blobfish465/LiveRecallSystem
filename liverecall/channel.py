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
sent". After each send we keep one ping outstanding; if its pong is overdue the
channel enters lag mode: coalescing turns on and only finals / control messages
are sent, stop-and-wait (one send, then wait for its pong). Provisionals stay
queued and keep being coalesced.

Recovery (Day 2): the server cannot see the client's read-ahead, so "caught up"
is only confirmed by trying full delivery. In lag mode, with nothing to send, a
probe ping goes out every `lag_s`; once pongs have all been fast for
`recover_s` and no final/control message is queued, lag mode ends (coalescing
off; the queue then holds at most the newest provisional per segment, so nothing
stale is sent). If the client is still slow it refills and lags again; entering
lag within REARM_S of leaving doubles the next wait (up to RECOVER_MAX_S) so a
still-slow client is not re-flooded. A longer calm period resets the wait.
"""

from __future__ import annotations

import asyncio
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
# Pongs must stay fast this long before lag mode ends. A still-slow reader needs
# ~25 s of finals-only to drain its read-ahead, and every failed retry refills it;
# measured on T3 (median of 3): 10 s -> 99.7% finals, 30 s -> 98.9%, 60 s -> 100%.
RECOVER_S = 60.0
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
        self._recover_s = recover_s  # current wait; doubles on relapse
        self._calm_since: float | None = None  # when pongs last turned fast
        self._left_lag_at: float | None = None
        self._pong_at = 0.0
        self._classified = True  # has the current pong's speed been recorded?
        self._pong: asyncio.Future[object] | None = None
        self._ping_at = 0.0
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
        # The oldest caption entry is at (or near) the left; skip control items.
        for item in self._queue:
            if item.caption is not None:
                if self._clock() - item.caption.recv_ts > self.hopeless_s:
                    self._overflow()
                return

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
                if self.lagging:
                    pong = self._pong
                    if pong is not None and (not pong.done() or not self._classified):
                        await self._wait_pong(pong)
                        continue
                    if self._can_leave_lag():
                        self._leave_lag()
                        continue
                    item = self._take_sendable()
                    if item is None and self._ping is not None:
                        # Nothing to send: probe at most every lag_s so pong
                        # speed keeps being measured, else sleep until then.
                        due = self._ping_at + self.lag_s - asyncio.get_running_loop().time()
                        if due <= 0:
                            await self._start_ping()
                        else:
                            self._wakeup.clear()
                            try:
                                await asyncio.wait_for(self._wakeup.wait(), due)
                            except asyncio.TimeoutError:
                                pass
                        continue
                else:
                    item = self._queue.popleft() if self._queue else None
                    if item is not None:
                        self._forget(item)
                if item is None:
                    self._wakeup.clear()
                    await self._wakeup.wait()
                    continue
                await self._send(item.wire)
                self.sent += 1
                if self._ping is not None and (self._pong is None or self._pong.done()):
                    await self._start_ping()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Connection closed or send failed: this client is gone.
            self.close()

    async def _start_ping(self) -> None:
        assert self._ping is not None
        loop = asyncio.get_running_loop()
        pong = asyncio.ensure_future(await self._ping())
        self._pong = pong
        self._classified = False
        self._ping_at = loop.time()

        def done(f: asyncio.Future[object]) -> None:
            self._pong_at = loop.time()
            # Retrieve a failure (connection closed) so asyncio doesn't log it.
            if not f.cancelled():
                f.exception()

        pong.add_done_callback(done)

    def _can_leave_lag(self) -> bool:
        if self._calm_since is None:
            return False
        if asyncio.get_running_loop().time() - self._calm_since < self._recover_s:
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
        if self.lagging or pong is None or pong.done():
            return
        now = asyncio.get_running_loop().time()
        if now - self._ping_at > self.lag_s:
            if self._left_lag_at is not None and now - self._left_lag_at < REARM_S:
                self._recover_s = min(self._recover_s * 2, RECOVER_MAX_S)
            else:
                self._recover_s = self._base_recover_s
            self._calm_since = None
            self.lagging = True
            self.coalescing = True
            self.lag_entries += 1
            self._compact()

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

    async def _wait_pong(self, pong: asyncio.Future[object]) -> None:
        """Wait for the pong, or for close(). A failed pong means the connection is gone."""
        closer = asyncio.ensure_future(self._closed_evt.wait())
        try:
            await asyncio.wait({pong, closer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            closer.cancel()
        if pong.done() and not pong.cancelled() and pong.exception() is not None:
            self.close()
        elif pong.done() and not pong.cancelled():
            self._classified = True
            if self._pong_at - self._ping_at > self.lag_s:
                self._calm_since = None
            elif self._calm_since is None:
                self._calm_since = self._pong_at

    def close(self) -> None:
        self.closed = True
        self._queue.clear()
        self._latest.clear()
        self._wakeup.set()
        self._closed_evt.set()

    @property
    def pending(self) -> int:
        return len(self._queue)
