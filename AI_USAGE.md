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

### Day 3 (QiAn) — browser client, constrained-bandwidth experiment, stress gate, convergence test

- **Tool:** Claude Code (Claude Opus 5.5, then Fable 5.1), with reviewer and skeptic sub-agents on each step.
- **Used for:** explaining the Day 1–2 code and the metrics in plain words, the browser client (`client/store.js`, `client/app.js`) and its headless-Firefox tests, `bandwidth_test.py`, running the bandwidth experiments and reading the CSVs, `check.py --stress` (300/500-client scenarios, server RSS and `[stats]` per run), `convergence_test.py` (slow-then-fast clients), drafting the handoff. Each step's code was checked by a review workflow (independent reviewers per concern, each finding re-checked by three skeptics) before I reviewed it myself.
- **Useful suggestions:**
  1. Measure the link *demand* before choosing the client count. Day 1's estimate suggested ~300 clients; the demand run showed the plain-FIFO baseline still fits at 300 and only the lag-aware server exceeds the cap (its ping after every send doubles its traffic), so 500 is the first fair load.
  2. Shape only the server port rather than all of `lo`: this VS Code session's SSH tunnel also runs over `lo` and would have been throttled, possibly killing the session mid-experiment.
  3. In the browser, route HISTORY items and live updates through the same acceptance rule as the server's, so recovered finals can never duplicate or overwrite what is shown.
- **Suggestions that were wrong or changed:**
  1. From a bulk-transfer probe (a 2 MB transfer stalls at 0 B under the profile's 4000-byte burst because loopback's MTU is 64 KB) it predicted the public profile would stall the caption stream on `lo`, and changed the script's default burst to the MTU. The real harness run with the profile's parameters showed no stall at all: per-connection backlogs stay a few hundred bytes, so TCP never builds oversized segments. Reverted to the profile's exact parameters; the probe is kept only as a documented caveat for single messages over 4000 B (e.g. a large HISTORY).
  2. The experiment was framed to *demonstrate* that the Day 2 policy beats a plain queue under constrained bandwidth. The data said the opposite (plain FIFO 100% complete / P95 1.6 s vs 94.3% / 9.3 s at 500 clients). The handoff reports it as a design finding with a proposed fix instead of tuning the setup until it looked good.
  3. Its first restart detection in the browser store reset on a live update at or below the newest final, but when that update was itself a *final* the page resumed after it and skipped the new run's earlier finals. A skeptic pass found it; fixed with a second reset in `applyUpdate` plus a test that fails without it.
  4. **Convergence tool.** Its useful idea: reuse the instructor's client code unmodified by passing a `slow` flag that turns false at a set time, so the converging clients behave exactly like T3's slow readers. Its first "full delivery after" metric (first provisional after the switch) reported 0.4 s in the 90 s-slow run, while the raw CSV showed that provisional was a leftover from a brief lag exit and none followed for 110 s. Redefined as "provisionals flow without a gap > 5 s until the end", with a test built from that case. The real number was 120 s: a policy finding, not a tool bug. Its first long run (200 s) was also too short to observe recovery after a relapse (up to 120 s); the review caught it, and the tool now only judges "never recovered" when the run is long enough.
  5. Its sampler thread shadowed `threading.Thread._stop` with an `Event` named `_stop`, so `join()` crashed; caught on the first smoke test.
  6. Its first stress gate scanned `/proc` without a guard, so on Windows/macOS the RSS sampler thread would die with a traceback and `--stress` would fail with "RSS max n/a", which reads like a memory problem. The review caught it; now the PID lookup returns nothing there and the gate says the RSS needs Linux. The same review found that the server's `[stats]` lines can be glued to another process's output in the shared log (one real log line read `source disconnectedSOURCE_DONE: ...`), so the parser now searches for `[stats` anywhere in the line. It also proposed 21 other findings that its own skeptics refuted (e.g. that the PID order could map RSS to the wrong run: each run's server exits before the next starts).
- **What I checked myself:**
  1. Nothing was committed by the assistant. For each step I had the changes explained file by file in plain words, asked follow-ups until I could explain them without the chat (why the store treats HISTORY and live updates with one rule, why the resume cursor is the latest *final* and not the latest revision, why restart detection needs a second reset, why the lag policy pings after every send), then read the diff and committed and pushed it under my own name.
  2. Before accepting the bandwidth result I checked the environment first, as `CLAUDE.md` requires: this VM is the team's reference Mininet VM, `check.py`'s timer drift is +1%, the shaping was applied to the server port only and `tc qdisc show dev lo` was back to `noqueue` after every run. I also checked that the "before" and "after" logs really ran in the modes they claim (the baseline line appears in every plain-FIFO log and in no lag-aware log); that check is now built into the script.
  3. I re-read the assignment's mandatory public tests against our state rather than taking the assistant's summary: T1–T4 are gated in `check.py`, T5 has only one WSL2 run, so Day 4 must re-run T1–T5 three times on one VM with valid timers, using the scripted tools rather than hand-typed `tc` commands, so that the report's numbers are reproducible and consistent across teammates.
  4. Decisions that were mine, not the tool's: the scope of the passwordless `sudo` rule (`/usr/sbin/tc` only, on this VM only); reporting the constrained-bandwidth loss honestly as a finding instead of re-tuning the experiment; and keeping the browser client's recall purely local (no request to the server), as the spec requires.

### Day 4 part 1 (Frankfurters) — lag-policy fixes for slow links and recovery

- **Tool:** Claude Code (Claude Opus 5.5), cloud session.
- **Used for:** reproducing Day 3's constrained-bandwidth result, redesigning the lag policy in `liverecall/channel.py` (ping budget, no stop-and-wait, lag cause from the kernel's TCP RTT, recovery jitter, hopeless-by-unanswered-ping), small per-client send buffers in `solution_server.py`, unit tests, and running the bandwidth / convergence / T3 / stress experiments.
- **Useful suggestions:**
  1. Read the kernel's smoothed TCP RTT (`TCP_INFO`) to tell a slow *link* from a slow *reader*: the client's kernel ACKs data whether or not its app reads, so a late pong with a low kernel RTT can only be the reader. It costs one `getsockopt` per lag decision and no network traffic.
  2. Look at caption age in 5 s windows instead of only the run's P95. That showed all 500 clients retrying full delivery in the same second (P50 ~80 ms between retries, ~900 ms during them), which ±50% jitter on the recovery wait fixed.
- **Suggestions that were wrong or changed:**
  1. Day 3's handoff (also Claude-drafted) proposed probing slow-reader recovery with one segment's provisionals. Working through the websockets client's 16-message read-ahead showed it can't detect anything: 2 extra messages fit in the read-ahead and the pong stays fast for slow and fast readers alike.
  2. Its first fix cut the recovery wait to 10 s for *every* lagging client. Convergence would have improved, but the T3 gate failed at 98.3% finals: each failed retry refills a slow reader's read-ahead (~8 s of backlog), and finals still unread when the run ends count as missing. Changed to 10 s for slow links only and kept 60 s for slow readers, with the T3 and convergence numbers in `HANDOFF.md`.
  3. It added a "send blocked > 50 ms = congested link" mode with `TCP_NOTSENT_LOWAT`. A debug counter showed it never fired in three 500-client runs (the backlog sits in the shaper's shared queue, not in our socket), so the mode was removed. The small buffers stayed because a with/without run showed they help on their own.
- **What I checked myself:** TODO (Frankfurters: read the diff and the "Why not for readers" paragraph in `HANDOFF.md` until you can explain the ping budget, the RTT classifier and why slow readers keep 60 s without notes).

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
