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
- **What I checked myself:** see the 2–3 Nov entry below, which covers both of my sessions.

### Day 2 (muhdbhz) — slow-reader backpressure

- **Tool:** Claude Code (Claude Opus 5.5, with Sonnet sub-agents for implementation).
- **Used for:** explaining the Day 1 code and the metrics, designing the lag/coalescing policy, implementing `liverecall/channel.py` changes and tests commit by commit, running the before/after and parameter measurements in WSL2.
- **Useful suggestion:** use pong delay as the lag signal. The websockets client stops reading (pings too) once 16 messages are unread, so a late pong reveals a backlog that never shows in our own queue. This took T3 from 95.1% to 100% finals.
- **Suggestions that were changed/rejected:**
  1. It proposed a 10 s recovery wait. Measured on T3: 10 s → 99.7% finals, 30 s → 98.9%, 60 s → 100% (each failed retry refills the backlog), so we used 60 s.
  2. It proposed a 30 s hopeless-client cap. Measured: it never fired, because our queue only sees the client's short pauses. Changed to 10 s, which fires for hopeless readers and never in T3.
  3. It first claimed slow clients would get sub-second latency. Wrong: the client library's 16-message read-ahead sets a floor of ~8–9 s P95, which we can't coalesce. Documented as a limitation.
  4. A review of its own lag-mode change found that provisionals queued *before* lag could be sent after their final (stale). Fixed with `_compact()` on lag entry, plus a test.
