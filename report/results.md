# Experiments and results (draft, Frankfurters)

> Draft for the report's results section. All numbers are from `HANDOFF.md` (Day 4 part 2) and `report/data/`. Figure slots marked **[Figure N]** are for the charts.

## Environment and method

Every number in this section comes from **one machine**, running the submitted lag policy (commit `0f026e5`). The exceptions are three T5 profiles (`delay_100ms`, `loss_3pct`, `reorder`) and the convergence runs, which ran one revision earlier. That revision differs only in when a slow *network link* is declared, and those runs never declared one, so the result is the same. The machine is an Ubuntu 26.04.1 LTS VM (Apple Silicon, aarch64, 2 vCPU, 5 GB RAM, Linux 7.0, Python 3.14.4, websockets 16.1.1). Source, server and clients all run on this VM and talk over loopback. All three are paced by asyncio timers, so before every experiment we measured how late `asyncio.sleep` fires (`check.py`'s timer-drift check). It was +0–1% throughout; above ~10% the source falls behind the clients' fixed test window and every metric is invalid.

All results use the **unmodified** instructor harness (`run_demo.py`, `load_clients.py`, `analyse_results.py`, `network_profiles/`). Each value is the **median of 3 independent runs**, each with a fresh server. **"Before"** is the same server with our overload policy switched off (`LIVERECALL_NO_COALESCE=1`): a plain per-client FIFO that sends every revision in order. **"After"** is the submitted server. Caption age is the time from the source's timestamp to the client receiving the update; "fresh" means at most 500 ms.

## Public tests T1–T4 and late join

| median of 3, public durations | T1 (20 clients) | T2 (100) | T3 (100, 10% slow) | T4 (100, 20% drop 5 s) | Late join (20%) |
|---|---:|---:|---:|---:|---:|
| Final completeness | **100%** | **100%** | **100%** | **100%** | **100%** |
| Duplicates / stale | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Normal update coverage | 100% | 100% | 94.6% | 98.3% | 92.2% |
| Caption age P50 / P95 / P99 (ms) | 2.6 / 5.6 / 7.5 | 6.6 / 14.3 / 17.5 | 6.3 / 20.0 / 7598 | 7.4 / 13.7 / 17.0 | 7.0 / 13.4 / 17.3 |
| Fresh captions / fresh finals | 100% / 100% | 100% / 100% | 96.4% / 93.2% | 100% / 98.4% | 100% / 92.7% |
| Recovery time P50 / P95 (ms) | – | – | – | 3.1 / 5.3 | – |
| Server memory (RSS, max) | 30 MB | 37 MB | 37 MB | 38 MB | 36 MB |

**[Figure 1]** Caption age P50/P95/P99 per test (log scale).

Every scenario delivers every final exactly once, with no stale updates. Coverage below 100% is by design, never a loss:
- **T3:** slow readers' outdated provisionals are skipped.
- **T4, late join:** clients that were away receive what they missed as finals from HISTORY, not as every intermediate revision.

At 100 clients the typical caption reaches a viewer in under 7 ms and 99% arrive within 18 ms. A dropped client gets its missed finals back within ~3 ms of reconnecting.

## T3 slow readers: before/after

The public T3 command (100 clients, 10% of them pausing 500 ms after every message, 60 s). Before and after runs alternate, 3 each.

| T3, median of 3 | Before: plain FIFO | After: our server |
|---|---:|---:|
| Final completeness (per run) | 95.13% (95.13 / 95.13 / 95.13) | **100%** (100 / 100 / 100) |
| Duplicates / stale / connection errors | 0 / 0 / 0 | 0 / 0 / 0 |
| Normal clients: caption age P95, fresh | 8.4 ms, 100% | 9.7 ms, 100% |
| Slow clients: caption age P50 / P95 | 14.7 s / 28.1 s | **3.4 s / 9.3 s** |
| Slow clients: fresh captions | 1.7% | **26.7%** |
| All clients: P95 / P99 | 2249 / 23995 ms | **25 / 7092 ms** |
| Normal update coverage | 95.2% | 94.5% |
| Server: lagging clients / provisionals skipped / max queue | 0 / 0 / 0 | 10 / ~1030 / 1 |

**[Figure 5]** Slow-client caption age percentiles, before vs after (`caption_age_percentiles.csv`, `t3_before_slow` / `t3_after_slow`).

A slow reader handles 2 messages per second, but the source produces about 4 (provisionals and finals).
- **FIFO:** every slow reader falls further behind for the whole run. By the end its captions are ~28 s old, and the finals still queued when the test ends are never read. That is the 4.87% of finals the FIFO loses: the same for all three runs because the workload is deterministic. This puts it just above the 95% grading threshold and well below 99%.
- **Our server:** a slow reader's pongs come back late while the kernel's TCP round-trip time stays low, so the server classifies it as a slow *reader*. It then sends that client finals immediately and only the newest provisional of each segment. At ~1.33 finals per second a 2 msg/s reader keeps up: its caption age stays bounded (P95 9.3 s, mostly the client library's 16-message read-ahead) and it receives every final.
- **Normal clients** are unaffected in both, because each client has its own queue and sender, and the source never waits for any client.

Coverage is almost the same (95.2% vs 94.5%): the FIFO sends every revision, but its slow readers never get to read the last ~30 s of them.

## T5 network impairment

Each profile is applied to the loopback interface with `tc netem`, then the public T5 command runs (100 clients, 40 s, 3 runs).

| Profile (on `lo`) | Final completeness | Coverage | P50 / P95 / P99 (ms) | Fresh / fresh finals | Dup / stale |
|---|---:|---:|---:|---:|---:|
| `delay_loss` (public T5): 120 ± 30 ms + 3% loss | 100% | 99.93% | 262 / 770 / 1103 | 87.8% / 86.1% | 0 / 0 |
| `delay_100ms`: 100 ± 20 ms | 100% | 100% | 204 / 251 / 270 | 100% / 100% | 0 / 0 |
| `loss_3pct`: 3% loss | 100% | 99.97% | 7.6 / 16.9 / 217 | 99.99% / 99.98% | 0 / 0 |
| `reorder`: 20 ms, 20% reordered | 100% | 100% | 49 / 56 / 57 | 100% / 100% | 0 / 0 |

**[Figure 2]** Caption age percentiles under `delay_loss`, ours vs plain FIFO (`caption_age_percentiles.csv`).

**Why delay and loss raise P95/P99 although TCP loses nothing.** Our server runs WebSocket over TCP, so every caption still arrives, once and in order: completeness is 100% under every profile. TCP guarantees delivery and order, not time. Delay and loss therefore show up as latency, and the shape of the latency follows TCP's mechanics:
- **Delay sets the floor.** Every caption crosses the impaired loopback twice (source → server, server → client). P50 is about twice the profile's one-way delay: 204 ms for 100 ms, 262 ms for 120 ms. No server design can go below this.
- **Loss adds a retransmission timeout to a few captions.** A lost segment is resent only after TCP notices. Our stream is many small messages, so there are often too few following segments to trigger fast retransmit (three duplicate ACKs), and TCP waits for the retransmission timeout instead. On Linux that timeout is at least 200 ms and grows with RTT and jitter. With 3% loss alone, the median stays at 7.6 ms but P99 jumps to **217 ms**: the 200 ms minimum timeout plus the normal path.
- **Head-of-line blocking spreads one loss across many captions.** TCP delivers bytes in order, so the client's kernel holds every caption received after the lost segment until the retransmission fills the gap, even though they have already arrived. One lost packet delays all captions behind it on that connection.
- **Delay and loss together multiply.** With 240 ms RTT plus jitter the retransmission timeout is several hundred ms. A segment lost twice waits for an exponentially backed-off timeout. So `delay_loss` has P95 770 ms and P99 1.1 s while its median is only 262 ms.
- **Reordering mostly costs nothing extra.** TCP's receiver reorders segments in its buffer. Only when reordering looks like loss (duplicate ACKs) does it trigger a spurious retransmit, so `reorder` stays within ~10 ms of its 40 ms floor.

These delays happen below the application. Our server's contribution is not to add to them. On `delay_loss` our server and the plain FIFO are within noise of each other (P95 770 vs 691 ms; fresh 87.8% vs 89.8%) with the same coverage (99.93% vs 99.97%). This required a fix found by this test, described in the overload section: the first version mistook retransmit-delayed pongs for a congested link and dropped provisionals for no benefit (coverage 93.5%).

## Constrained bandwidth: before/after (main optimisation comparison)

500 clients share a **2 Mbit/s** link (the public `bandwidth_2mbit.sh` token-bucket shaper, `tbf rate 2mbit burst 4000 latency 400ms`, applied only to the server's port), for 60 s. Before and after runs alternate, 3 runs each (`bandwidth_test.py --apply --clients 500 --duration 60 --runs 3`). With every revision of every caption, 500 clients need more than 2 Mbit/s, so the link is genuinely overloaded.

| 500 clients, 2 Mbit/s, median of 3 | Before: plain FIFO | After: our server |
|---|---:|---:|
| Final completeness (per run) | 98.1% (98.1 / 98.7 / 97.4) | **100%** (100 / 100 / 100) |
| Caption age P50 / P95 / P99 | 1117 / 1593 / 2466 ms | **535 / 1295 / 1914 ms** |
| Fresh captions / fresh finals (≤ 500 ms) | 0.0% / 0.0% | **47.1% / 50.1%** |
| Normal update coverage | **97.9%** | 57.9% |
| Duplicates / stale | 0 / 0 | 0 / 0 |
| Link use, average | 1.66 Mbit/s | 1.63 Mbit/s |
| Packets dropped by the shaper | 37.3k | 11.3k |
| Server memory (RSS, max) | 65.7 MB | 66.6 MB |

**[Figure 3]** Caption age over time (10 s windows), before vs after (`bandwidth_windows_10s.csv`).

**Result.** Our server delivers **every final in every run** where the FIFO loses 1.3–2.6%. It halves the median caption age, and half of all finals arrive within 500 ms where the FIFO manages none.
- **Why the FIFO is slow:** it queues every revision. TCP packs the queued captions into fewer, larger packets, so the stream just fits the link, but only with a standing backlog of about a second in every connection (P50 1.1 s, flat for the whole run).
- **What our server does:** it notices each client's lag from late WebSocket pongs, and from the kernel's TCP round-trip time it classifies the cause as the link, not the reader. It then sends finals immediately and skips provisionals that a newer revision has already replaced. About every 10 s (jittered per client) it tries full delivery again.

**Trade-off.** Coverage falls from 98% to 58%: under overload clients see fewer intermediate revisions. This is the choice the specification allows ("suppress obsolete provisionals under genuine overload"). On this link, full coverage and fresh captions cannot both be had: the FIFO pays for its coverage with a second of delay on every caption.

## Scale and memory

**Stress** (`check.py --stress --runs 3`, 40 s each; all 9 runs pass the gate):

| median of 3 | 300 clients | 500 clients | 500 clients, 10% slow |
|---|---:|---:|---:|
| Final completeness / dup / stale | 100% / 0 / 0 | 100% / 0 / 0 | 100% / 0 / 0 |
| Caption age P50 / P95 (ms) | 18 / 30 | 25 / 43 | normal clients P95 39, slow clients P95 9.1 s |
| Coverage | 100% | 100% | 95.1% |
| Server RSS max | 52 MB | 67 MB | 67 MB |

On 2 vCPUs the 500 simulated clients run on the same machine as the server, so these latencies partly measure the load generator; they are upper bounds.

**5-minute memory run**: T3 conditions (100 clients, 10% slow) for 300 s, server RSS sampled every second with `monitor_server.py`, 1 run each.

| 300 s, 10% slow readers | Before: plain FIFO | After: our server |
|---|---:|---:|
| Server RSS, start → max | 36.9 → 37.4 MB | 36.9 → 37.1 MB |
| Final completeness | 98.39% | **99.87%** |
| Slow-client caption age P95 | 47.1 s | **9.2 s** |
| Normal-client caption age P95 | 12.4 ms | 14.0 ms |
| Connection errors | 20 | 0 |

**[Figure 4]** Server RSS and server history size over 300 s (`memory_5min_rss_*.csv`, `memory_5min_stats_after.csv`).

Memory is flat. The server's history of finals stops growing at 120 entries after ~90 s (bounded by time and count), and per-client queues never hold more than one message. The FIFO's memory is also flat, but only because its slow readers' backlog sits in kernel socket buffers until the WebSocket keepalive disconnects them: 20 disconnects, caption age 47 s, and **98.39%** completeness, below the 99% threshold. Our server's 0.13% missing finals are a known limitation, discussed with the trade-offs: the last few finals of slow readers that were still slow when the run ended.

## Recovery after overload (convergence)

`convergence_test.py --runs 3`: 100 normal clients plus 10 clients that read slowly (500 ms per message) for 30 s and then at full speed, 120 s in total.

| median of 3 | Before: plain FIFO | After: our server |
|---|---:|---:|
| Final completeness / dup / stale | 100% / 0 / 0 | 100% / 0 / 0 |
| Converging clients, caption age P95 while slow | 13.6 s | **9.9 s** |
| Backlog when they speed up | 14.6 s | **6.1 s** |
| Fresh again after speeding up | 0.20 s | 0.19 s |
| Every revision delivered again after speeding up | **0.2 s** | 65.9 s |

With 90 s of slow reading (230 s run, 1 run): the FIFO's backlog reached 44.6 s and all 10 slow clients were disconnected (recovered via HISTORY), while ours stayed at 9.3 s P95 with no disconnects. Freshness returns at once in both. Our server waits 60–90 s of fast pongs before trying full delivery again, because a failed retry costs a still-slow reader its last finals; this delay is the price.
