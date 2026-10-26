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

Extension point for Day 2: `_enforce_bound()` and `offer()` are where
lag-aware coalescing belongs (e.g. replacing a queued provisional of the same
segment as soon as the client is behind, instead of waiting for the cap).
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .state import Caption

DEFAULT_MAX_PENDING = 256


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
    ) -> None:
        self._send = send
        self.client_id = client_id
        self.max_pending = max_pending
        self._on_overflow = on_overflow
        self._queue: deque[Outgoing] = deque()
        self._wakeup = asyncio.Event()
        self.closed = False
        # Counters for logs, tests, and the report.
        self.sent = 0
        self.dropped = 0
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
        self._queue.append(Outgoing(caption.wire, caption))
        if len(self._queue) > self.max_pending:
            self._enforce_bound()
        self._wakeup.set()

    def _enforce_bound(self) -> None:
        while len(self._queue) > self.max_pending:
            victim = self._oldest_superseded_provisional()
            if victim is None:
                self._overflow()
                return
            del self._queue[victim]
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
                if not self._queue:
                    self._wakeup.clear()
                    await self._wakeup.wait()
                    continue
                item = self._queue.popleft()
                await self._send(item.wire)
                self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            # Connection closed or send failed: this client is gone.
            self.close()

    def close(self) -> None:
        self.closed = True
        self._queue.clear()
        self._wakeup.set()

    @property
    def pending(self) -> int:
        return len(self._queue)