- **What I checked myself:**
  1. **The code path, line by line.** I traced one caption from `handle_source` through `CaptionState.apply()`, `broadcast()` and `offer()` to the sender loop in `run()`, then a client from `handler()` and `handle_client` to the `finally` cleanup. I asked for the exact line of every claim and read it myself. For example, I couldn't find where the server pings, and found that `ws.ping` is passed into the channel as `ping=` in `handle_client`. For each concept I had a diagram drawn (the channel's queue and `_latest` map, the normal and lagging lanes, the lag states) and checked it against the code.
  2. **Where the backlog really is.** I didn't take "16-message read-ahead" on trust. I checked that it is the websockets client's `max_queue` default, which the instructor's `load_clients.py` leaves unchanged, and that the kernel receive buffer is OS memory outside our code. That is why `max_pending` stays 0 in T3, and why a bounded queue alone can't fix freshness.
  3. **Corrections to my own understanding.**
     - `channel.py` runs on the server, not the client.
     - The read-ahead and our `_queue` are different queues on different machines.
     - A lagging client still holds the newest provisional of each segment.
     - `handle_client` runs once per connection.
     - The ping goes from the server to the client, and the client library answers it automatically.
  4. **Lag rules against the constants.** I traced lag entry and exit against the constants in `channel.py`:
     - what `_calm_since` records and what resets it;
     - where `_recover_wait` is set, including the jitter;
     - how a relapse is detected through `_left_lag_at` and `LagMemory`;
     - the three conditions in `_can_leave_lag()`.
  5. **Report figures against the data.** Figures 4 to 6 are generated from `report/data` by `make_charts.py`, not drawn by hand. I checked each footer against the number of runs in the data. The 5-minute memory test is one run each, not a median of 3, so Figure 6 says so. I did not let the assistant make up terminal screenshots for Appendix E. Those have to come from the reference VM.
  6. **Commits were my decision.** Nothing was committed or pushed without my go-ahead, and every commit is under my own name.

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
  6. **Hidden-condition probes (2 Nov).** Useful: running the unmodified harness on the final code under conditions the public tests do not cover found two that fall under the 99% completeness cap (slow readers at 0.7–1.0 s per message: 98.0% / 95.8%; slow readers behind the public delay_loss profile: 98.9%), each still better than the plain FIFO. Changed: its first reading of the delay_loss + slow-reader run (97.96%) blamed every lost final on the server. A per-client breakdown showed 68 of them were only the last segment, which arrived after the clients' window because loss had delayed the source's own connection, an artefact that affects any server. The clean re-run with server stats (98.92%, all lag entries classified "slow link") is the number we report.
  7. Its first stress gate scanned `/proc` without a guard, so on Windows/macOS the RSS sampler thread would die with a traceback and `--stress` would fail with "RSS max n/a", which reads like a memory problem. The review caught it; now the PID lookup returns nothing there and the gate says the RSS needs Linux. The same review found that the server's `[stats]` lines can be glued to another process's output in the shared log (one real log line read `source disconnectedSOURCE_DONE: ...`), so the parser now searches for `[stats` anywhere in the line. It also proposed 21 other findings that its own skeptics refuted (e.g. that the PID order could map RSS to the wrong run: each run's server exits before the next starts).
- **What I checked myself:**
  1. Nothing was committed by the assistant. For each step I had the changes explained file by file in plain words, asked follow-ups until I could explain them without the chat (why the store treats HISTORY and live updates with one rule, why the resume cursor is the latest *final* and not the latest revision, why restart detection needs a second reset, why the lag policy pings after every send), then read the diff and committed and pushed it under my own name.
  2. Before accepting the bandwidth result I checked the environment first, as `CLAUDE.md` requires: this VM is the team's reference Mininet VM, `check.py`'s timer drift is +1%, the shaping was applied to the server port only and `tc qdisc show dev lo` was back to `noqueue` after every run. I also checked that the "before" and "after" logs really ran in the modes they claim (the baseline line appears in every plain-FIFO log and in no lag-aware log); that check is now built into the script.
  3. I re-read the assignment's mandatory public tests against our state rather than taking the assistant's summary: T1–T4 are gated in `check.py`, T5 has only one WSL2 run, so Day 4 must re-run T1–T5 three times on one VM with valid timers, using the scripted tools rather than hand-typed `tc` commands, so that the report's numbers are reproducible and consistent across teammates.
  4. Decisions that were mine, not the tool's: the scope of the passwordless `sudo` rule (`/usr/sbin/tc` only, on this VM only); reporting the constrained-bandwidth loss honestly as a finding instead of re-tuning the experiment; and keeping the browser client's recall purely local (no request to the server), as the spec requires.

### Days 4–5 (Frankfurters) — lag-policy fixes, reference-VM runs, report results

- **Tool:** Claude Code (Claude Opus 5.5): a cloud session for Day 4 part 1, then a local session on the reference VM.
- **Used for:**
  - *Day 4 part 1:* reproducing Day 3's constrained-bandwidth result, redesigning the lag policy in `liverecall/channel.py` (ping budget, no stop-and-wait, lag cause from the kernel's TCP RTT, recovery jitter, hopeless-by-unanswered-ping), small per-client send buffers in `solution_server.py`, unit tests, and running the bandwidth / convergence / T3 / stress experiments.
  - *Day 4 part 2:* running T1–T5 x3, bandwidth before/after, convergence, stress and the 5-minute memory run; diagnosing two failures those runs exposed; fixing them in `liverecall/channel.py` with unit tests.
  - *Day 5:*
    - README test commands;
    - the results draft (`report/results.md`) and the chart data in `report/data/`;
    - the `CONTRIBUTIONS.md` draft;
    - explaining the system and metrics so I can present them;
    - `burst_test.py`, a faster/bursty caption source (spec section 10 says the hidden tests may vary "caption revision rate and burstiness").
- **Useful suggestions:**
  1. Read the kernel's smoothed TCP RTT (`TCP_INFO`) to tell a slow *link* from a slow *reader*: the client's kernel ACKs data whether or not its app reads, so a late pong with a low kernel RTT can only be the reader. It costs one `getsockopt` per lag decision and no network traffic.
  2. Look at caption age in 5 s windows instead of only the run's P95. That showed all 500 clients retrying full delivery in the same second (P50 ~80 ms between retries, ~900 ms during them), which ±50% jitter on the recovery wait fixed.
  3. Test a faster and burstier source with the instructor's `caption_source.py`, unmodified, using its own `--revision-delay`/`--segment-gap` options and a transcript that mixes 1- and 3-revision sentences. This covers a hidden-test variable that no public test exercises without touching the harness. Result: 100% finals and coverage at 5× and 12× speed and in back-to-back bursts.
