# CLAUDE.md — LiveRecall (CS3103 Assignment 4)

Shared context for every teammate's Claude session. Read this first, then `HANDOFF.md` (latest state of the work).

## Project in one paragraph

`caption_source.py` → **`solution_server.py`** (ours) → many browser/load-test clients (`client/`, ours).
The source emits caption **segments**, each as provisional revisions followed by one final revision.
The server must fan this out to many clients **correctly** (no stale/duplicate finals), **freshly** (low Caption Age),
with **bounded** memory, while clients are slow, disconnect, join late, or sit on impaired/low-bandwidth links.

- Deadline: **4 Nov 2026**. Demo + individual viva: **6 Nov 2026** (15-min slot, book on the SharePoint sheet).
- Spec: `miniProject_Assignment4_LiveRecall_System.pdf` (course site). Starter docs: `START_HERE.md`, `PROTOCOL_EXAMPLES.md`, `NETWORKING_PRIMER.md`, `PUBLIC_TESTS.md`.

## Code map

| Path | What |
|---|---|
| `solution_server.py` | Entry point + WebSocket wiring only (source handler, client handler, `[stats]` line every 10 s) |
| `liverecall/state.py` | `CaptionState`: revision rules, bounded history, `history_for()` → HISTORY selection. Pure logic, no I/O |
| `liverecall/channel.py` | `ClientChannel`: per-client bounded queue + sender task. **Backpressure/overload policy goes here** |
| `tests/` | pytest: `test_state.py`, `test_channel.py`, `test_server.py` (real sockets, in-process) |
| `check.py` | Regression gate: runs unmodified `run_demo.py` scenarios, fails on completeness < 100% / dup / stale; prints timer drift first; `--stress` = 300/500-client scenarios; every row also shows the server's RSS + `[stats]` peaks |
| `bandwidth_test.py` | Constrained-bandwidth experiment (needs `sudo tc`): before/after on a shaped loopback port, link + tbf + `[stats]` + RSS evidence |
| `convergence_test.py` | Slow-then-fast clients (instructor's client code, imported): how fast freshness and full delivery return after a reader stops being slow, before/after |
| `HANDOFF.md` / `TESTING.md` | Latest state + numbers / what's tested and how |
| `results/` | Harness output (gitignored) |

## Quick start

```bash
python3 -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest -q          # ~1 s
python check.py              # ~3 min; must print "GATE PASSED" and timer drift < 10%
```

`tc` needs root. In WSL2 use `wsl -d Ubuntu -u root`; in a VM use `sudo`. Always run `network_profiles/reset.sh lo` after an impairment run.

## Hard constraints (do not break)

- Entry point stays `python solution_server.py --port <int>`; bind `0.0.0.0`; ready < 10 s; no interactive input.
- Runtime deps only in `requirements.txt` (currently `websockets`). Dev/test deps go in `requirements-dev.txt`. No cloud APIs, external DBs, or bonus-ASR deps on the mandatory path.
- **Never modify instructor harness files** (reported results must use unmodified measurement logic):
  `caption_source.py`, `load_clients.py`, `run_demo.py`, `analyse_results.py`, `monitor_server.py`, `submission_check.py`, `network_profiles/`.
  Our own test tools go in `tests/` or `check.py`.
- Server must ignore unknown fields and `SOURCE_DONE`, and survive malformed JSON / clients dropping mid-send.
- Do not assume exactly 3 revisions per segment (public source does; hidden one may not).
- Boundedness is graded as correctness: no unbounded history, per-client queue, or memory growth.
- **The server path (`solution_server.py`, `liverecall/`) must run on Python 3.8 + websockets 13.1** (Ubuntu 20.04; the instructor's starter allows `websockets>=13`). `requirements.txt` uses per-version lines: **Python 3.10+ requires websockets 16.x** (pip also upgrades an older one already installed, so shared test clients get the library our lag policy was measured on), and 3.8/3.9 get `>=13.1,<16` (13.1 on 3.8).
  - Don't use: `@dataclass(slots=True)`, `X | Y` in runtime expressions (annotations are fine with `from __future__ import annotations`), `match`, `dict | dict`, `str.removeprefix`, `asyncio.to_thread`/`timeout`/`TaskGroup`.
  - Don't create asyncio objects (Event, Lock, Queue) outside a running loop (3.8 binds them at construction).
  - Import the server as `from websockets.asyncio.server import serve` (on 13.1 `websockets.serve` is the legacy one).
  - Check before pushing: `uv python install 3.8`, create `venv38` from that interpreter, `pip install -r requirements.txt "pytest<8.4"`, run `venv38/Scripts/python -m pytest -q` (or `venv38/bin/python`). Tests and tools may use 3.10+, but currently all pass on 3.8 too.
- Shell scripts must stay LF (`.gitattributes` enforces it). If copying from an old Windows checkout, run `sed -i 's/\r$//' network_profiles/*.sh`.

## Protocol (field names are fixed)

- Source → server: `SOURCE_HELLO {session}`, then `CAPTION_UPDATE {session, segment, revision, source_ts, final, text}`, optional `SOURCE_DONE`.
- Client → server: `CLIENT_HELLO {client_id, session, last_segment, last_revision, history_seconds}`.
  `last_segment/last_revision` = client's latest **finalized** state (0/0 for a new client). `history_seconds` ≤ 60.
- Server → client: `HISTORY {items:[CAPTION_UPDATE...]}` first — finals only, oldest→newest, excluding anything the client already holds — then live `CAPTION_UPDATE`s.
- A final is durable: no older or equal revision may ever replace it. Clients must never display an older revision after a newer one.

## Teaching-team clarifications (Prof. Anand, 2 Nov 2026; also on Canvas)

Answers to our questions about the hidden tests. Exact parameters stay hidden; this is the intended behaviour. **Treat these as part of the spec.**

- **Slow readers:** no client will be made *permanently* too slow to keep up with the **final-caption stream alone**. The challenge is clients that can't keep up with the full stream of *provisional* revisions, while finals and the latest useful state are preserved.
- **Final completeness:** measured after a **short, bounded** grace period for in-flight/recovery traffic. Don't rely on a long grace period or on an unbounded queue that only drains after the source stops.
- **Unsolicited HISTORY: not allowed.** `HISTORY` is only for connect / reconnect / late-join recovery after `CLIENT_HELLO`. Live delivery uses `CAPTION_UPDATE` only. (So no "batch finals into a HISTORY message" for lagging clients.)
- **Constrained bandwidth:** never set below what **final captions alone** need. Forwarding every provisional promptly is infeasible, so degrade gracefully: suppress obsolete provisionals, keep finals and the latest useful state.
- **Bounded history:** retain enough finals for **60 s** of recovery/late join (more is allowed). History *and any per-client pending state* need a **fixed upper bound** that doesn't grow with lecture length or client delay. A client disconnected or unable to keep up beyond the retained window isn't guaranteed older captions.
- **Graceful overload:** obsolete provisionals may be coalesced or discarded. Finals within the retained window must **not** be discarded to make room for provisionals, and the newest relevant revision of an active segment should be kept. No reliance on an unbounded per-client queue or a long backlog drained after the source stops.

How our design maps onto this:
- finals-only + coalescing while lagging (slow reader or slow link);
- per-client queue capped (64 entries, 10 s age);
- hopeless clients closed and recovered via HISTORY *after a new `CLIENT_HELLO`*, which is allowed;
- history bounded by 90 s **and** 6000 items, which holds 60 s up to ~100 segments/s.

Known residual risk: Day 4's end-of-run tail. A slow reader that retries full delivery in the last ~10–25 s may not drain before a *short* grace period ends; 99.87% in the 5-min run. See `HANDOFF.md`.

## Design rules we agreed on

- **Source handler never awaits client sends.** It updates state and hands the update to each client's channel.
- **One `ClientChannel` per client** (queue + sender task) with interface `offer(update)`; this is where backpressure lives.
- **Snapshot history + register client for live updates with no `await` in between** (atomic in asyncio) — otherwise finals get duplicated or lost across HISTORY/live.
- **Server history**: deque of finals bounded by time (90 s) **and** a max count (6000, enough for 60 s at high caption rates). Each HISTORY reply is capped at 768 KiB (newest items kept) so it never exceeds websockets clients' 1 MiB message limit.
- **Overload policy**: under real lag only, coalesce provisional revisions per segment (keep newest); **never drop finals**; send the newest revision with its own `source_ts`. When the client catches up, return to full-update delivery (Normal Update Coverage is graded).
- **Lag memory + reader reclassification (2 Nov, `liverecall/channel.py`):**
  - A lagging client that disconnects resumes lag mode on reconnect (bounded `LagMemory` by client_id, kept 120 s) instead of being flooded with the full stream again.
  - In slow-link lag, a pong pending > max(6 s, 4 × RTT) reclassifies the client as a slow reader (longer recovery wait).
  - Found with `hidden_sim.py` (slow700, bw2m_slow); see `HANDOFF.md` "Update 2 Nov (2)".
- **TCP segment cap (2 Nov):** the server's listening socket sets `TCP_MAXSEG = 1400`. On loopback (MTU 64 KiB) the public 2 Mbit/s tbf (burst 4000 B) drops any larger segment for good, so a big HISTORY reply stalled its connection. Keep the cap; see `HANDOFF.md` "Update 2 Nov (3)".
- **Confirmed on Day 1:** for slow readers the backlog sits in the `websockets` receive queue and TCP buffers, *not* in our queue (`max_pending` stays 0 during T3). A bounded app queue alone can't fix slow-client freshness; we need a lag signal plus a cap on in-flight messages. See "Key finding" in `HANDOFF.md`.
- Keep state/channel logic in plain classes that don't touch real sockets, so they are unit-testable.

## Relay plan (one person per day, then a team meet-up)

| Day | Owner | Work | Definition of done |
|---|---|---|---|
| 1 ✅ | lihloway | **DONE.** Repo hygiene, server state + bounded history, `HISTORY`/resume/late join, `ClientChannel` skeleton (bounded, never drops finals), `tests/` + `check.py` + `TESTING.md` stubs, naive-baseline numbers | `submission_check.py` passes; T1/T2/T4/late-join: 100% final completeness, 0 duplicates, 0 stale; `HANDOFF.md` written |
| 2 ✅ | muhdbhz | **DONE** (`--no-coalesce` is the before/after flag). **Backpressure**: lag detection, provisional coalescing, constrained-bandwidth policy, `--naive` flag for before/after, channel unit tests, T3 gate. Start with the "Key finding" in `HANDOFF.md` (backlog sits in socket buffers, not our queue) | T3 gated in `check.py`: normal clients fresh, slow clients 100% finals, memory flat; earlier gates still green |
| 3 ✅ | QiAn | **DONE** (lag-policy fixes for slow links and post-overload recovery handed to Day 4, see `HANDOFF.md`). **Browser client** (in-place revisions, provisional/final styling, ≥60 s local history, recall 30 s default + 15/30/60, auto-reconnect with finalized resume state, HISTORY dedup, configurable WS URL via query string) + **test tooling** (slow-then-fast convergence client, `check.py --stress` with 300–500 clients, bandwidth-test script). Finish any leftover Day 2 work | Manual browser checklist ticked; new tools run; earlier gates green |
| 4 ✅ | Frankfurters | **DONE** (part 1: lag-policy fixes; part 2: reference-VM runs + two lag-policy fixes, see `HANDOFF.md`). T1–T5 × 3 runs (median); bandwidth + stress + convergence + 5-min memory runs; before/after comparisons | All gates green on the reference VM; results in `HANDOFF.md` |
| 5 | Frankfurters | Charts, README (reproducible from a fresh clone), report draft from the Day 4 tables | Charts + README + report draft committed |
| Meet-up | All | Integration fixes, demo video (5–7 min), `AI_USAGE.md`, `CONTRIBUTIONS.md`, final report, `submission_check.py --final`, viva walkthrough of each other's code | Submitted |

Day 2 and Day 4 (and Day 3 for bandwidth tests) need Ubuntu for `tc/netem` — WSL is not the reference environment. **Team decision (2 Nov): the reference environment is Frankfurters' Ubuntu 26.04.1 LTS VM** (Apple Silicon, aarch64, 2 vCPU, 5 GB, Python 3.14, timer drift +0–1%). Every final report table comes from it, on the final code (Day 4 part 2 in `HANDOFF.md`; data in `report/data/`), labelled "Ubuntu 26.04 VM (2 vCPU), median of 3". It replaces the earlier choice of the Mininet lab VM, because all final experiments already ran there on one code version; re-running everything elsewhere would cost ~2.5 h of machine time for no gain. Caveat to state in the report: with 2 vCPUs the 500-client stress/bandwidth runs share the CPU with the load generator, so their latencies are upper bounds. Day 1–3 numbers (WSL2, Mininet VM) stay in `HANDOFF.md` as development history; don't mix them into report tables. Any re-run for the report must use this VM.

**Before trusting any numbers, check `check.py`'s `asyncio timer drift` line (must be < 10%).** VirtualBox/Multipass on a Windows PC with Hyper-V on (WSL2, Docker, Core isolation) runs in "NEM" mode with timers 100–200% late, which makes every metric meaningless (see "Test environments" in `HANDOFF.md`). WSL2 is accurate and fine for development; the report's final tables come from the reference VM above.

## Testing rules (every day)

1. **Add tests for your own work** — unit tests in `tests/` (pytest) and/or scenarios in `check.py`, or checklist items in `TESTING.md` for UI.
2. **Before handing off, run `pytest` and `python check.py`; both must pass.** Never hand off a red gate — if something is unfinished, say so in `HANDOFF.md`.
3. `check.py` reads the JSON summaries `run_demo.py` writes (`duplicates`, `stale`, `normal_update_coverage_pct`, `fresh_final_pct`, …). Check key names against a real run before relying on them.
4. Required thresholds for every gate scenario: final completeness 100%, duplicates 0, stale 0; coverage ≈100% when no clients are slow/disconnected.

Coverage checklist (spec gaps that public tests don't exercise — make sure each has an owner and a test):
- [x] Constrained bandwidth: `bandwidth_test.py` (Day 3). Day 4 policy (ping budget, no stop-and-wait, RTT-classified lag, small send buffers) beats plain FIFO at 500 clients on 2 Mbit/s: 100% vs 98.7% finals, P95 1.14 vs 1.56 s, 60% vs 0% fresh; coverage 51% vs 99% is the trade-off (`HANDOFF.md`, Day 4). Reference VM, final code (Day 4 part 2): 100% vs 98.1% finals, P95 1.30 vs 1.59 s, 50% vs 0% fresh finals, coverage 58% vs 98%
- [x] Scale beyond 100 clients: `check.py --stress` (300 / 500 / 500 + 10% slow; Day 3)
- [~] Bursty / faster revisions and variable revision counts: 1/2/3/8 revisions unit-tested (Day 1); bursty source rate still untested
- [~] Rapid convergence after overload ends: `convergence_test.py` (Day 3). Day 4: slow-*link* lag retries full delivery after ~10 s (jittered); slow-*reader* lag deliberately keeps 60–90 s (jitter upward only; ~66 s after the reader speeds up), because earlier retries cost T3 finals (98.3–99.96%). Reasoning in `HANDOFF.md`, Day 4 parts 1 and 2
- [x] Long-run boundedness: 1-hour simulation unit-tested for server history (Day 1); 5-min real run with `monitor_server.py` (Day 4 part 2): RSS 36.9 → 37.1 MB, history plateaus at 120 finals; 99.87% finals (slow readers' tail, FIFO 98.39%)
- [x] Network impairment T5 (Day 4 part 2): all 4 netem profiles x3, 100% finals; slow-link lag needs confirmation so public `delay_loss` keeps 99.93% coverage
- [x] Robustness: unknown fields, `SOURCE_DONE`, malformed JSON, bad hello, abrupt client drop (`tests/test_server.py`, Day 1)
- [ ] Browser: "What did I miss?" uses local state only; connection state visible; works when browser and server are on different hosts. Day 3

## Handoff + documentation rules

- At the end of your day, add your section to **`HANDOFF.md`**: what's done, how to run it, design decisions + why, interfaces the next person builds on, known issues, notes for your report section, and a **"Numbers today (median of 3)"** block from the public tests (`run_demo.py ... --runs 3`, copy the medians). Use this format:

  ```
  ## Day N — <name>
  Done: ...
  Not done / known bugs: ...
  Decisions made (and why): ...
  Numbers today (median of 3), environment + timer drift:
  - T1 (20 clients):   dup 0, stale 0, completeness 100%, coverage 100%
  - T2 (100 clients):  P50 .. ms, P95 .. ms, P99 .. ms, completeness ..%
  - T3 (10% slow):     normal P95 .. ms, slow P95 .. ms, completeness ..%
  - T4 (20% drop 5s):  dup 0, stale 0, completeness ..%, recovery P50 .. ms
  Next person should start with: ...
  Claude suggestion I rejected (if applicable): ...   <- also goes in AI_USAGE.md
  ```
- Keep an **AI usage log** as you go in `AI_USAGE.md` (tool, what for, one useful suggestion, and at least one suggestion that was wrong/inefficient and how you changed it). The spec requires this; it's lost if not recorded daily.
- Commit often under your own name (git history supports `CONTRIBUTIONS.md` and the viva). Don't commit `venv/`, `__pycache__/`, or bulky raw CSVs.

## Instructions for Claude

- Explain non-obvious design decisions in code comments or `HANDOFF.md` — every teammate must be able to explain any line in the individual viva.
- Prefer small, reviewable changes; run `pytest` and `python check.py` after server changes.
- Don't change the protocol's required fields or the CLI. Don't edit instructor harness files.
- When a choice affects another day's work (e.g. the `ClientChannel` interface), note it in `HANDOFF.md`.
- If results look surprisingly bad, first check the environment: timer drift in `check.py`'s first line, and whether the naive starter (`git show e131719:solution_server.py`) scores the same. Don't debug the server against numbers from a broken environment.
- Report numbers honestly: say which environment (reference VM / WSL2 / Mininet VM / Windows) and whether it's 1 run or the median of 3. Report tables use the reference VM only.

## Grading reminders

A 15 app/demo · B 15 protocol correctness · C 10 public experiments · **D 30 hidden performance** · E 10 repo/report · F 20 individual viva (+3 optional ASR bonus).
Hidden D is capped at 15/30 if final completeness < 99%, 5/30 if < 95%. The two 6-mark advanced challenges (slow-reader backpressure, constrained bandwidth) reward behavior clearly better than a plain queue.

Report must cover: architecture + protocol, revision/recovery semantics, backpressure/overload policy, public experiments (T1–T5, 3 runs, median), ≥1 before/after comparison, P50/P95/P99 + freshness + completeness, failures/trade-offs, limitations, future work/FYP direction.
