# Team 14 - LiveRecall

> Work in progress. Sections marked TODO are filled in by later days (see `CLAUDE.md`).

## Team members

- Tay Cole Lin / A0210841U / server core (revision state, bounded history, reconnect/late-join recovery), test suite and regression gate, hidden-condition testing and fixes, submission
- Muhammad Bin Hamzah / A0272938N / overload policy v1 (slow-reader backpressure), report figures and charts
- Ong Qi An / A0281202W / browser client, constrained-bandwidth, stress and convergence tooling, demo video
- Nicholas Kee / A0308575W / overload policy v2 (slow links), reference-VM experiments, report assembly

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

Plain HTML and JavaScript in `client/`, with no build step and no extra dependencies. Any static file server works; Python's is used below.

```bash
# terminal 1: server
python solution_server.py --port 8765
# terminal 2: serve the page
python -m http.server 8000 --directory client
# terminal 3: captions (any duration over 60 s makes the 60 s recall meaningful)
python caption_source.py --duration 300
```

Open `http://127.0.0.1:8000` in two or more browser tabs. Each tab is a separate client.

**Browser on another machine.** Open `http://<server-ip>:8000/`. The page connects to `ws://<server-ip>:8765` by default, because it uses the host it was loaded from. Settings can be changed in the page URL:

| URL parameter | Default | Meaning |
|---|---|---|
| `?ws=ws://10.0.2.15:8765` | this page's host, port 8765 | server address |
| `?session=L1` | `L1` | session name sent in `CLIENT_HELLO` |
| `?history=60` | `60` | seconds of context requested on a fresh join (1–60) |

**What the page does**

- **Live captions.** One row per segment, updated in place when a newer revision arrives, so revisions never appear as duplicate lines. Provisional rows are grey and italic with a yellow "… provisional" badge; final rows are dark with a green "✓ final" badge. The live panel shows the newest 10 segments.
- **Revision rules.** `client/store.js` applies the same rules as the server (`liverecall/state.py`). A newer revision replaces the row. The same revision again is ignored as a duplicate. An older revision, or anything after the segment's final, is ignored as stale. HISTORY items and live updates go through the same check, so a recovered final can never be shown twice.
- **"What did I miss?"** Opens on the last 30 s, with 15 / 30 / 60 s buttons. It reads only finals already stored in the page, so no request goes to the server and it keeps working while disconnected (the panel then says the list may be incomplete). Time is measured on the lecture clock (`source_ts`), not the browser's clock, so it stays correct when browser and server clocks differ. Each line is labelled with how long ago it was said ("42 s ago"), the panel scrolls instead of growing the page, and "Hide" closes it. The page keeps 120 s of finals and at most 600 segments; older ones are dropped.
- **Connection state.** Always visible at the top: green "connected", yellow "connecting", red "disconnected" with the reason (for example "server unreachable") and the time until the next retry.
- **Automatic reconnect and incremental recovery.** After a drop the page retries after 0.5 s, doubling up to 5 s between attempts. On reconnect, `CLIENT_HELLO` reports the latest *final* the page holds (never a provisional), so the server's HISTORY contains only the finals it missed. The status line then says what came back, for example "connected (resumed after segment 41): recovered 13 missed captions (segments 42–54), 0 duplicates", and the recovered context opens in the recall panel straight away.
- **Late join.** A new tab sends `last_segment = 0` and asks for 60 s of context; it shows that context, then continues with live updates.
- **Dead-link watchdog.** Browsers cannot see WebSocket pings, so if nothing arrives for 30 s the page closes the connection and rejoins. This shows as yellow "no captions for 30 s, re-checking the link", not as a failure, because a pause in the lecture looks the same.
- **Server restart.** If the server and source restart mid-lecture, segment numbering starts again at 1. The page detects this, shows "stream restarted, rejoining", clears its old captions and joins the new run as a fresh client.
- **Demo helper.** "Simulate 5 s network drop", in the "Demo tools" row at the bottom, closes the connection for 5 s while the lecture continues, to show reconnect and recovery.
- **Stats line** at the bottom: connections, live updates, recovered finals, duplicates, stale, invalid messages, restarts and segments held. Duplicates and stale should stay at 0. The same counters are available in the browser console as `liverecall.counters`, and the stored captions as `liverecall.store`.

