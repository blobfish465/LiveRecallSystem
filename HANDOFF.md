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

## Day 3 — QiAn — browser client, constrained bandwidth, stress gate

### Status: DONE (steps 1–4 of 4)

`pytest` (101 tests: 63 + 6 browser + 5 bandwidth-tool + 15 check + 12 convergence-tool) passes on the Mininet lab VM (Ubuntu, 4 vCPU, Python 3.12 venv, timer drift +1%).

Done:
- **Browser client** (commit 14bfd92): `client/store.js` mirrors the server's revision rules (final durable, never older after newer, duplicate/stale rejected, bounded by 120 s of lecture time and 600 segments); `client/app.js` updates one row per segment in place, styles provisional/final, "What did I miss?" 30 s default + 15/30/60 from local state only, auto-reconnect with backoff resuming from the latest *final*, HISTORY merged through the same rule (no duplicates), late join, server+source restart detection, 30 s stall watchdog, demo drop button, `?ws=` for a server on another host. 6 end-to-end tests in headless Firefox (`tests/test_client.py`, skipped where Firefox is missing).
- **Constrained-bandwidth experiment** (`bandwidth_test.py`, this section): the Day 2 policy is **worse than a plain FIFO on a slow link**. Details below; the policy fix is **handed to Day 4** (team decision, 29 Oct).
- **Stress gate** (`check.py --stress`, this section): 300 and 500 clients, and 500 clients with 10% slow readers, all pass the full gate on this VM; `check.py` now also reports the server's RSS and `[stats]` peaks for every run.
- **Slow-then-fast convergence** (`convergence_test.py`, this section): freshness comes back at once in both configurations (the backlog drains in < 10 ms), but the lag-aware server keeps a recovered client on finals-only for **45 s** after it speeds up (**120 s** after a relapse), where the plain FIFO restores full delivery at once. A second policy issue, handed to Day 4 with the bandwidth fix.

Not done / handed on: the bandwidth policy fix (Day 4, below); T5 x3 (Day 4).

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

**Proposed fix, handed to Day 4** (decision 29 Oct: Day 3 moved on to the stress and convergence tooling). Whoever takes it: prototype in `liverecall/channel.py` behind the existing tests, re-run `check.py` (the T3 limits) and `python bandwidth_test.py --apply --clients 500 --duration 60 --runs 3`, and keep whichever policy wins on the graded metrics; if the fix is not done, the report states the finding as it stands here.
1. **Ping budget:** at most one ping per client per ~1 s instead of one per send. T3 detection still works: a stalled reader's pong is late by seconds, not by one RTT.
2. **No stop-and-wait:** while lagging, send finals as they arrive (1.33/s) and keep coalescing provisionals; keep one ping outstanding only as the lag *measurement*. Finals-only traffic at 500 clients is ~1 Mbit/s, under the cap, so caption age should drop to tens of ms with 100% completeness, i.e. clearly better than the FIFO's 1.6 s P95.
3. Probe pings in lag mode every ~5 s instead of every `LAG_S` (500 lagging clients × 1 probe/s ≈ 0.5 Mbit/s today).
4. Optionally read `TCP_INFO` (`tcpi_rtt`, `tcpi_notsent_bytes`, no network cost; `ws.transport.get_extra_info("socket")`) to tell "link congested" (coalesce, keep sending) from "reader stalled" (today's policy).

Methodology notes for the report: shaping only the server port is the public profile with a classifier in front (same rate/burst/latency); sch_tbf drops any packet larger than its burst, and loopback's MTU is 65536, so a single message over 4000 B stalls its connection for ever (a 2 MB probe transfer: 0 B received, 67 drops; with burst 66536: 1.84 Mbit/s). The caption stream never builds such segments (per-connection backlog is a few hundred bytes), but a large uncompressed HISTORY would; keep history replies small or use `--burst 66536` when that matters. Link Mbit/s counts every byte through `lo` in both directions (captions, pings/pongs, ACKs, handshakes); "peak 5 s" is the highest 5 s average, because tbf lets short bursts through. tbf counters and the `[stats]` peaks come from `tc -s qdisc` and the server log of each run.

### Stress: `check.py --stress` (300 / 500 clients)

`python check.py --stress [--runs 3]` runs three opt-in scenarios through the unmodified harness and applies the
full gate to each: `s300` (300 clients, 40 s), `s500` (500 clients) and `s500slow` (500 clients, 10% slow readers at
500 ms/msg, i.e. T3 at 5x scale). Gates: 100% finals, 0 duplicates / stale / non-final HISTORY / connection errors,
coverage >= 99% (s300, s500), normal P95 <= 100 ms (s300) / 150 ms (s500, s500slow), slow P95 <= 15 s, and the
server's peak RSS <= 200 MB. `check.py` now also captures `run_demo.py`'s output per scenario to
`results/check_<name>.log`, samples the server's RSS every 0.5 s and reads its `[stats]` lines, so every table row
shows the server side (RSS max, lagging peak, `max_pending`, coalesced, hopeless closes), and a run is rejected
when our server never printed its `listening` line (a stale server on the port would otherwise be measured).

| median of 3 runs, 40 s, Mininet VM (drift +1%) | s300 | s500 | s500slow |
|---|---:|---:|---:|
| final completeness % | 100.0 | 100.0 | 100.0 |
| normal update coverage % | 100.0 | 100.0 | 95.1 |
| P50 / P95 / P99 caption age, all clients (ms) | 19.2 / 30.6 / 33.3 | 26.3 / 41.7 / 46.0 | 26.4 / 839.6 / 7899.5 |
| normal-client P95 / slow-client P95 (ms) | n/a | n/a | 40.0 / 9162.7 |
| duplicates / stale / hopeless closes | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| server RSS max (MB) | 44.9 | 58.6 | 58.5 |
| lagging clients, peak | 0 | 0 | 50 |
| max_pending, peak | 0 | 0 | 1 |
| provisionals coalesced | 0 | 0 | 3600 |

Why 500 and not more: one `load_clients.py` process reads for all clients on one core; at 500 it is the harness, not
the server, that sits near a core's limit (the server's P95 grows only from 31 ms at 300 to 43 ms at 500 clients, RSS
from 45 to 59 MB). Beyond that the numbers measure the load generator. At 500 clients with 50 slow readers the picture
is exactly T3's: normal clients unaffected (P95 45 ms), slow clients at the client library's 9 s read-ahead floor, 100%
finals, `max_pending` 1, no hopeless closes, RSS flat. The slow readers lower the overall Normal Update Coverage
(95%) by design: their provisionals are coalesced.

