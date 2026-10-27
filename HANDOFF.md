# HANDOFF

Latest state of the relay. Read `CLAUDE.md` first for the plan and rules; this file says what's actually done.

---

## Day 1 — lihloway — server core

### Status: DONE

`pytest` (42 tests) and `python check.py` (quick gate: t1, t2, t4, late gated; t3 report-only) pass on **Windows 11** and **WSL2 Ubuntu 24.04**; pytest also passes on a real **Ubuntu 22.04 VM (Python 3.10)**. `submission_check.py` passes on all three.

> ⚠️ **Read "Test environments" below before trusting any numbers you produce.** VirtualBox on a PC with Hyper-V on gives garbage results.

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

### Test environments — read before trusting numbers

**⚠️ VirtualBox + Hyper-V gives invalid timing.** On Windows with Hyper-V active (WSL2 installed, Docker, or Core isolation → *Memory integrity* / *Kernel-mode hardware-enforced stack protection* on), VirtualBox can't get VT-x and silently falls back to "NEM" mode on top of Hyper-V. `VBox.log` then contains `HM: HMR3Init: Attempting fall back to NEM: VT-x is not available`. In that mode the VM's clock is right but **asyncio timers fire 13–240% late** (we measured `10 × sleep(0.1)` taking up to 3.4 s). The caption source, load clients and server are all paced by asyncio timers, so the source falls behind the clients' fixed test window and finals "go missing":

| Ubuntu 22.04 VirtualBox VM, NEM mode | Final % |
|---|---:|
| Our server, plain T1 (no impairment) | 68.8 |
| Original naive starter, plain T1 | 56.3 |

Both should be 100% (they are on Windows and WSL2), so this is the VM, not the code. **Don't debug the server against numbers from such a VM.**

**Check any machine first:** `check.py` now prints `asyncio timer drift` before running and warns above +10%. Measured: Windows +2%, WSL2 +0%, VirtualBox-on-Hyper-V **+134% to +240%**. Quick manual check from the repo root (prints the overrun fraction; should be close to 0):

```bash
python3 -c "import check; print(f'{check.timer_drift():+.0%}')"
```

**What we used instead:** WSL2 Ubuntu 24.04 (Python 3.12, kernel 6.6.87.2-microsoft-standard-WSL2, real `sch_netem`/`sch_tbf` modules, `tc` run as root via `wsl -u root`). Its timers are accurate, so its **correctness numbers (completeness, dup, stale) and relative comparisons are trustworthy**. But WSL2 is *not* the spec's reference environment ("use a full Ubuntu VM"), so its absolute latencies are **dev numbers only**.

**For the report (Day 4):** produce the final T1–T5 tables on a **full Ubuntu VM or native Ubuntu** where `check.py` reports timer drift < 10%: a teammate's machine, a Mac with Multipass, or a Windows PC with Hyper-V off. (lihloway's laptop is Windows 11 Home with Hyper-V held on by WSL2 + Core isolation, so it can't host a valid VM without disabling those.) If no full VM is available, use WSL2 and say so explicitly in the report.

### Results — Ubuntu (WSL2 Ubuntu 24.04, 16 vCPU, timer drift +0%, 1 run each, dev numbers)

Quick gate (`python check.py`):

| Scenario | Gate | Final % | Coverage % | P50 ms | P95 ms | P99 ms | Dup | Stale | Recovery P95 ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| t1 20c/15s | PASS | 100.0 | 100.0 | 1.2 | 1.6 | 2.0 | 0 | 0 | – |
| t2 100c/25s | PASS | 100.0 | 100.0 | 3.3 | 4.7 | 5.2 | 0 | 0 | – |
| t3 100c/40s, 10% slow | info | 95.3 | 95.3 | 4.0 | 1803 | 15781 | 0 | 0 | – |
| t4 100c/30s, 20% disc 5 s | PASS | 100.0 | 96.3* | 3.3 | 11.6 | 21.5 | 0 | 0 | 11.7 |
| late 100c/25s, 20% late 8 s | PASS | 100.0 | 93.3* | 3.8 | 8.0 | 10.6 | 0 | 0 | 0.7 |

t3 detail: normal-client P95 16.6 ms (isolated ✓), slow-client P95 18.6 s, slow fresh 2.6% ✗.

Network profiles on `lo` (`network_profiles/*.sh`, 100 clients, 40 s, reset afterwards):

| Profile | Final % | Coverage % | Dup/Stale | P50 ms | P95 ms | P99 ms | Fresh ≤500 ms | Fresh final ≤500 ms |
|---|---:|---:|---|---:|---:|---:|---:|---:|
| T5 `delay_loss.sh` (120±30 ms, 3% loss) | 100.0 | 99.95 | 0/0 | 260 | 823 | 1170 | 85.3% | 84.6% |
| T2 under `bandwidth_2mbit.sh` | 100.0 | 100.0 | 0/0 | 11.8 | 33.8 | 37.5 | 100% | 100% |

- T5: loss shows up as **tail latency** (TCP retransmission + head-of-line blocking), not lost captions: completeness stays 100%. Good material for the report's T5 explanation.
- **2 Mbit/s doesn't constrain 100 clients**: the stream is only ~4 msg/s × ~200 B × 100 clients ≈ 0.6 Mbit/s. Day 2/3: the bandwidth challenge needs ~300+ clients (or a lower rate) before it bites.

Naive starter, T3 on WSL2 (100c/60s, 10% slow) — Ubuntu "before" number for Day 2: final 95.1%, coverage 95.2%, P95 2253 ms, P99 24046 ms; normal P95 23 ms; **slow P50 14.7 s, P95 28.1 s, fresh 1.7%**.

### Results — Windows 11 laptop (loopback, 1 run each, dev numbers)

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
- No stress (>100 clients) or long-run memory runs yet (Days 2–4; see `TESTING.md`). Bandwidth profile run once, but 100 clients don't saturate 2 Mbit/s.
- No numbers yet from a full Ubuntu VM with valid timers (see "Test environments"). Ubuntu numbers above are WSL2.
- Line endings: `.gitattributes` now forces LF for `*.sh`. If you copy the repo from a Windows checkout made *before* this commit, run `sed -i 's/\r$//' network_profiles/*.sh` or bash will fail.

### Report notes (architecture & recovery sections)

- Draw the pipeline: source → `CaptionState` → N × `ClientChannel` → clients.
- Revision semantics: the three reject rules; why finals are durable; bounded memory argument (time + count + evicted floor).
- Recovery: resume vs fresh-join selection rule; atomic snapshot+register and the test that proves it; why no provisional on connect.
- Before/after: T4 completeness 98.2% → 100% from correct HISTORY.

---

## Day 2 — TBD

## Day 3 — TBD

## Day 4 — TBD
