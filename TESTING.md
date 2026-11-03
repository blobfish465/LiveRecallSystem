# Testing

Rule: **before handing off, `pytest` and `python check.py` must both pass.** Each day adds tests for its own work here.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
```

## 1. Unit + in-process integration tests (seconds)

```bash
python -m pytest -q          # ~15 s; 150 tests (144 + 6 browser tests, skipped without Firefox)
```

| File | Tests | Covers |
|---|---:|---|
| `tests/test_state.py` | 32 | revision rules, final durability, malformed/unknown fields, variable revision counts (1/2/3/8), history bounded by time + count, evicted-segment rejection, active-segment cap, 1-hour boundedness, HISTORY selection (fresh vs resume, 60 s clamp, ordering, finals only), 60 s at 50 segments/s, the 768 KiB reply cap (newest kept, exact boundary) |
| `tests/test_channel.py` | 52 | control-first ordering, full delivery when not lagging, opt-in coalescing (in place, finals/control never replaced), lag detection by late pong (finals/control only, sent without waiting for pongs), ping budget, lag cause from kernel RTT (slow link vs slow reader) and its recovery wait, recovery jitter bounds, compaction on lag entry (no provisional after its final), recovery after fast pongs + backoff on relapse (never below the cause's base), hopeless cap (age + count + unanswered ping, not on a slow link) closes, send/pong failure, offer() never blocks, `LagMemory` (bounded, expiring, per client; a lagging client resumes lag mode on reconnect), slow-link lag reclassified as reader lag when a pong is stuck |
| `tests/test_server.py` | 10 | real WebSockets in-process: late join HISTORY→live, resume sends only missed finals, 30 clients joining during a burst see every final exactly once (atomic snapshot+register), source garbage/unknown types/SOURCE_DONE ignored, bad hello → 1008, abrupt client drop doesn't affect others, `[stats]` totals survive disconnects, listening socket caps TCP segments at 1400 B (Linux; skipped where the option can't be read) |
| `tests/test_client.py` | 6 | browser e2e in headless Firefox (see §5); skipped where Firefox/geckodriver are missing. All 6 pass on the final code (3 Nov, WSL2, Firefox 159 snap, `python -m pytest -m browser`) |
| `tests/test_check.py` | 15 | `check.py`: server-log parsing, `[stats]` peaks, gate rules |
| `tests/test_bandwidth_tool.py` | 5 | `bandwidth_test.py`: tc output parsing, link-rate maths |
| `tests/test_convergence_tool.py` | 12 | `convergence_test.py`: metric definitions |
| `tests/test_hidden_sim.py` | 18 | `hidden_sim.py` / `custom_source.py`: scenario bounds, roles, flappers, source selection, flap wrapper, CSV merging, tc chain, metrics, revisions/bursts |

**Python 3.8 check** (the server must also run on Ubuntu 20.04; see `CLAUDE.md`, "Hard constraints"). Before pushing server changes:

```bash
uv python install 3.8       # then create venv38 from that interpreter
venv38/bin/python -m pip install -r requirements.txt "pytest<8.4"     # Windows: venv38\Scripts\python
venv38/bin/python -m pytest -q
```

## 2. Regression gate on the public harness (minutes)

```bash
python check.py            # quick (~3 min)
python check.py --full     # PUBLIC_TESTS.md durations
python check.py --only t3  # one scenario
python check.py --stress   # 300/500-client scenarios (~2 min), not part of the default gate
```

`check.py` first prints `asyncio timer drift`. If it warns (> +10%), **stop**: this machine's timers are unreliable (typically VirtualBox on a Hyper-V host) and every metric will be wrong. See "Test environments" in `HANDOFF.md`.

Gates for every scenario: final completeness 100%, duplicates 0, stale 0, non-final HISTORY items 0, connection errors 0. T1/T2 also need Normal Update Coverage ≥ 99%. T3 also needs normal-client P95 ≤ 100 ms and slow-client P95 ≤ 15 s (measured ~10 ms / ~9 s; the `--no-coalesce` baseline gives ~28 s and 95% finals). Every run must also contain our server's `listening` line (a stale server on the port would otherwise be measured). Each scenario's harness + server output is kept in `results/check_<scenario>.log`; the table shows the server's peak RSS and `[stats]` peaks per run.

Stress scenarios (`--stress`, Day 3) add a server RSS ceiling of 200 MB and P95 limits of ~3x what the Mininet VM measured (results in `HANDOFF.md`).

| Scenario | Quick params | Added by |
|---|---|---|
| t1 | 20 clients, 15 s | Day 1 |
| t2 | 100 clients, 25 s | Day 1 |
| t3 | 100 clients, 10% slow @ 500 ms, 40 s (gated + P95 limits) | Day 1 (gated Day 2) |
| t4 | 100 clients, 20% disconnect @ 10 s for 5 s, 30 s | Day 1 |
| late | 100 clients, 20% join after 8 s, 25 s | Day 1 |
| s300 | `--stress`: 300 clients, 40 s; P95 ≤ 100 ms, RSS ≤ 200 MB | Day 3 |
| s500 | `--stress`: 500 clients, 40 s; P95 ≤ 150 ms, RSS ≤ 200 MB | Day 3 |
| s500slow | `--stress`: 500 clients, 10% slow @ 500 ms, 40 s; normal P95 ≤ 150 ms, slow P95 ≤ 15 s, RSS ≤ 200 MB | Day 3 |

## 3. Constrained bandwidth (Day 3; Ubuntu, needs `sudo tc`)

```bash
python bandwidth_test.py --apply --clients 500 --duration 60 --runs 3   # shape, run before/after, reset
python bandwidth_test.py --unlimited --clients 500 --duration 30        # the demand with no limit
```

`--apply` shapes **only the server port** with the public profile's parameters (`tbf rate 2mbit burst 32kbit
latency 400ms`, see the script's docstring for why not `bandwidth_2mbit.sh` as is) and runs the unmodified
`run_demo.py` twice per run, interleaved: `LIVERECALL_NO_COALESCE=1` (plain FIFO, "before") and the real server
("after"). It prints one table (medians over runs) with the harness metrics plus the evidence the harness cannot
see: link Mbit/s from `/proc/net/dev`, tbf drops from `tc -s qdisc`, the server's `[stats]` peaks (lagging clients,
coalesced provisionals, hopeless closes, `max_pending`) and its RSS. Files: `results/bandwidth_*` (gitignored).
Pick the client count from the demand run: 100 clients need ~0.6 Mbit/s and never saturate the 2 Mbit/s link;
**500 clients** put both configurations over it (plain FIFO ~2.9 Mbit/s; Day 3's lag-aware code ~5.4 Mbit/s because
it pinged after every send, which Day 4's ping budget removed). There is no pass/fail gate: the numbers go into
`HANDOFF.md` and the report's before/after comparison. `tests/test_bandwidth_tool.py` covers the tc parsing and the
rate maths; `tests/test_check.py` the server-log parsing and the gate rules shared with `check.py`. The policy fix
this experiment called for was done on Day 4 (`HANDOFF.md`, Day 4 parts 1–2): final code 100% vs FIFO ~98% finals.
Always check `tc qdisc show dev lo` prints `noqueue` afterwards; `sudo ./network_profiles/reset.sh lo` if not.

## 4. Slow-then-fast convergence (Day 3; any OS, no root)

```bash
python convergence_test.py --runs 3                        # 100 normal + 10 converging clients, slow for 30 s of 120 s, before/after
python convergence_test.py --slow-until 90 --duration 230  # slow longer than RECOVER_S (60 s): the relapse/backoff path
```

Runs our server and the public source with the public client code imported unmodified: the converging clients get a
`slow` flag that turns false at the switch, so they pause 500 ms per message like T3's slow readers and then read at
full speed. `analyse_results.analyse_file` gives the public metrics (the converging clients are its "slow" role); on
top, per converging client relative to the switch: backlog at the switch, seconds until every message is fresh
(≤ 500 ms), seconds until the first live provisional (= the server is back to full delivery), settled P95 and the
provisional share of the last 30 s, and the drain time from the client's first read after the switch (the harness
pauses *after* each message, so the first read can come up to 500 ms after the switch). Exit 1 when an "after" run
(our server) has < 100% finals, duplicates, stale or connection errors, when a server is not ours / in the wrong mode,
or when a converging client never got full delivery back (judged only when the fast phase exceeds `RECOVER_MAX_S` +
10 s = 130 s). The plain-FIFO baseline's problems are printed, not failed. Result names carry every timing setting,
and `--only` refuses to merge into a summary made with other settings. `tests/test_convergence_tool.py`
covers the metric definitions. Results: `results/convergence_*` (gitignored); numbers in `HANDOFF.md`.

## 4b. Hidden-evaluation variations (`hidden_sim.py`; Linux, shaped scenarios need root for `tc`)

```bash
python hidden_sim.py --list                         # the 25 scenarios
python hidden_sim.py                                # all, before (plain FIFO) + after (ours), ~1 h
python hidden_sim.py --runs 3 --before-runs 1       # medians of 3 for ours, 1 baseline run, ~2 h
python hidden_sim.py --only adv2 --runs 3           # one component (adv1 | adv2 | recon | delay | scale) or scenario names
```

More settings for the three harder hidden components than the public tests give. Every scenario stays inside the teaching team's 2 Nov bounds: no reader slower than the finals stream, no link narrower than finals need.
- **adv1 (slow readers):** 30% slow; readers at 250 ms and 700 ms; a 3× faster source with 150 ms readers; 8 revisions per segment; 3 s bursts at 8× every 10 s.
- **adv2 (bandwidth):** 1 Mbit/s with 250 clients; 2 Mbit/s with 8 revisions per segment; 2 Mbit/s combined with 10% slow readers or with 1% loss; the limit lifted after 30 s (catch-up); reconnects and 60 s late joins under the limit.
- **recon:** 100% of clients reconnecting at once; 50% away for 30 s; 20% away for 75 s; reconnects on the T5 link; flapping clients (20% drop every ~8 s) and flapping slow readers (lag memory).
- **recon / adv2, large HISTORY:** non-repeating text with joins at 55 s asking 60 s, unshaped and on 2 Mbit/s (a big HISTORY reply on the shaped link: see the MSS cap in `HANDOFF.md`).
- **delay:** 300 ± 100 ms each way, alone, with 3% loss, and with 10% slow readers.
- **scale:** 1000 clients, and 1500 with 10% slow readers, spread over 4 / 6 load processes. One `load_clients` process tops out near 500.

How it runs:
- Same machinery as `convergence_test.py`: the public client code imported unmodified, roles assigned per client, the public source with its own `--revision-delay/--segment-gap`, and `analyse_results`.
- **More revisions / bursts:** these scenarios use `custom_source.py`, a test-only source speaking the same protocol, with `--revisions N` and `--burst-every/--burst-len/--burst-factor`. Every other scenario uses the public `caption_source.py`.
- **Flapping:** the tool wraps the harness's `websockets.connect` for the chosen clients, so their connection is aborted every ~N s. The public client then reconnects with its own resume state and logs as usual.
- **Multi-process:** workers (`hidden_sim.py --worker ...`) share one wall-clock start, and their CSVs are merged before analysis.
- Impairment comes from `tc` on the test port only (prio → netem → tbf), optionally only for part of the run, and is always reset.
- Extra column `tail P95`: normal clients over the last 30 s, to show convergence after an impairment ends.
- Extra column **`due %`**: completeness over the finals the source sent at least `GRACE_S` (3 s) before the clients stopped.
  - This mirrors the evaluator's "short bounded grace period" (teaching team, 2 Nov).
  - On a shaped link the source's own handshake can be delayed a few seconds, so its last finals go out just as the clients stop. That's a measurement artifact, not a server loss.
  - Finals sent earlier and never delivered still count as lost.
- An "after" run fails on due < 100% or any dup/stale/non-final HISTORY item. Connection errors are reported, not failed.
- `tests/test_hidden_sim.py` covers the scenario bounds, roles, flappers, source selection, the flap wrapper, CSV merging, the tc chain, the metrics and `custom_source.py`'s revisions/bursts.

**Latest results** (3 Nov, WSL2, `--runs 3 --before-runs 1`, final code):
- **Ours:** 100% of due finals in all 3 runs of 24 of the 25 scenarios; `bw2m_slow` 99.96–99.99%. 0 duplicates/stale in all 100 runs.
- **Plain FIFO:** falls short in 7 scenarios (86.7–96.4%).
- Full table and trade-offs in `HANDOFF.md` "Update 3 Nov"; known limitations in `CLAUDE.md`.

**Large-HISTORY stall probe** (the bug behind the TCP segment cap): a 20.8 KB incompressible HISTORY through the 2 Mbit/s tbf never arrives with the cap off, and arrives in 0.04 s with it. It's not a committed script; its setup (2 Mbit/s tbf on the port, 40 incompressible finals, one client joining with `history_seconds: 60`) and results are in `HANDOFF.md` "Update 2 Nov (3)". `test_listening_socket_caps_segment_size` guards the fix itself.

## 5. Manual checklist (browser, Day 3)

Automated in `tests/test_client.py` (real server + page in headless Firefox via selenium; skipped where
Firefox/geckodriver are missing; on WSL2, `snap install firefox` provides both under `/snap/bin`): in-place revision replacement and provisional/final styling,
recall 15/30/60 s from local state only with each line's age and the Hide button, drop → auto-reconnect →
HISTORY recovery with no duplicates, resume from the latest final and the status line's "recovered N missed
captions" message, late join in a second tab, server+source restart detection.
Still check by hand before the demo (`python -m http.server 8000 --directory client`):

- [ ] Two tabs show the same live stream; provisional rows grey/italic with a yellow "… provisional" badge, finals dark with a green "✓ final" badge; no duplicate lines
- [ ] "What did I miss?" defaults to 30 s; 15/30/60 s switch the window; the panel updates as new finals arrive
- [ ] "Simulate 5 s network drop" (Demo tools row) → status red → auto-reconnect → status says "recovered N missed captions (segments a–b), 0 duplicates"
- [ ] Each recall line shows "N s ago"; a 60 s recall scrolls inside its panel; Hide closes it
- [ ] No captions for 30 s (before the source starts or after it ends): status turns yellow "no captions for 30 s, re-checking the link", not red, then green again
- [ ] New tab mid-lecture: the recall panel shows the recent context at once, then live rows follow
- [ ] Browser on a different host: open `http://<server-ip>:8000/` (connects to `ws://<server-ip>:8765` by default) or pass `?ws=ws://<server-ip>:8765`
- [ ] Restart server + source mid-lecture (stopping the server also ends the source, so numbering restarts at 1): the tab shows "stream restarted, rejoining" and continues with the new run (stats: restarts 1)

## Still to add (owners per CLAUDE.md)

- [x] Day 2: t3 gated, with normal/slow P95 limits
- [x] Day 3: bandwidth-test script + runs (section 3; results in `HANDOFF.md`)
- [x] Day 3: `check.py --stress` (section 2; medians in `HANDOFF.md`)
- [x] Day 3: slow-then-fast convergence client (section 4)
- [x] Day 4: bandwidth policy fix + re-run section 3 (cloud container, median of 3; re-run on the reference VM)
- [x] Day 4: T5 tc/netem ×3 (all 4 profiles) and the 5-minute memory run with `monitor_server.py` (reference VM, `HANDOFF.md` Day 4 part 2)
- [x] 2–3 Nov: Python 3.8 test run; `hidden_sim.py` (25 scenarios, median of 3); unit tests for the history byte cap, `LagMemory`, reader reclassification and the TCP segment cap
- [x] 3 Nov: the 6 browser tests on the final code (WSL2 + Firefox snap): 6 passed
- [ ] Final code on the reference VM: T3 before/after ×3 (Frankfurters, task 6 in `CLAUDE.md`)
- [ ] Manual browser checklist (§5), incl. a browser on another host, before recording the demo video (QiAn)