### Convergence: `convergence_test.py` (slow-then-fast clients)

`python convergence_test.py --runs 3` (and `--slow-until 90 --duration 230` for the relapse case). Our server and
the public source, with the **public client code imported unmodified** (`load_clients.simulated_client`): 100 normal
clients plus 10 "converging" clients whose `slow` flag is an object that turns false at the switch, so they read like
T3's slow readers (500 ms pause per message) for 30 s and at full speed afterwards. The public metrics come from
`analyse_results.analyse_file`; the tool adds per-client measurements relative to the switch (see its docstring).

| 100 + 10 converging, slow 30 s of 120 s, median of 3 | plain FIFO (before) | lag-aware (after) |
|---|---:|---:|
| final completeness / duplicates / stale | 100% / 0 / 0 | 100% / 0 / 0 |
| normal clients P95 | 11 ms | 14 ms |
| converging, slow phase P95 | 13.6 s | **9.1 s** |
| backlog at the switch (age of the first caption read after it) | 14.6 s | **4.5 s** |
| fresh again after the switch (drain from the first read) | 0.25 s (0.008 s) | 0.24 s (0.000 s) |
| **full delivery (every revision) again after the switch** | **0.2 s** | **45.3 s** |
| settled P95 (switch + 10 s to the end) | 10 ms | 12 ms |
| normal update coverage, all clients | 100% | 96.7% |
| lag entries / provisionals coalesced / hopeless closes | 0 / 0 / 0 | 10 / 1700 / 0 |

Slow for 90 s of 230 s (1 run): FIFO slow-phase P95 41.9 s, backlog at the switch 44.3 s, and all 10 converging
clients were disconnected (`ConnectionClosedError`; no overflow line in the server log, so the WebSocket keepalive
closed them) and recovered 53 finals each via HISTORY: still 100% finals. Lag-aware: slow-phase P95 8.6 s, backlog
7.7 s, no disconnects, but **full delivery returned only 119.9 s after the switch** (coverage 95.0%).

What this shows:
- **Freshness converges immediately in both.** The "fresh after" ~0.25 s is the harness client finishing its last
  500 ms pause (it pauses *after* each message); from its first read the backlog drains in under 10 ms. Once the reader
  is fast, TCP and the client library empty any backlog at loopback speed, so the policy cannot do much better here.
- **The lag-aware server's gain is during overload**: fresher captions while slow (P95 9 s vs 14 s over 30 s; 9 s vs
  42 s over 90 s, where the FIFO's backlog grows without limit), no disconnects. This is the T3 result, and it holds.
