# HANDOFF

Latest state of the relay. Read `CLAUDE.md` first for the plan and rules; this file says what's actually done.

---

## Day 1 — lihloway — server core

### Status: DONE

`pytest` (42 tests) and `python check.py` (quick gate: t1, t2, t4, late gated; t3 report-only) pass on **Windows 11** and **WSL2 Ubuntu 24.04**; pytest also passes on a real **Ubuntu 22.04 VM (Python 3.10)**. `submission_check.py` passes on all three.

> ⚠️ **Read "Test environments" below before trusting any numbers you produce.** VirtualBox on a PC with Hyper-V on gives garbage results.

### Numbers today (median of 3)

Public-test commands from `PUBLIC_TESTS.md` (`--runs 3`). Environment: WSL2 Ubuntu 24.04 with timer drift +1%, so these are dev numbers, not the reference Mininet VM. Dup/stale were 0 in all 12 runs.

```
- T1 (20 clients, 20 s):   dup 0, stale 0, completeness 100%, coverage 100%
- T2 (100 clients, 40 s):  P50 6.3 ms, P95 23.3 ms, P99 33.2 ms, completeness 100%
- T3 (10% slow, 60 s):     normal P95 21.4 ms, slow P95 28086 ms, completeness 95.1%   <- Day 2
- T4 (20% drop 5 s, 60 s): dup 0, stale 0, completeness 100%, recovery P50 2.3 ms (P95 2.8 ms)
```

Compared with the naive starter: T4 completeness 98.2% → **100%** (correct HISTORY recovery). T3 is unchanged (95.1%) because slow-reader backpressure is Day 2's work.

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

**Team decision — reference environment: the CS3103 Mininet lab Ubuntu VM.** The spec explicitly allows reusing it, so the report's final T1–T5 tables should come from the Mininet VM. Condition: run it on a machine where `check.py` reports **timer drift < 10%** (no Hyper-V underneath, or VT-x available to the hypervisor). lihloway's laptop can't host it validly: Windows 11 Home with Hyper-V held on by WSL2 + Core isolation. So whoever runs Day 4 should use the Mininet VM on their own machine, or a teammate's, after checking drift. If no valid Mininet VM is available, fall back to WSL2 and say so explicitly in the report.

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

## Day 2 — muhdbhz — slow-reader backpressure

### Status: DONE

`pytest` (63 tests), pyright (0 errors) and `python check.py` (**t3 now gated**) pass on WSL2 Ubuntu 26.04, Python 3.10.22, websockets 16.1.1, timer drift +0%.

Done: lag detection, lag-only provisional coalescing, finals-only delivery while lagging, recovery to full delivery, a cap for hopeless clients, `--no-coalesce` baseline, cumulative `[stats]`, t3 gate, `write_limit`, websockets pinned to 16.x.
Not done: constrained-bandwidth run (moved to Day 3 with the bandwidth-test script; `write_limit` is untested there).

### Numbers today (median of 3)

Public-test commands from `PUBLIC_TESTS.md` (`--runs 3`). WSL2 Ubuntu 26.04, timer drift +0%: dev numbers, not the reference Mininet VM. Dup/stale/connection errors were 0 in all 12 runs.

```
- T1 (20 clients, 20 s):   dup 0, stale 0, completeness 100%, coverage 100%
- T2 (100 clients, 40 s):  P50 6.4 ms, P95 10.1 ms, P99 13.8 ms, completeness 100%
- T3 (10% slow, 60 s):     normal P95 10.0 ms, slow P95 8890 ms, completeness 100%
- T4 (20% drop 5 s, 60 s): dup 0, stale 0, completeness 100%, recovery P50 2.6 ms (P95 4.1 ms)
```

**Before/after** (same session, x3 medians; "before" = `LIVERECALL_NO_COALESCE=1`, plain FIFO):

| | T2 before | T2 after | T3 before | T3 after |
|---|---|---|---|---|
| Final completeness | 100% | 100% | **95.1%** | **100%** |
| Coverage | 100% | 100% | 95.2% | 94.5% |
| P95 / P99 (all clients) | 7.8 / 11.1 ms | 12.6 / 16.0 ms | 2254 / 24044 ms | 12.2 / 6845 ms |
| Normal-client P95 | 7.8 ms | 12.6 ms | 5.6 ms | 9.8 ms |
| Slow-client P50 / P95 | – | – | 14.7 s / 28.1 s | 3.2 s / 8.9 s |
| Slow fresh (≤ 500 ms) | – | – | 1.7% | 28.4% |

