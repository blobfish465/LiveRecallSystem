"""Caption revision state and bounded history of finalized captions.

Pure logic with no networking, so it can be unit-tested directly.

Invariants:
  * A segment's revision only ever increases. An update whose revision is <=
    the revision we already hold is rejected (never forwarded).
  * A final caption is durable: once a segment is final, every further update
    for that segment is rejected.
  * Memory is bounded: finalized captions live in a history deque bounded by
    BOTH time (history_window_s) and count (history_max_items); in-progress
    segments are capped at max_active. Evicted segments are remembered only
    as a single integer floor (`_evicted_floor`) so stale updates for them are
    still rejected.
"""

from __future__ import annotations

import json
import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

# The spec requires recovery of up to 60 s of finals. We keep a bit more so a
# reconnecting client whose gap straddles the 60 s edge still gets everything.
DEFAULT_HISTORY_WINDOW_S = 90.0
# Hard cap independent of time, in case the source bursts many tiny segments.
DEFAULT_HISTORY_MAX_ITEMS = 2000
# Clients may ask for at most this much context on a fresh (late) join.
MAX_HISTORY_SECONDS = 60.0
DEFAULT_HISTORY_SECONDS = 30.0
# The public source completes one segment before starting the next, so in
# practice there is one active segment. The cap only guards a misbehaving
# source that abandons segments without finalizing them.
DEFAULT_MAX_ACTIVE = 64


@dataclass(frozen=True)
class Caption:
    segment: int
    revision: int
    final: bool
    text: str
    source_ts: float
    session: str
    recv_ts: float  # server wall-clock arrival time, used for history windows
    # Serialized CAPTION_UPDATE, built once and reused for every client.
    wire: str = field(compare=False, repr=False)

    def as_message(self) -> dict[str, Any]:
        return json.loads(self.wire)


def _as_positive_int(value: Any) -> int | None:
    # bool is a subclass of int; "segment": true must not be accepted as 1.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    return None


def _as_finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def make_caption(msg: dict[str, Any], recv_ts: float) -> Caption | None:
    """Validate a CAPTION_UPDATE dict. Returns None if it is malformed.

    Unknown extra fields are ignored and are not forwarded to clients.
    """
    segment = _as_positive_int(msg.get("segment"))
    revision = _as_positive_int(msg.get("revision"))
    source_ts = _as_finite_float(msg.get("source_ts"))
    final = msg.get("final")
    text = msg.get("text")
    session = msg.get("session", "L1")
    if (segment is None or revision is None or source_ts is None
            or not isinstance(final, bool) or not isinstance(text, str)
            or not isinstance(session, str)):
        return None
    wire = json.dumps({
        "type": "CAPTION_UPDATE",
        "session": session,
        "segment": segment,
        "revision": revision,
        "source_ts": source_ts,
        "final": final,
        "text": text,
    }, separators=(",", ":"))
    return Caption(segment, revision, final, text, source_ts, session, recv_ts, wire)


class CaptionState:
    def __init__(
        self,
        history_window_s: float = DEFAULT_HISTORY_WINDOW_S,
        history_max_items: int = DEFAULT_HISTORY_MAX_ITEMS,
        max_active: int = DEFAULT_MAX_ACTIVE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.history_window_s = history_window_s
        self.history_max_items = history_max_items
        self.max_active = max_active
        self._clock = clock
        # Latest provisional revision of each not-yet-final segment.
        self._active: OrderedDict[int, Caption] = OrderedDict()
        # Finalized captions, oldest first (arrival order).
        self._history: deque[Caption] = deque()
        self._final_segments: set[int] = set()
        # Every segment <= this has been evicted; updates for it are stale.
        self._evicted_floor = 0
        self.rejected = 0

    # ------------------------------------------------------------------ apply
    def apply(self, msg: dict[str, Any]) -> Caption | None:
        """Apply one CAPTION_UPDATE from the source.

        Returns the accepted Caption (to be forwarded to clients) or None if
        the update is malformed, stale, a duplicate, or for an already-final
        segment.
        """
        now = self._clock()
        cap = make_caption(msg, now)
        if cap is None or not self._is_newer(cap):
            self.rejected += 1
            self._prune(now)
            return None

        if cap.final:
            self._active.pop(cap.segment, None)
            self._history.append(cap)
            self._final_segments.add(cap.segment)
        else:
            self._active[cap.segment] = cap
            self._active.move_to_end(cap.segment)
            while len(self._active) > self.max_active:
                # Drop the lowest abandoned segment and treat it as evicted.
                seg = min(self._active)
                del self._active[seg]
                self._evicted_floor = max(self._evicted_floor, seg)
        self._prune(now)
        return cap

    def _is_newer(self, cap: Caption) -> bool:
        if cap.segment <= self._evicted_floor:
            return False
        if cap.segment in self._final_segments:
            return False  # final captions are durable
        current = self._active.get(cap.segment)
        return current is None or cap.revision > current.revision

    def _prune(self, now: float) -> None:
        cutoff = now - self.history_window_s
        while self._history and (
            len(self._history) > self.history_max_items
            or self._history[0].recv_ts < cutoff
        ):
            old = self._history.popleft()
            self._final_segments.discard(old.segment)
            self._evicted_floor = max(self._evicted_floor, old.segment)
        # Provisional segments older than retained finals were abandoned.
        for seg in [s for s in self._active if s <= self._evicted_floor]:
            del self._active[seg]

    # ---------------------------------------------------------------- recover
    def history_for(
        self,
        last_segment: int = 0,
        last_revision: int = 0,
        history_seconds: float | None = DEFAULT_HISTORY_SECONDS,
    ) -> list[Caption]:
        """Finalized captions to put in a HISTORY reply, oldest -> newest.

        * Never includes a final the client already holds, i.e. anything at or
          before (last_segment, last_revision).
        * Fresh / late join (last_segment == 0): only the last
          `history_seconds` (clamped to 60 s) of finals.
        * Resume (last_segment > 0): every retained final after last_segment,
          so a client that was away slightly longer than history_seconds still
          ends up complete. This is bounded by the server's history window, so
          it never replays the whole lecture.
        """
        now = self._clock()
        self._prune(now)

        if last_segment > 0:
            cutoff = -math.inf
        else:
            secs = _clamp_history_seconds(history_seconds)
            cutoff = now - secs

        items = [
            c for c in self._history
            if c.recv_ts >= cutoff
            and (c.segment > last_segment
                 or (c.segment == last_segment and c.revision > last_revision))
        ]
        items.sort(key=lambda c: c.segment)
        return items

    # -------------------------------------------------------------- inspect
    @property
    def history_len(self) -> int:
        return len(self._history)

    @property
    def active_len(self) -> int:
        return len(self._active)

    def active_captions(self) -> Iterable[Caption]:
        return list(self._active.values())


def _clamp_history_seconds(value: Any) -> float:
    secs = _as_finite_float(value)
    if secs is None or secs < 0:
        return DEFAULT_HISTORY_SECONDS
    return min(secs, MAX_HISTORY_SECONDS)


def history_message(items: Iterable[Caption]) -> str:
    """Serialize a HISTORY reply by splicing the pre-built caption JSON."""
    return '{"type":"HISTORY","items":[' + ",".join(c.wire for c in items) + "]}"
