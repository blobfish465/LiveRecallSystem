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
python -m pytest -q
```

| File | Covers |
|---|---|
| `tests/test_state.py` | revision rules, final durability, malformed/unknown fields, variable revision counts (1/2/3/8), history bounded by time + count, evicted-segment rejection, active-segment cap, 1-hour boundedness, HISTORY selection (fresh vs resume, 60 s clamp, ordering, finals only) |
| `tests/test_channel.py` | control-first ordering, full delivery when not lagging, opt-in coalescing (in place, finals/control never replaced), lag detection by late pong (finals/control only, stop-and-wait), compaction on lag entry (no provisional after its final), recovery after fast pongs + backoff on relapse, hopeless cap (age + count) closes, send failure, offer() never blocks |
| `tests/test_server.py` | real WebSockets in-process: late join HISTORY→live, resume sends only missed finals, 30 clients joining during a burst see every final exactly once (atomic snapshot+register), source garbage/unknown types/SOURCE_DONE ignored, bad hello → 1008, abrupt client drop doesn't affect others |

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
**500 clients** put both configurations over it (plain FIFO ~2.9 Mbit/s, lag-aware ~5.4 Mbit/s, because it pings
after every send). There is no pass/fail gate: the numbers go into `HANDOFF.md` and the report's before/after
comparison. `tests/test_bandwidth_tool.py` covers the tc parsing and the rate maths; `tests/test_check.py` the server-log parsing and the gate rules shared with `check.py`. The policy fix this experiment calls for is handed to Day 4 (`HANDOFF.md`, Day 3). Always check `tc qdisc show dev lo`
prints `noqueue` afterwards; `sudo ./network_profiles/reset.sh lo` if not.

## 4. Manual checklist (browser, Day 3)

Automated in `tests/test_client.py` (real server + page in headless Firefox via selenium; skipped where
Firefox/geckodriver are missing, e.g. WSL2): in-place revision replacement and provisional/final styling,
recall 15/30/60 s from local state only, drop → auto-reconnect → HISTORY recovery with no duplicates and
resume from the latest final, late join in a second tab, server+source restart detection.
Still check by hand before the demo (`python -m http.server 8000 --directory client`):

- [ ] Two tabs show the same live stream; provisional rows grey/italic with …, finals dark with ✓; no duplicate lines
- [ ] "What did I miss?" defaults to 30 s; 15/30/60 s switch the window; the panel updates as new finals arrive
- [ ] "Simulate 5 s network drop" → status red → auto-reconnect → missed finals recovered (stats line: recovered N, duplicates 0)
- [ ] New tab mid-lecture: the recall panel shows the recent context at once, then live rows follow
- [ ] Browser on a different host: open `http://<server-ip>:8000/` (connects to `ws://<server-ip>:8765` by default) or pass `?ws=ws://<server-ip>:8765`
- [ ] Restart server + source mid-lecture (stopping the server also ends the source, so numbering restarts at 1): the tab shows "stream restarted, rejoining" and continues with the new run (stats: restarts 1)

## Still to add (owners per CLAUDE.md)

- [x] Day 2: t3 gated, with normal/slow P95 limits
- [x] Day 3: bandwidth-test script + runs (section 3; results in `HANDOFF.md`)
- [x] Day 3: `check.py --stress` (section 2; medians in `HANDOFF.md`)
- [ ] Day 3: slow-then-fast convergence client
- [ ] Day 4: bandwidth policy fix + re-run section 3; T5 tc/netem x3; 5-minute memory run with `monitor_server.py`