- **Its cost is after overload: full delivery comes back late.** In finals-only mode a 500 ms reader keeps up
  (2 reads/s vs 1.33 finals/s), so its pongs turn fast within seconds of entering lag; lag mode then ends `RECOVER_S`
  (60 s) after that, i.e. ~45 s after the switch here, *regardless of when the client actually sped up*. When the
  slow phase outlasts that 60 s (the 90 s case), the server tries full delivery while the client is still slow,
  relapses, doubles the wait to `RECOVER_MAX_S` (120 s), and the now-fast client waits ~2 minutes for provisionals.
  The spec asks for a return to full delivery "when the client catches up"; Normal Update Coverage pays for the delay.

**Proposed fix, for Day 4 together with the bandwidth one** (same file, `liverecall/channel.py`; same validation:
`check.py` T3 limits, then `convergence_test.py --runs 3` and the 230 s case): the pong in finals-only mode only says
"keeps up with finals", not "keeps up with everything". Probe full delivery cheaply instead of waiting a fixed 60 s:
e.g. every ~10 s in lag mode send the queued provisionals of *one* segment and check that the pong stays fast;
leave lag after a few such probes pass, and do not double the wait when the relapse was caught by a probe (it only
cost one segment's provisionals). The Day 2 measurement (shorter `RECOVER_S` lost finals in T3 because each failed
retry refilled the read-ahead) is why the probe has to be small, not why the wait has to be long.

### Numbers today (median of 3), Mininet lab VM (Ubuntu, 4 vCPU), timer drift +1%

From `python check.py --runs 3` (quick durations: unmodified `run_demo.py`, shorter than PUBLIC_TESTS.md) and `python check.py --stress --runs 3`; every one of the 24 runs passed its gate.
- T1 (20 clients):   dup 0, stale 0, completeness 100%, coverage 100%, P50 3.9 ms, P95 6.1 ms, server RSS 26.0 MB
- T2 (100 clients):  P50 8.3 ms, P95 15.0 ms, P99 16.6 ms, completeness 100%, coverage 100%, RSS 31.4 MB
- T3 (10% slow):     normal P95 14.2 ms, slow P95 9102.3 ms, completeness 100%, coverage 95.1%, coalesced 720, max_pending 1, hopeless closes 0
- T4 (20% drop 5s):  dup 0, stale 0, completeness 100%, coverage 96.3%, recovery P50 3.4 ms, P95 4.0 ms
- late join (20%):   dup 0, stale 0, completeness 100%, recovery P50 0.6 ms
- stress s300 / s500 / s500slow: see the table above (P95 30.6 / 41.7 / normal 40.0 ms, RSS 44.9 / 58.6 / 58.5 MB)
- T5 (impairment profiles): not run on Day 3 (Day 4, x3 with `network_profiles/`)

### Interface / files for the next steps

- `bandwidth_test.py --apply|--unlimited --clients N --duration S --runs R [--only before|after] [--burst B] [--tag T]`; results in `results/bandwidth_<mode><tag>_c<N>_<config>_runNN.{csv,json,log}` + `..._summary.json`. Needs `sudo tc` (passwordless on this VM: `/etc/sudoers.d/liverecall-tc`).
- `convergence_test.py [--clients N] [--converging K] [--slow-ms 500] [--slow-until 30] [--duration 120] [--runs R] [--only before|after] [--tag T]`; results `results/convergence<tag>_c<N>_k<K>_s<slow-until>_d<duration>_m<slow-ms>_<config>_runNN.{csv,json,log}` + `_summary.json`. Imports `load_clients.simulated_client`, `run_demo` helpers and `analyse_results.analyse_file` from the unmodified harness.
- `check.py --stress [--runs 3] [--full]`; `--only s500` etc. also works. `server_pid`, `rss_mb`, `parse_stats`, `server_listened` live in `check.py` and are imported by `bandwidth_test.py`. Each scenario's harness + server output is in `results/check_<name>.log`.
- The browser client's interface is the protocol only; `window.liverecall` exposes `store`, `counters`, `dropConnection()` for tests and the console.

## Day 4 — TBD

To pick up (in this order):
1. **Two lag-policy fixes in `liverecall/channel.py`** (Day 3, "Proposed fix" under bandwidth and under convergence): (a) on a slow *link*: ping budget, no stop-and-wait in lag mode, rarer probes; (b) after overload: probe full delivery with one segment's provisionals instead of waiting a fixed 60/120 s. Keep `check.py` green (T3 limits), then re-run `bandwidth_test.py --apply --clients 500 --runs 3` and `convergence_test.py --runs 3` (+ `--slow-until 90 --duration 230`) and put the before/after tables in the report.
2. T1–T5 x3 on one VM with timer drift < 10% (`check.py --full --runs 3` for T1–T4 + late join; T5 with `network_profiles/` + `run_demo.py --runs 3`), `check.py --stress --runs 3`, 5-minute memory run with `monitor_server.py`.
3. Charts, README, report draft.
