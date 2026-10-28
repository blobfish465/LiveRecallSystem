# AI / Coding-Assistant Usage

> Running log: each member appends their day's entries, then we condense this into the final required sections before submission.

## Running log

### Day 1 (lihloway) — server state, history, recovery, client channel, tests

- **Tool:** Claude Code (Claude Opus 5.5).
- **Used for:** summarising the spec, planning the relay, drafting `liverecall/state.py`, `liverecall/channel.py`, the new `solution_server.py`, the pytest suite and `check.py`, and running the public harness.
- **Useful suggestion:** build the HISTORY snapshot and register the client's channel with no `await` in between, so asyncio can't deliver a CAPTION_UPDATE in the gap. Verified with `tests/test_server.py::test_joins_during_burst_see_every_final_exactly_once` (30 clients joining while 150 segments stream: every final exactly once) and the T4/late-join gates.
- **Suggestion that was changed/rejected:**
  1. An early idea was to also send the in-progress segment's latest provisional right after HISTORY on connect, so a late joiner sees the current line immediately. Rejected: the server only knows the client's latest *final*, not which provisional it already saw. After a very short reconnect the client may already hold that exact revision, and the harness counts a repeat as a `duplicate`. The next live revision arrives within ~0.35 s anyway.
  2. The first generated channel test asserted that every queued provisional had already been superseded. That is wrong by design: provisionals are only dropped once the queue is over its cap (full delivery under normal load is graded via Normal Update Coverage). The test was rewritten to check the cap, that all finals survive, and the exact drop count.
  3. **Test environment (the main one, see final section below):** the AI suggested running the Ubuntu experiments in a VirtualBox VM (and the original plan suggested Multipass). On this Windows laptop that produced invalid numbers, which it initially reported as test results before the root cause was found. Switched to WSL2 for development and added a timer-drift check.
- **What I checked myself:** TODO (lihloway: fill in what you read/verified so you can defend it in the viva).

### Day 2 (muhdbhz) — slow-reader backpressure

- **Tool:** Claude Code (Claude Opus 5.5, with Sonnet sub-agents for implementation).
- **Used for:** explaining the Day 1 code and the metrics, designing the lag/coalescing policy, implementing `liverecall/channel.py` changes and tests commit by commit, running the before/after and parameter measurements in WSL2.
- **Useful suggestion:** use pong delay as the lag signal. The websockets client stops reading (pings too) once 16 messages are unread, so a late pong reveals a backlog that never shows in our own queue. This took T3 from 95.1% to 100% finals.
- **Suggestions that were changed/rejected:**
  1. It proposed a 10 s recovery wait. Measured on T3: 10 s → 99.7% finals, 30 s → 98.9%, 60 s → 100% (each failed retry refills the backlog), so we used 60 s.
  2. It proposed a 30 s hopeless-client cap. Measured: it never fired, because our queue only sees the client's short pauses. Changed to 10 s, which fires for hopeless readers and never in T3.
  3. It first claimed slow clients would get sub-second latency. Wrong: the client library's 16-message read-ahead sets a floor of ~8–9 s P95, which we can't coalesce. Documented as a limitation.
  4. A review of its own lag-mode change found that provisionals queued *before* lag could be sent after their final (stale). Fixed with `_compact()` on lag entry, plus a test.
- **What I checked myself:** TODO (muhdbhz: fill in what you read/verified so you can defend it in the viva).

## Tools used

TODO (final): list all tools across the team.

## One useful suggestion

TODO (final).

## One suggestion that was wrong, inefficient, or required modification

**Suggestion:** to get Ubuntu numbers on a Windows laptop, the coding assistant (Claude Code) suggested running the public tests inside a VirtualBox Ubuntu VM. Our team plan, also drafted with its help, suggested `multipass launch ...` as the default. It set the VM up, ran the quick gate and the tc/netem profiles there, and reported the results.

**Problem:** the results were wrong, and not because of our server. Even plain T1 with no impairment scored 68.8% final completeness, and the **unmodified starter server scored 56.3%**, versus 100% for both on Windows. The cause: the laptop runs Windows 11 Home with Hyper-V active (WSL2 + Core isolation), so VirtualBox couldn't use VT-x and fell back to "NEM" mode on top of Hyper-V (`VBox.log`: `fall back to NEM: VT-x is not available`). In that mode the VM's clock is correct, but **asyncio timers fire 134–240% late**. The caption source, load clients and server are all paced by asyncio timers, so the source ran about 2× slow and the clients' fixed test window closed before the last finals were sent. Multipass on Windows Home also runs through VirtualBox, so it would have had the same problem. Had we trusted those numbers, we could have spent a day chasing a non-existent server bug, or put invalid results in the report.

**How we caught it:** comparing against the naive starter server (if *it* also "loses" finals, the environment is suspect), then measuring timers directly: `10 × asyncio.sleep(0.1)` took up to 3.4 s instead of 1.0 s.

**Correction:**
- Ran the Ubuntu development tests in **WSL2 Ubuntu 24.04** instead. It runs directly on Hyper-V, has accurate timers (+0% drift) and real `sch_netem`/`sch_tbf` kernel modules. We label these numbers "WSL2, dev only", because the spec says WSL isn't the reference environment.
- Added an **automatic timer-drift check to `check.py`**: it measures asyncio sleep overrun before every run and warns above 10%.
- Team decision: the report's final tables come from the **CS3103 Mininet lab VM**, run on a machine where the drift check passes.

**Lesson:** a benchmark harness measures the machine as much as the code. Validate the environment (timer accuracy, a known-good baseline) before trusting or debugging against the numbers.

## Responsibility statement

We understand that the team remains responsible for the submitted code, experimental results, and claims, and that every member may be asked to explain or modify relevant code during the viva.