**Files.** `index.html` holds the layout and styles. `store.js` holds the caption state and rules, with no DOM or network code. `app.js` holds the WebSocket connection, rendering, recall, reconnect and the stats line.

**Tests.** `tests/test_client.py` runs the real server and drives the page in headless Firefox: in-place revision replacement, recall from local state only, drop and reconnect with no duplicate finals, late join in a second tab, and server restart detection. They need Firefox and geckodriver (on Ubuntu, `sudo snap install firefox` provides both) plus `selenium` from `requirements-dev.txt`, and are skipped when those are missing. The manual checklist for the demo is in `TESTING.md`, section 5.

```bash
python -m pytest -m browser -v      # browser tests only
```

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

Demo video (5–7 min): VIDEO_LINK

Uses the submitted server and browser client with the supplied `caption_source.py`. Activate the venv in each terminal.

```bash
# terminal 1: server
python solution_server.py --port 8765
# terminal 2: the page
python -m http.server 8000 --directory client
# terminal 3: the lecture, started within 30 s of opening the pages
python caption_source.py --duration 900 --revision-delay 1.0 --segment-gap 0.8
```

`--revision-delay 1.0 --segment-gap 0.8` only slows the same deterministic source down, so that each provisional row stays on screen for a second and the replacement is visible. The default pacing (0.2 s between revisions) works the same way, just faster.

1. **Two clients.** Open `http://127.0.0.1:8000` in two browser windows side by side (or `http://<server-ip>:8000` from another machine). Both show green "connected (fresh join)" and the same captions.
2. **Revision replacement.** A row appears grey and italic as `#12 r1` with a yellow "… provisional" badge, changes in place to `r2`, and turns dark as `r3` with a green "✓ final" badge when the final arrives. No line is ever duplicated.
3. **What did I miss?** After at least 60 s of lecture, click "What did I miss? (30 s)": it lists the finals of the last 30 s, each with its age. Click 15 s and 60 s to change the window. The stats line's `connections` count does not change, because recall uses only captions stored in the page.
4. **Disconnect and incremental recovery.** In one window click "Simulate 5 s network drop". The status turns red, then reconnects on its own and reports for example "recovered 2 missed captions (segments 30–31), 0 duplicates". Terminal 1 shows `client connected: id=... resume=(29,3) history_items=2`: only the missed finals were sent, not the whole lecture.
5. **Late join.** Open a third window mid-lecture. It shows "fresh join: N recent captions loaded", opens the last 30 s in the recall panel, and continues with live rows.

**Restart the server before re-running the source.** Each `caption_source.py` run numbers its segments from 1. A server that already holds a lecture treats a second run's segments as old and ignores them, so the pages stay frozen. Stop terminal 1 (which also ends the source), start the server, then the source: open pages show "stream restarted, rejoining" and follow the new lecture.

## Design notes

- **Revision state** (`liverecall/state.py`): per-segment latest revision; updates with revision ≤ current, or for an already-final segment, are rejected and never forwarded. Finals are durable.
- **Bounded server history**: finals kept in a deque bounded by 90 s **and** 6000 items (60 s of recovery even at ~100 segments/s), each HISTORY reply capped at 768 KiB; stale updates for evicted segments are rejected via a single "evicted floor" integer.
- **Reconnect / late join**: on `CLIENT_HELLO` the server snapshots HISTORY and registers the client with no `await` in between (atomic in asyncio), so each final reaches the client exactly once. Fresh joins get the last `history_seconds` (≤ 60 s); resumes get every retained final after `last_segment`.
- **Per-client buffering** (`liverecall/channel.py`): one bounded queue + sender task per client; the source never awaits a client. TODO (Day 2): lag-aware coalescing and bandwidth policy.
- **Overload behaviour**: TODO (Day 2).

## Optional ASR bonus

Not attempted (yet).
