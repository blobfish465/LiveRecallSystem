# HANDOFF

Latest state of the relay. Read `CLAUDE.md` first for the plan and rules; this file says what's actually done.

---

## Day 1 — lihloway — server core

### Status: DONE

`pytest` (42 tests) and `python check.py` (quick gate: t1, t2, t4, late gated; t3 report-only) pass. `submission_check.py` passes.

**Biggest open item for Day 2: T3 slow readers still lose ~half their finals (95.1% completeness overall).** The cause and a suggested direction are under "Interface for Day 2" below. When you fix it, set `"gated": True` for t3 in `check.py`.

### How to run

```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest -q
python check.py                                    # ~3 min
python solution_server.py --port 8765              # the server itself
```

### Files

| File | What |
|---|---|
| `solution_server.py` | Entry point + WebSocket wiring only: source handler, client handler, periodic `[stats]` line |
| `liverecall/state.py` | `CaptionState`: revision rules, bounded history, `history_for()` for HISTORY replies. No I/O |
| `liverecall/channel.py` | `ClientChannel`: per-client bounded queue + sender task. **Day 2 works here** |
| `tests/` | pytest: `test_state.py`, `test_channel.py`, `test_server.py` (real sockets, in-process) |
| `check.py` | Regression gate over the unmodified `run_demo.py` |
| `TESTING.md` | What's tested, how, and the manual checklist |

### Design decisions (and why)

1. **Source never awaits clients.** `handle_source` → `STATE.apply()` → `channel.offer()` for every client. `offer()` is synchronous; each client has its own sender task. A slow client can only hurt itself. (The starter `await`ed `gather(send...)` per update, so the slowest client paced everyone.)
2. **Revision rules in one place** (`CaptionState._is_newer`): reject if revision ≤ held revision, if the segment is already final, or if the segment is older than anything retained (`_evicted_floor`). Rejected updates are never forwarded, so clients can't see stale/duplicate revisions from the server.
3. **Bounded memory.** History deque bounded by **90 s and 2000 items** (spec needs 60 s; the margin covers gaps straddling the edge). Active (non-final) segments capped at 64. Evicted segments are remembered only as one integer. `test_long_lecture_stays_bounded` simulates an hour.
4. **Atomic HISTORY + register.** In `handle_client`: `history_for()` → queue HISTORY as the channel's first message → add channel to `CHANNELS`, with **no `await` in between**. Updates applied after the snapshot go into the channel queue behind HISTORY, so every final arrives exactly once and in order. Proven by `test_joins_during_burst_see_every_final_exactly_once`.
5. **HISTORY selection** (`history_for`):
   - Fresh join (`last_segment == 0`): finals from the last `history_seconds` (default 30, clamped to 60).
   - Resume (`last_segment > 0`): **every retained final after `last_segment`**, ignoring `history_seconds`, so a client away slightly longer than it asked for still ends up complete. Bounded by the 90 s window, so it never replays the lecture.
6. **No provisional on connect.** We don't send the in-progress segment's latest provisional after HISTORY: the server doesn't know which provisional the client already holds, so it could be a duplicate. Next revision comes within ~0.35 s.
7. **Day-1 overload policy (placeholder for Day 2):** per-client queue cap 256. Over the cap, drop the oldest provisional that has a newer item of the same segment queued. If the backlog is all finals, close with code 1013; the client reconnects and recovers from HISTORY. Finals are never silently dropped.
8. Unknown fields / message types, `SOURCE_DONE`, malformed JSON and bad hellos are ignored or closed with 1008 without affecting anyone else. Outgoing captions are re-serialized with only the protocol fields, built **once** per update (`Caption.wire`) and shared by all clients; HISTORY is spliced from those strings.

### Interface for Day 2 (backpressure)

```python
class ClientChannel:
    def offer(self, caption: Caption) -> None      # called by the source loop, must not await
    def offer_control(self, wire: str) -> None     # HISTORY etc., never dropped
    async def run(self) -> None                    # sender loop, one send in flight
    def _enforce_bound(self) -> None               # current cap policy -> replace/extend
    pending, sent, dropped, overflowed             # counters (used by [stats] line)
```

`Caption` (in `state.py`) has `segment, revision, final, text, source_ts, recv_ts, wire`.