Hopeless readers (20 clients, 10% at 1200 ms/msg, slower than finals arrive, 60 s, x2): 94.2% → 96.6% finals, 2 connection errors (one close + reconnect per hopeless client), ~70 finals recovered via HISTORY.

### How to run

```bash
python check.py                                   # quick gate, t3 included
python solution_server.py --port 8765 --no-coalesce          # baseline server
LIVERECALL_NO_COALESCE=1 python run_demo.py ...    # baseline under the harness (run_demo passes --port only)
```

### Design (all in `liverecall/channel.py`; module docstring has the full story)

1. **Lag signal = pong delay.** `send()` returns once bytes are in buffers, so our queue never sees a slow reader (Day 1's key finding). But the websockets client stops parsing frames, pings included, once 16 messages are unread. So after each send the channel keeps one ping outstanding (`ping=ws.ping`); a pong later than `LAG_S = 1.0` s means "lagging". 1 s leaves room for T5's ~0.25 s RTT.
2. **While lagging:** coalescing turns on (a new revision overwrites the unsent provisional of its segment in place), `_compact()` drops provisionals queued before lag that a newer entry supersedes (otherwise one could be sent after its final → stale), and only finals/control are sent, stop-and-wait (one send, wait for its pong). Finals alone are ~1.33 msg/s, below T3's 2 msg/s reader, so slow clients catch up on finals.
3. **Coalescing is off for clients that keep up**, so they get every revision (coverage is graded). Coverage drops 95.2 → 94.5% on T3 only because slow clients skip provisionals.
4. **Recovery:** while lagging with nothing to send, probe ping every `LAG_S`; leave lag after `RECOVER_S = 60` s of fast pongs with no final queued. Relapse within `REARM_S = 120` s doubles the wait (cap `RECOVER_MAX_S = 120`). Measured on T3 (x3): 10 s → 99.7% finals, 30 s → 98.9%, 60 s → 100%: each failed retry refills the read-ahead with provisionals and costs finals.
5. **Hopeless cap:** oldest queued caption older than `HOPELESS_S = 10` s, or > 64 entries after compaction → close 1013 (`on_overflow`); the client reconnects and recovers from HISTORY (90 s window). 30 s never fired: our queue only sees the client's pauses (~13 reads, high 16 → low 4 read-ahead). 10 s fires for hopeless clients and never for T3's.
6. **`write_limit=4096`** (default 32 KiB) on `websockets.serve`, so on a congested link `send()` blocks sooner and the backlog stays in our coalescable queue. No effect on T3 (measured); untested under bandwidth limits.
7. **websockets pinned to 16.x:** the lag signal depends on this client read-ahead behaviour.

### Interface for Day 3

```python
ClientChannel(send, client_id=None, max_pending=64, on_overflow=None, ping=None,
              lag_s=1.0, recover_s=60.0, hopeless_s=10.0, clock=time.time)
# offer(), offer_control(), run(), close() unchanged.
# Counters: sent, coalesced, lag_entries, lag_exits, overflowed; state: lagging, pending.
```

- No `ping` → no lag detection → plain FIFO (that's what `--no-coalesce` does, plus `hopeless_s=inf`).
- `[stats]` line: `clients lagging history active max_pending coalesced_total lag_entries_total hopeless_closes_total` (totals include disconnected clients).
- `check.py` t3 gate: 100% finals, 0 dup/stale/conn errors, normal P95 ≤ 100 ms, slow P95 ≤ 15 s.

### Known issues / trade-offs

- **Normal clients pay ~4–5 ms P95** (T2 7.8 → 12.6 ms, T3 5.6 → 9.8 ms, same session, so real, not noise). Likely the ping after every send (doubles the frames written). Possible fix, not tried: at most one ping per ~100 ms (lag detection then reacts up to 100 ms later). Still far under the 500 ms freshness threshold (T2 fresh 100%).
- **Slow-client latency floor ~8–9 s P95**: the client library buffers 16 messages we can't see or coalesce. Beating it would need the client to read faster.
- **Hopeless clients can't reach 100%** by definition; each close also counts as a `connection_error` in the harness.
- **Recovery isn't exercised by a 60 s T3**: slow clients stay lagging (they really are slow). It's unit-tested with short timings; Day 3's slow-then-fast client should test it for real (run > 60 s).
- Constrained bandwidth not run yet (Day 3).

### Next person should start with

The slow-then-fast convergence client (checks recovery to full delivery, needs > `RECOVER_S`) and the bandwidth run with ~300 clients (checks `write_limit` and whether lag detection triggers on a congested link). Browser client work is independent of all this.

### Report notes (backpressure section)

- Why a bounded app queue alone fails (backlog in socket/library buffers) → pong delay as the lag signal.
- Policy: coalesce only while lagging, never drop finals, compaction on lag entry, finals-only stop-and-wait, recovery with backoff, hopeless cap + HISTORY.
- Before/after table above; the 10/30/60 s recovery and 10/30 s hopeless measurements as design evidence.
- Trade-offs: +4–5 ms normal P95; 8 s floor; hopeless-client connection errors.

## Day 3 — QiAn — browser client, constrained bandwidth

### Status: IN PROGRESS (steps 1–2 of 4 done)

`pytest` (75 tests: 63 + 6 browser + 6 bandwidth-tool) passes on the Mininet lab VM (Ubuntu, 4 vCPU, Python 3.12 venv, timer drift +1%).

Done:
- **Browser client** (commit 14bfd92): `client/store.js` mirrors the server's revision rules (final durable, never older after newer, duplicate/stale rejected, bounded by 120 s of lecture time and 600 segments); `client/app.js` updates one row per segment in place, styles provisional/final, "What did I miss?" 30 s default + 15/30/60 from local state only, auto-reconnect with backoff resuming from the latest *final*, HISTORY merged through the same rule (no duplicates), late join, server+source restart detection, 30 s stall watchdog, demo drop button, `?ws=` for a server on another host. 6 end-to-end tests in headless Firefox (`tests/test_client.py`, skipped where Firefox is missing).
- **Constrained-bandwidth experiment** (`bandwidth_test.py`, this section): the Day 2 policy is **worse than a plain FIFO on a slow link**. Details and a proposed fix below.

Not done yet: `check.py --stress` (300–500 clients), slow-then-fast convergence client, the "Numbers today" block (end of day).

### Constrained bandwidth: result

Setup: `python bandwidth_test.py --apply --clients 500 --duration 60 --runs 3`. The public profile's shaper (`tbf rate 2mbit burst 32kbit latency 400ms`) on `lo`, restricted to the server port (this VM is used over an SSH/VS Code tunnel that also runs over `lo`; see the script's docstring). Unmodified `run_demo.py`, "before" = `LIVERECALL_NO_COALESCE=1` (plain FIFO), "after" = the real server, interleaved, median of 3. `write_limit=4096` (Day 2's open question): no effect, `max_pending` stayed 0 for the FIFO in every run; on a slow link the backlog sits in the kernel's send queue and the shaper, never in our outbox.

Demand with no limit (30 s, 1 run, peak over 5 s): 300 clients: FIFO 1.8 Mbit/s, lag-aware 3.3; 500 clients: FIFO 2.9, lag-aware 5.4. 100 clients (~0.6 Mbit/s) never touch the 2 Mbit/s limit; **500 puts both configurations over it**, which makes the comparison fair. The lag-aware server needs ~1.9× the FIFO's bandwidth because it pings after every caption and the client pongs: with permessage-deflate the public transcript (10 repeated sentences) compresses a caption to ~17 B of payload, so the ~52 B of TCP/IP headers per packet dominate, and ping + pong + ACKs cost more than the caption itself.

| 500 clients, 60 s, median of 3 | plain FIFO (before) | lag-aware (after) |
|---|---:|---:|
| final completeness | **100%** (98.7 / 100 / 100) | **94.3%** (94.3 / 94.5 / 92.8) |
| normal update coverage | 98.4% | 41.9% |
| P50 / P95 / P99 caption age | 1.1 / 1.6 / 2.4 s | 1.0 / **9.3** / 10.5 s |
| fresh captions / fresh finals (≤ 500 ms) | 0.2% / 0% | 0% / 0% |
| duplicates / stale | 0 / 0 | 0 / 0 |
| connection errors | 0 | 392 (= hopeless closes → reconnect + HISTORY) |
| link usage avg / peak 5 s | 1.72 / 1.81 Mbit/s | 1.68 / 1.80 Mbit/s |
| tbf packets dropped | 36.7k | 4.5k |
| lagging clients, peak | 0 | 409 |
| provisionals coalesced / hopeless closes | 0 / 0 | 49k / 387 |
| server RSS, max | 58.6 MB | 82.6 MB |

300 clients (1 run): FIFO 100% complete, P95 289 ms, fresh 100%; lag-aware 96.3%, P95 5.2 s, fresh 2.4%, 76 lagging. The FIFO fits in the link at 300 (1.8 Mbit/s demand); the lag-aware server's own control traffic pushes it over the cap, and the lag policy then makes it worse.

**Why** (from `results/bandwidth_limited_c500_*.csv`, 10 s windows): under the FIFO the caption age is *flat* at ~1.1 s for the whole run, every client gets every message: TCP coalesces the tiny queued captions into fewer, larger segments (fewer headers per caption), so the stream fits with a constant ~1 s of queueing. Under the lag-aware server a pong round trip on the congested link takes 0.5–1 s (queueing + RTOs on the shaper's drops), so ~400 clients enter lag mode within 10 s; in lag mode a client gets **one final per round trip** (stop-and-wait), slower than the source's 1.33 finals/s, so finals queue in the channel (`max_pending` 14), the oldest passes `HOPELESS_S = 10 s`, the client is closed with 1013, reconnects, and recovers via HISTORY, which costs still more bandwidth. With an MTU-safe burst (`--burst 66536`, fewer drops) there are no hopeless closes but caption age still grows from 2 s to 4.5 s across the run and completeness is 93.8%.

**Conclusion:** the pong-based lag policy was designed for a slow *reader* (T3: the client stops reading; the link is fine). On a slow *link* it (1) adds ~90% control traffic, (2) caps throughput at one message per RTT, (3) closes clients that are merely queued. A plain FIFO + TCP does better on every graded metric, so the 6-mark "constrained bandwidth" challenge is currently lost against our own baseline. Reported as is; not hidden.

**Proposed fix** (not implemented on Day 3; needs T3 re-validation with `check.py` + the x3 medians):
1. **Ping budget:** at most one ping per client per ~1 s instead of one per send. T3 detection still works: a stalled reader's pong is late by seconds, not by one RTT.
2. **No stop-and-wait:** while lagging, send finals as they arrive (1.33/s) and keep coalescing provisionals; keep one ping outstanding only as the lag *measurement*. Finals-only traffic at 500 clients is ~1 Mbit/s, under the cap, so caption age should drop to tens of ms with 100% completeness, i.e. clearly better than the FIFO's 1.6 s P95.
3. Probe pings in lag mode every ~5 s instead of every `LAG_S` (500 lagging clients × 1 probe/s ≈ 0.5 Mbit/s today).
4. Optionally read `TCP_INFO` (`tcpi_rtt`, `tcpi_notsent_bytes`, no network cost; `ws.transport.get_extra_info("socket")`) to tell "link congested" (coalesce, keep sending) from "reader stalled" (today's policy).

Methodology notes for the report: shaping only the server port is the public profile with a classifier in front (same rate/burst/latency); sch_tbf drops any packet larger than its burst, and loopback's MTU is 65536, so a single message over 4000 B stalls its connection for ever (a 2 MB probe transfer: 0 B received, 67 drops; with burst 66536: 1.84 Mbit/s). The caption stream never builds such segments (per-connection backlog is a few hundred bytes), but a large uncompressed HISTORY would; keep history replies small or use `--burst 66536` when that matters. Link Mbit/s counts every byte through `lo` in both directions (captions, pings/pongs, ACKs, handshakes); "peak 5 s" is the highest 5 s average, because tbf lets short bursts through. tbf counters and the `[stats]` peaks come from `tc -s qdisc` and the server log of each run.

### Interface / files for the next steps

- `bandwidth_test.py --apply|--unlimited --clients N --duration S --runs R [--only before|after] [--burst B] [--tag T]`; results in `results/bandwidth_<mode><tag>_c<N>_<config>_runNN.{csv,json,log}` + `..._summary.json`. Needs `sudo tc` (passwordless on this VM: `/etc/sudoers.d/liverecall-tc`).
- The browser client's interface is the protocol only; `window.liverecall` exposes `store`, `counters`, `dropConnection()` for tests and the console.

## Day 4 — TBD
