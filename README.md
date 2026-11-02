# Team XX - LiveRecall

> Work in progress. Sections marked TODO are filled in by later days (see `CLAUDE.md`).

## Team members

- TODO: Name / student ID / responsibility (×4)

## Ubuntu setup

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip iproute2
```

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt          # server only
pip install -r requirements-dev.txt      # + pytest, for tests
```

## Required server

```bash
python solution_server.py --port 8765
```

The server binds `0.0.0.0`, needs only `websockets`, and prints a one-line `[stats]` summary every 10 s (clients, history size, max per-client queue, dropped provisionals).

## Browser client

TODO (Day 3).

## Tests

See `TESTING.md`.

```bash
python -m pytest -q        # unit + in-process integration tests
python check.py            # regression gate on the public harness
```

## Public deterministic tests

All reported numbers are the **median of 3 independent runs** (`--runs 3`; `run_demo.py` restarts the server for each run) through the **unmodified** instructor harness. Run from the repo root with the venv active. Each command writes `<stem>_runNN.{csv,json}` and prints the analyser's summary.

**Check the environment first.** Every component is paced by asyncio timers, so a VM with late timers produces meaningless results. `python check.py` prints `asyncio timer drift` as its first line; it must be below 10%. To check drift on its own:

```bash
python -c "import check; print(f'timer drift {check.timer_drift():+.0%}')"
```

| Test | Command |
|---|---|
| T1 basic correctness | `python run_demo.py --clients 20 --duration 20 --runs 3 --output-stem t1` |
| T2 public scale | `python run_demo.py --clients 100 --duration 40 --runs 3 --output-stem t2` |
| T3 slow readers | `python run_demo.py --clients 100 --duration 60 --runs 3 --slow-percent 10 --slow-ms 500 --output-stem t3` |
| T4 reconnect | `python run_demo.py --clients 100 --duration 60 --runs 3 --disconnect-percent 20 --disconnect-at 15 --disconnect-seconds 5 --output-stem t4` |
| Late join (extra) | `python run_demo.py --clients 100 --duration 40 --runs 3 --late-percent 20 --late-seconds 15 --output-stem late` |

**T5, network impairment** (Ubuntu only; `tc` needs root). Apply one profile to the loopback interface, run, and **always reset afterwards**:

```bash
sudo ./network_profiles/delay_loss.sh lo        # public T5 profile: 120 ± 30 ms delay each way + 3% loss
python run_demo.py --clients 100 --duration 40 --runs 3 --output-stem t5_delay_loss
sudo ./network_profiles/reset.sh lo
```

We ran the same command with each profile: `delay_loss.sh`, `delay_100ms.sh`, `loss_3pct.sh`, `reorder.sh` (stems `t5_<profile>`). `tc` shapes all of `lo`, including any SSH/VS Code session tunnelled over it. To avoid typing a password per run, a rule limited to `tc` works: `echo "$USER ALL=(root) NOPASSWD: /usr/sbin/tc" | sudo tee /etc/sudoers.d/liverecall-tc`.

**Before/after (plain queue).** Setting `LIVERECALL_NO_COALESCE=1` makes the server run as a plain FIFO per client (no lag detection or coalescing, Day 3 socket settings); `run_demo.py` passes the environment through to the server it starts. For example, T3 without our overload policy:

```bash
LIVERECALL_NO_COALESCE=1 python run_demo.py --clients 100 --duration 60 --runs 3 --slow-percent 10 --slow-ms 500 --output-stem t3_before
```

### Our test tools

| Tool | What it does | Command |
|---|---|---|
| `pytest` | Unit tests (state, channel policy) + in-process server tests on real sockets | `python -m pytest -q` (~20 s) |
| `check.py` | Regression gate: runs T1–T4 + late join through `run_demo.py`, fails on final completeness < 100%, any duplicate/stale, low coverage or slow P95; also reports the server's RSS and `[stats]` peaks per run | `python check.py` (quick durations, ~3 min) · `python check.py --full --runs 3` (public durations) · `--only t3,t4` |
| `check.py --stress` | 300 clients, 500 clients, 500 clients with 10% slow readers, same gate | `python check.py --stress --runs 3` |
| `bandwidth_test.py` | Constrained bandwidth: 2 Mbit/s token bucket (the public `bandwidth_2mbit.sh` profile) on the server port only, plain FIFO vs our server, interleaved; reports link usage, tbf drops and the server's stats | `python bandwidth_test.py --apply --clients 500 --duration 60 --runs 3` (needs `sudo tc`; resets `lo` itself) |
| `convergence_test.py` | Clients that read slowly, then speed up (instructor's client code, imported unmodified): how fast freshness and full delivery return, FIFO vs our server | `python convergence_test.py --runs 3` · long case: `python convergence_test.py --slow-until 90 --duration 230` |
| Memory run | 5 minutes of T3 load with `monitor_server.py` sampling the server's RSS every second | see below |

5-minute memory run. Start the load, then attach the monitor to the **server's** PID. Match the Python command, not just `solution_server.py`: when the commands run through `bash -c '...'` (scripts, CI, coding assistants), that shell's own command line contains the name and `pgrep` picks it (a 3.6 MB "server").

```bash
python run_demo.py --clients 100 --duration 300 --runs 1 --slow-percent 10 --slow-ms 500 --output-stem mem5min &
sleep 3
python monitor_server.py --pid "$(pgrep -f 'python -u solution_server.py' | head -1)" --interval 1 --duration 300 --output mem5min_rss.csv
wait
```

## Mandatory deterministic application demo

TODO (Day 3/4).

## Design notes

- **Revision state** (`liverecall/state.py`): per-segment latest revision; updates with revision ≤ current, or for an already-final segment, are rejected and never forwarded. Finals are durable.
- **Bounded server history**: finals kept in a deque bounded by 90 s **and** 2000 items; stale updates for evicted segments are rejected via a single "evicted floor" integer.
- **Reconnect / late join**: on `CLIENT_HELLO` the server snapshots HISTORY and registers the client with no `await` in between (atomic in asyncio), so each final reaches the client exactly once. Fresh joins get the last `history_seconds` (≤ 60 s); resumes get every retained final after `last_segment`.
- **Per-client buffering** (`liverecall/channel.py`): one bounded queue + sender task per client; the source never awaits a client. TODO (Day 2): lag-aware coalescing and bandwidth policy.
- **Overload behaviour**: TODO (Day 2).

## Optional ASR bonus

Not attempted (yet).
