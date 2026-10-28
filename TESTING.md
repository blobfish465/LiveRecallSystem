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
```

`check.py` first prints `asyncio timer drift`. If it warns (> +10%), **stop**: this machine's timers are unreliable (typically VirtualBox on a Hyper-V host) and every metric will be wrong. See "Test environments" in `HANDOFF.md`.

Gates for every scenario: final completeness 100%, duplicates 0, stale 0, non-final HISTORY items 0, connection errors 0. T1/T2 also need Normal Update Coverage ≥ 99%. T3 also needs normal-client P95 ≤ 100 ms and slow-client P95 ≤ 15 s (measured ~10 ms / ~9 s; the `--no-coalesce` baseline gives ~28 s and 95% finals).

| Scenario | Quick params | Added by |
|---|---|---|
| t1 | 20 clients, 15 s | Day 1 |
| t2 | 100 clients, 25 s | Day 1 |
| t3 | 100 clients, 10% slow @ 500 ms, 40 s (gated + P95 limits) | Day 1 (gated Day 2) |
| t4 | 100 clients, 20% disconnect @ 10 s for 5 s, 30 s | Day 1 |
| late | 100 clients, 20% join after 8 s, 25 s | Day 1 |

## 3. Manual checklist (browser, Day 3)

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
- [ ] Bandwidth profile run (moved from Day 2 to Day 3, with the bandwidth-test script)
- [ ] Day 3: `check.py --stress` (300–500 clients), slow-then-fast convergence client, bandwidth-test script
- [ ] Day 4: T5 tc/netem, 5-minute memory run with `monitor_server.py`