**Key finding — why T3 fails and why our queue can't fix it on its own.**
Running T3 with server logs shows `max_pending=0` for the whole run: our per-client queue is *always empty*, so its cap/drop policy never triggers. Each `ws.send()` completes immediately because the whole slow-client backlog (~80 messages, ~20 KB) fits in buffers *past* the server: the client library's receive queue (`websockets` reads ahead up to 16 messages) plus the TCP receive/send buffers. The slow reader consumes 2 msg/s while the source produces ~4 msg/s, so at test end ~half its finals are still sitting in those buffers, undelivered → 95.1% completeness, slow P50 ≈ 10 s. Write-buffer backpressure (`send()` blocking) only kicks in after far more data than this stream ever produces.

So Day 2 needs a **lag signal that doesn't depend on buffers filling**, and must **limit how much is in flight** to each client, so excess provisionals are still in *our* queue (where they can be coalesced) rather than in socket buffers. Ideas to evaluate:
- **Ping/pong as an ack.** The `websockets` client answers pings from its reader task, which stops reading once its 16-message queue is full; so pong delay ≈ the client's backlog. Send a ping every ~0.5 s per client; if RTT exceeds a threshold, or more than N messages were sent since the last pong, stop handing messages to `ws.send` and let `ClientChannel` coalesce (keep all finals + newest provisional per segment). Resume full delivery once the RTT drops (the spec requires going back to full updates once the client catches up). (Hypothesis, not verified yet.)
- Smaller `write_limit` / `SO_SNDBUF` only help once buffers are actually full, so they're probably not enough on their own for this workload; worth measuring under the 2 Mbit bandwidth profile.
- Arithmetic: finals alone are ~1.33 msg/s, below the slow reader's 2 msg/s, so with good coalescing slow clients *can* keep up and reach 100% finals.
- `websockets` enables **permessage-deflate** by default: costs CPU per client per message, but saves bandwidth. Worth testing both under the bandwidth profile.
- Keep a `--naive` / no-coalesce switch for the required before/after comparison. The naive starter's numbers are below already.

### Results (Windows 11 laptop, loopback, 1 run each — not the report numbers)

Quick gate (`python check.py`) with the Day 1 server:

| Scenario | Gate | Final % | Coverage % | P50 ms | P95 ms | P99 ms | Dup | Stale | Recovery P95 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| t1 20c/15s | PASS | 100.0 | 100.0 | 2.0 | 10.6 | 12.5 | 0 | 0 | – |
| t2 100c/25s | PASS | 100.0 | 100.0 | 7.0 | 23.9 | 32.9 | 0 | 0 | – |
| t3 100c/40s, 10% slow | info | 95.1 | 95.2 | 10.7 | 1537 | 15809 | 0 | 0 | – |
| t4 100c/30s, 20% disc 5 s | PASS | 100.0 | 96.5* | 10.6 | 26.5 | 38.1 | 0 | 0 | 8.5 |
| late 100c/25s, 20% late 8 s | PASS | 100.0 | 93.4* | 7.0 | 22.5 | 37.4 | 0 | 0 | 4.0 |

\* Coverage < 100% is expected: disconnected / late clients weren't connected for the provisionals sent while away, and HISTORY carries only finals. t3 detail: normal-client P95 28 ms (isolated ✓), slow-client P95 18.5 s, slow fresh 2.6% ✗.

Naive starter server ("before" baseline, same harness, default public durations):

| Test | Final % | Coverage % | P50 ms | P95 ms | P99 ms | Notes |
|---|---:|---:|---:|---:|---:|---|
| T1 20c/20s | 100.0 | 100.0 | 2.0 | 11.0 | 17.7 | |
| T2 100c/40s | 100.0 | 100.0 | 8.5 | 23.2 | 27.0 | |
| T3 100c/60s, 10% slow | 95.1 | 95.1 | 9.0 | 2090 | 23960 | slow clients P50 14.6 s, fresh 1.7% |
| T4 100c/60s, 20% disc | 98.2 | 98.3 | 9.4 | 24.0 | 30.6 | empty HISTORY → missed finals lost |

### Known issues / not done

- Slow-client freshness is not handled yet (Day 2).
- No bandwidth-profile, stress (>100 clients) or long-run memory runs yet (Days 2–4; see `TESTING.md`).
- All numbers so far are from Windows, not Ubuntu.

### Report notes (architecture & recovery sections)

- Draw the pipeline: source → `CaptionState` → N × `ClientChannel` → clients.
- Revision semantics: the three reject rules; why finals are durable; bounded memory argument (time + count + evicted floor).
- Recovery: resume vs fresh-join selection rule; atomic snapshot+register and the test that proves it; why no provisional on connect.
- Before/after: T4 completeness 98.2% → 100% from correct HISTORY.

---

## Day 2 — TBD

## Day 3 — TBD

## Day 4 — TBD
