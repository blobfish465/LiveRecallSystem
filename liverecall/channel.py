"""Per-client outbound channel.

Every connected client gets one ClientChannel: a bounded queue plus one
sender task. The source handler calls offer(), which never awaits, so one slow
client can never delay the source or any other client.

Day-1 overload policy (deliberately simple; Day 2 extends it):
  * The queue holds at most `max_pending` items.
  * When it is over the cap, drop the oldest *superseded* provisional, i.e.
    a provisional revision for which a newer item of the same segment is
    already queued. Finals and control messages (HISTORY) are never dropped.
  * If nothing can be dropped (the backlog is all finals), the client is too
    far behind to catch up live: close it. It reconnects and recovers the
    finals it missed from the server's bounded HISTORY, so state stays bounded
    and nothing final is lost.

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
queued and keep being coalesced. Lag mode is sticky for now (no recovery yet).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .state import Caption

DEFAULT_MAX_PENDING = 256
# A pong later than this means the client stopped reading; loopback RTT is ~ms and
# the T5 profile adds ~0.25 s RTT, so 1 s avoids false positives.
LAG_S = 1.0


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
    ) -> None:
        self._send = send
        self.client_id = client_id
        self.max_pending = max_pending
        self._on_overflow = on_overflow
        self._queue: deque[Outgoing] = deque()
        # segment -> its most recent still-queued entry (for coalescing).
        self._latest: dict[int, Outgoing] = {}
        self.coalescing = False
        self._ping = ping
        self.lag_s = lag_s
        self.lagging = False
        self.lag_entries = 0
        self._pong: asyncio.Future[object] | None = None
        self._ping_at = 0.0
        self._wakeup = asyncio.Event()
        self._closed_evt = asyncio.Event()
        self.closed = False
        # Counters for logs, tests, and the report.
        self.sent = 0
        self.dropped = 0
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
                return
        entry = Outgoing(caption.wire, caption)
        self._queue.append(entry)
        self._latest[caption.segment] = entry
        if len(self._queue) > self.max_pending:
            self._enforce_bound()
        self._wakeup.set()

    def _enforce_bound(self) -> None:
        while len(self._queue) > self.max_pending:
            victim = self._oldest_superseded_provisional()
            if victim is None:
                self._overflow()
                return
            gone = self._queue[victim]
            del self._queue[victim]
            self._forget(gone)
            self.dropped += 1

    def _oldest_superseded_provisional(self) -> int | None:
        # Walk newest -> oldest remembering which segments have a newer item,
        # then pick the oldest provisional whose segment has one.
        seen: set[int] = set()
        candidate = None
        for i in range(len(self._queue) - 1, -1, -1):
            cap = self._queue[i].caption
            if cap is None:
                continue
            if not cap.final and cap.segment in seen:
                candidate = i
            seen.add(cap.segment)
        return candidate

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
                    if self._pong is not None and not self._pong.done():
                        await self._wait_pong(self._pong)
                        continue
                    item = self._take_sendable()
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
                    self._pong = asyncio.ensure_future(await self._ping())
                    # Retrieve a failure (connection closed) so asyncio doesn't log it.
                    self._pong.add_done_callback(lambda f: f.cancelled() or f.exception())
                    self._ping_at = asyncio.get_running_loop().time()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Connection closed or send failed: this client is gone.
            self.close()

    def _check_lag(self) -> None:
        pong = self._pong
        if self.lagging or pong is None or pong.done():
            return
        if asyncio.get_running_loop().time() - self._ping_at > self.lag_s:
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

    def close(self) -> None:
        self.closed = True
        self._queue.clear()
        self._latest.clear()
        self._wakeup.set()
        self._closed_evt.set()

    @property
    def pending(self) -> int:
        return len(self._queue)