- **Suggestions that were wrong or changed:**
  1. **A 10 s recovery wait for every lagging client.**
     - *Suggestion:* the first fix for slow convergence cut the wait before retrying full delivery from 60 s to 10 s for every lagging client.
     - *Why it was wrong:* this works for a slow *link* but not for a slow *reader*. Each retry sends every provisional again, so a slow reader's 16-message read-ahead fills up: about 8 s of backlog at 500 ms per message. Finals stuck behind that backlog when the run ends count as missing. T3 dropped to 98.3% finals, below the 99% line where the hidden-test score is capped.
     - *What we did:* 10 s for slow links (detected from the kernel's TCP RTT) and 60 s kept for slow readers. The T3 and convergence numbers for both are in `HANDOFF.md`.
  2. **A T5 fix that broke the bandwidth challenge.**
     - *Background:* on public T5 (`delay_loss`: 120 ms delay each way, 3% loss), TCP sometimes loses a packet carrying a ping or a pong. The retransmit waits at least 200 ms, so that pong comes back late. The link's kernel RTT (~240 ms) is above our 50 ms threshold, so the late pong was read as a slow link and the client switched to coalescing. Coverage dropped to 93.5% and bought nothing: plain FIFO on the same profile had 99.97% coverage with the same P95.
     - *Suggestion:* require "two late pongs in a row (or one very late, > 3 s)" before declaring slow-link lag. One retransmit only delays one pong, so T5 stopped entering lag.
     - *Why it was wrong:* on the 2 Mbit/s bandwidth test the links really are slow. Each time a link retries full delivery after 10 s and relapses, the rule makes the server wait for a second late pong before coalescing again. During that wait the full provisional stream piles up in the shared shaper queue. Fresh finals fell from 53% to 16%, 14 new clients' handshakes timed out, and one run had 98.97% finals (below the 99% cap line).
     - *What we did:* kept the rule for a link's *first* lag entry and added a relapse rule: a link already confirmed slow in the last 120 s is believed at its first late pong. Result: T5 coverage 99.93%, bandwidth fresh finals 50%, 100% finals in every run.
- **What I checked myself:**
  1. I read the Day 4 diff (`liverecall/channel.py`, `tighten_send_path` and `kernel_rtt` in `solution_server.py`) until I could explain the three main decisions without notes:
     - *Ping budget:* one ping per client per second (one per 3 s while lagging) instead of one per send. At 500 clients on 2 Mbit/s each client's share is ~500 B/s, so a ping after every 17-byte caption cost more than the caption itself. A stalled reader's pong is seconds late, so one ping a second still detects it.
     - *RTT classifier:* the client's kernel ACKs our data even when its app isn't reading, so the kernel's TCP RTT tells the two causes apart. A late pong with RTT under 50 ms is a slow reader; at 50 ms or more it's a slow link. The cause decides the retry wait, and on a slow link an unanswered ping isn't treated as hopeless.
     - *Why slow readers keep 60 s:* a cheap probe can't tell a fast reader from a T3 reader, because both answer pings from the client library's 16-message read-ahead. The only real test is full delivery, and for a reader that's still slow, one failed try leaves ~8 s of backlog that takes ~24 s to drain. With a 10 s wait, T3 dropped to 98.3% finals; with 60 s it was 100%. Finals are capped in grading and coverage isn't, so I kept 60 s and accepted the slower return to full delivery (~70 s).
  2. For the final report numbers I re-ran everything on the reference VM on the final commit (0e8982e). I checked the timer drift (+0–1%) and that `tc qdisc show dev lo` was back to `noqueue` after each impaired stage.
  3. I checked the report's numbers and constants against `report/data/`, `results/final/` and the code itself (`liverecall/channel.py`, `liverecall/state.py`), and corrected the ones that came from older runs or other environments.

### 2–3 Nov (lihloway) — hidden-condition testing, four server fixes, Python 3.8

- **Tool:** Claude Code (Claude Opus 5.5).
- **Used for:**
  - `hidden_sim.py`: 25 scenarios beyond the public tests, built on the unmodified harness, with a test-only `custom_source.py`, flapping clients and multi-process load.
  - Diagnosing the failures it found.
  - Four server fixes: history cap + HISTORY byte cap, lag memory across reconnects, slow-reader reclassification on slow links, TCP segment cap.
  - Python 3.8 support.
  - The handoff notes, and plain-language explanations of the design for the report.
- **Useful suggestions:**
  1. **Test the hidden variables, not just the public tests.** Running the unmodified public client under conditions the public tests don't cover found a real failure. On the public 2 Mbit/s profile, a large HISTORY reply leaves loopback as one TCP segment bigger than the shaper's 4000-byte burst, so it is dropped on every retransmit and the connection stalls for good. All 60 late joiners in that scenario received no HISTORY and no captions (80% finals). A direct probe confirmed it (a 20.8 KB HISTORY never arrives) and the fix: capping TCP segments at 1400 B makes it arrive in 0.04 s, with no cost on the bandwidth test (3+3 runs).
  2. **A "finals due" metric** that excuses only finals sent in the last 3 s before the clients stop. It separates end-of-run artefacts from real losses, and the per-client breakdowns behind it showed which failures were real (`slow700`, `bw2m_slow`) and which weren't.
- **Suggestions that were wrong or changed:**
  1. **Batching finals into a mid-stream HISTORY message** for lagging clients, so slow readers could take in many finals per read. The teaching team's 2 Nov clarification ruled out unsolicited HISTORY, so it was dropped.
  2. **Raising the history cap from 2000 to 6000** on its own would have let a 60 s HISTORY exceed the 1 MiB message limit of websockets clients, which drop the connection. Added a 768 KiB cap per reply that keeps the newest finals.
  3. **Python 3.8 support as a single `websockets>=13.1,<17` line.** pip does not upgrade a package that already satisfies a requirement, so a shared grader environment with websockets 13–15 would keep it, and test clients there would get the older client library, under which our slow-reader advantage disappears. Changed to per-version lines: 16.x on Python 3.10+, the old range only on 3.8/3.9. Verified that 15.0.1 gets upgraded to 16.1.1.
  4. **Two "regressions" that weren't.** It first treated finals lost in `bw2m_lifted` and on the 2 Mbit/s test as server problems. A per-client breakdown showed every client missing only the last 2–4 segments, because the source's own handshake had crossed the saturated link and started 3.4 s late: an artefact of fixed-length runs, seen in both configurations.
  5. **The first A/B of the TCP segment cap** used a late join at 55 s, whose HISTORY happened to compress under 4000 B, so even the uncapped server passed and the A/B proved nothing. Replaced with a deterministic probe using incompressible text. The patch that disabled the cap for that A/B had also silently matched nothing (CRLF line endings in the copied files); caught by counting the patched lines before the run.
  6. **The tests used `asyncio.timeout`** (Python 3.11+) and failed on Ubuntu 22.04's Python 3.10. Replaced with `asyncio.wait_for`.
- **What I checked myself:**
  1. I ensured architecture, revision rules and recovery were written as expected, and reviewed code pushed to the team.
  2. I questioned all details regarding bounded states, to ensure that they were up to par and met requirement 8 of the updated spec.
  3. I questioned the AI's design choices against the spec and the teaching team's 2 Nov clarifications rather than accepting them.
  4. I made the environment decisions myself:
     - kept my VM untouched until I chose to allow it;
     - required a full Ubuntu environment for reported numbers;
     - accepted WSL2 only for development numbers once the timer check showed VirtualBox on Hyper-V was invalid.
  5. I reviewed what each push contained (files and commits) before allowing it, and asked whether each Python 3.8 change could affect performance on the grader's likely Python 3.10+ setup before keeping it.

## Tools used

- **Claude Code** (Anthropic), used by all four members:
  - **Claude Opus 5.5:** main model for every member.
  - **Sonnet sub-agents:** implementation steps on Day 2.
  - **Fable 5.1, with reviewer and skeptic sub-agents:** Day 3, to check each change before it was committed.
- **What it was used for:** explaining the spec, the harness and our own code; designing and implementing the server, the overload policy, the browser client and our test tools; running and analysing experiments; drafting handoff notes and documentation. Every commit was reviewed, explained back and pushed by the team member named in `CONTRIBUTIONS.md`.

## One useful suggestion

**Using the WebSocket pong delay as the lag signal** (Day 2, muhdbhz).

- **The problem:** `ws.send()` returns as soon as bytes reach the socket buffers, so a slow reader's backlog never shows in the server's own queue. Day 1 measured `max_pending = 0` throughout T3 while slow clients fell 15 s behind and lost about half their finals.
- **The suggestion:** the websockets client stops parsing incoming frames, pings included, once 16 messages are unread. So a pong that comes back late is direct evidence that this client has not processed what we sent, at no protocol cost: ping/pong are standard WebSocket control frames.
- **How we verified it:**
  - **T3:** final completeness 95.1% → 100%, slow-reader P95 28 s → 9.6 s, with normal clients unaffected (reference VM, median of 3).
  - **Harder variants:** every slow-reader variant in `hidden_sim.py` (30% slow readers, 8 revisions per segment, a 3× faster source, 1500 clients) stayed at 100% where a plain queue lost 4–14% of finals.
  - **Refined later:** Day 4 added a ping budget, and the kernel's TCP RTT to tell slow links from slow readers. The core idea is unchanged.

## One suggestion that was wrong, inefficient, or required modification

**Suggestion:** to get Ubuntu numbers on a Windows laptop, the coding assistant (Claude Code) suggested running the public tests inside a VirtualBox Ubuntu VM. Our team plan, also drafted with its help, suggested `multipass launch ...` as the default. It set the VM up, ran the quick gate and the tc/netem profiles there, and reported the results.

**Problem:** the results were wrong, and not because of our server. Even plain T1 with no impairment scored 68.8% final completeness, and the **unmodified starter server scored 56.3%**, versus 100% for both on Windows. The cause: the laptop runs Windows 11 Home with Hyper-V active (WSL2 + Core isolation), so VirtualBox couldn't use VT-x and fell back to "NEM" mode on top of Hyper-V (`VBox.log`: `fall back to NEM: VT-x is not available`). In that mode the VM's clock is correct, but **asyncio timers fire 134–240% late**. The caption source, load clients and server are all paced by asyncio timers, so the source ran about 2× slow and the clients' fixed test window closed before the last finals were sent. Multipass on Windows Home also runs through VirtualBox, so it would have had the same problem. Had we trusted those numbers, we could have spent a day chasing a non-existent server bug, or put invalid results in the report.

**How we caught it:** comparing against the naive starter server (if *it* also "loses" finals, the environment is suspect), then measuring timers directly: `10 × asyncio.sleep(0.1)` took up to 3.4 s instead of 1.0 s.

**Correction:**
- Ran the Ubuntu development tests in **WSL2 Ubuntu 24.04** instead. It runs directly on Hyper-V, has accurate timers (+0% drift) and real `sch_netem`/`sch_tbf` kernel modules. We label these numbers "WSL2, dev only", because the spec says WSL isn't the reference environment.
- Added an **automatic timer-drift check to `check.py`**: it measures asyncio sleep overrun before every run and warns above 10%.
- Team decision: the report's final tables all come from one reference Ubuntu VM with valid timers (Frankfurters' Ubuntu 26.04 VM, timer drift +0–1%), never from a machine that fails the drift check.

**Lesson:** a benchmark harness measures the machine as much as the code. Validate the environment (timer accuracy, a known-good baseline) before trusting or debugging against the numbers.

## Responsibility statement

We understand that the team remains responsible for the submitted code, experimental results, and claims, and that every member may be asked to explain or modify relevant code during the viva.
