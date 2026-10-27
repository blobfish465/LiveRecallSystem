# CLAUDE.md — LiveRecall (CS3103 Assignment 4)

Shared context for every teammate's Claude session. Read this first, then `HANDOFF.md` (latest state of the work).

## Project in one paragraph

`caption_source.py` → **`solution_server.py`** (ours) → many browser/load-test clients (`client/`, ours).
The source emits caption **segments**, each as provisional revisions followed by one final revision.
The server must fan this out to many clients **correctly** (no stale/duplicate finals), **freshly** (low Caption Age),
with **bounded** memory, while clients are slow, disconnect, join late, or sit on impaired/low-bandwidth links.

- Deadline: **4 Nov 2026**. Demo + individual viva: **6 Nov 2026** (15-min slot, book on the SharePoint sheet).
- Spec: `miniProject_Assignment4_LiveRecall_System.pdf` (course site). Starter docs: `START_HERE.md`, `PROTOCOL_EXAMPLES.md`, `NETWORKING_PRIMER.md`, `PUBLIC_TESTS.md`.

## Hard constraints (do not break)

- Entry point stays `python solution_server.py --port <int>`; bind `0.0.0.0`; ready < 10 s; no interactive input.
- Runtime deps only in `requirements.txt` (currently `websockets`). Dev/test deps go in `requirements-dev.txt`. No cloud APIs, external DBs, or bonus-ASR deps on the mandatory path.
- **Never modify instructor harness files** (reported results must use unmodified measurement logic):
  `caption_source.py`, `load_clients.py`, `run_demo.py`, `analyse_results.py`, `monitor_server.py`, `submission_check.py`, `network_profiles/`.
  Our own test tools go in `tests/` or `check.py`.
- Server must ignore unknown fields and `SOURCE_DONE`, and survive malformed JSON / clients dropping mid-send.
- Do not assume exactly 3 revisions per segment (public source does; hidden one may not).
- Boundedness is graded as correctness: no unbounded history, per-client queue, or memory growth.

## Protocol (field names are fixed)

- Source → server: `SOURCE_HELLO {session}`, then `CAPTION_UPDATE {session, segment, revision, source_ts, final, text}`, optional `SOURCE_DONE`.
- Client → server: `CLIENT_HELLO {client_id, session, last_segment, last_revision, history_seconds}`.
  `last_segment/last_revision` = client's latest **finalized** state (0/0 for a new client). `history_seconds` ≤ 60.
- Server → client: `HISTORY {items:[CAPTION_UPDATE...]}` first — finals only, oldest→newest, excluding anything the client already holds — then live `CAPTION_UPDATE`s.
- A final is durable: no older or equal revision may ever replace it. Clients must never display an older revision after a newer one.

## Design rules we agreed on

- **Source handler never awaits client sends.** It updates state and hands the update to each client's channel.
- **One `ClientChannel` per client** (queue + sender task) with interface `offer(update)`; this is where backpressure lives.
- **Snapshot history + register client for live updates with no `await` in between** (atomic in asyncio) — otherwise finals get duplicated or lost across HISTORY/live.
- **Server history**: deque of finals bounded by time (e.g. 90 s) **and** a max count.
- **Overload policy**: under real lag only, coalesce provisional revisions per segment (keep newest); **never drop finals**; send the newest revision with its own `source_ts`. When the client catches up, return to full-update delivery (Normal Update Coverage is graded).
- Watch for backlog hiding in the `websockets` write buffer / kernel socket buffer — a bounded app queue alone may not bound latency.
- Keep state/channel logic in plain classes that don't touch real sockets, so they are unit-testable.

## Relay plan (one person per day, then a team meet-up)

| Day | Owner | Work | Definition of done |
|---|---|---|---|
| 1 | lihloway | Repo hygiene, server state + bounded history, `HISTORY`/resume/late join, `ClientChannel` skeleton (bounded, never drops finals), `tests/` + `check.py` + `TESTING.md` stubs, naive-baseline numbers | `submission_check.py` passes; T1/T2/T4/late-join: 100% final completeness, 0 duplicates, 0 stale; `HANDOFF.md` written |
| 2 | TBD | **Backpressure**: lag detection, provisional coalescing, constrained-bandwidth policy, `--naive` flag for before/after, channel unit tests, T3 gate. Start with the "Key finding" in `HANDOFF.md` (backlog sits in socket buffers, not our queue) | T3 gated in `check.py`: normal clients fresh, slow clients 100% finals, memory flat; earlier gates still green |
| 3 | TBD | **Browser client** (in-place revisions, provisional/final styling, ≥60 s local history, recall 30 s default + 15/30/60, auto-reconnect with finalized resume state, HISTORY dedup, configurable WS URL via query string) + **test tooling** (slow-then-fast convergence client, `check.py --stress` with 300–500 clients, bandwidth-test script). Finish any leftover Day 2 work | Manual browser checklist ticked; new tools run; earlier gates green |
| 4 | TBD | Ubuntu VM + tc/netem; T1–T5 × 3 runs (median); bandwidth + stress + 5-min memory runs; before/after comparison; charts; README; report draft | Results + charts committed; README reproducible from a fresh clone |
| Meet-up | All | Integration fixes, demo video (5–7 min), `AI_USAGE.md`, `CONTRIBUTIONS.md`, final report, `submission_check.py --final`, viva walkthrough of each other's code | Submitted |

Day 2 and Day 4 (and Day 3 for bandwidth tests) need Ubuntu for `tc/netem` — WSL is not the reference environment. **Team decision: the reference environment is the CS3103 Mininet lab Ubuntu VM** (allowed by the spec); the report's final tables come from it. If you don't have it, a fresh Ubuntu VM (e.g. `multipass launch --name liverecall --cpus 2 --memory 4G --disk 15G`) is the next best option.

**Before trusting any numbers, check `check.py`'s `asyncio timer drift` line (must be < 10%).** VirtualBox/Multipass on a Windows PC with Hyper-V on (WSL2, Docker, Core isolation) runs in "NEM" mode with timers 100–200% late, which makes every metric meaningless (see "Test environments" in `HANDOFF.md`). WSL2 is accurate and fine for development; the report's final tables should come from a full Ubuntu VM with valid timers, or be labelled WSL2.

## Testing rules (every day)

1. **Add tests for your own work** — unit tests in `tests/` (pytest) and/or scenarios in `check.py`, or checklist items in `TESTING.md` for UI.
2. **Before handing off, run `pytest` and `python check.py`; both must pass.** Never hand off a red gate — if something is unfinished, say so in `HANDOFF.md`.
3. `check.py` reads the JSON summaries `run_demo.py` writes (`duplicates`, `stale`, `normal_update_coverage_pct`, `fresh_final_pct`, …). Check key names against a real run before relying on them.
4. Required thresholds for every gate scenario: final completeness 100%, duplicates 0, stale 0; coverage ≈100% when no clients are slow/disconnected.

Coverage checklist (spec gaps that public tests don't exercise — make sure each has an owner and a test):
- [ ] Constrained bandwidth (`network_profiles/bandwidth_2mbit.sh`)
- [ ] Scale beyond 100 clients
- [ ] Bursty / faster revisions and variable revision counts (1, 8, …)
- [ ] Rapid convergence after overload ends (slow-then-fast client)
- [ ] Long-run boundedness (5-min run with `monitor_server.py`)
- [ ] Robustness: unknown fields, `SOURCE_DONE`, malformed JSON, mid-send disconnect
- [ ] Browser: "What did I miss?" uses local state only; connection state visible; works when browser and server are on different hosts

## Handoff + documentation rules

- At the end of your day, update **`HANDOFF.md`**: what's done, how to run it, design decisions + why, interfaces the next person builds on, known issues, test results (numbers), and notes for your report section.
- Keep an **AI usage log** as you go in `AI_USAGE.md` (tool, what for, one useful suggestion, and at least one suggestion that was wrong/inefficient and how you changed it). The spec requires this; it's lost if not recorded daily.
- Commit often under your own name (git history supports `CONTRIBUTIONS.md` and the viva). Don't commit `venv/`, `__pycache__/`, or bulky raw CSVs.

## Instructions for Claude

- Explain non-obvious design decisions in code comments or `HANDOFF.md` — every teammate must be able to explain any line in the individual viva.
- Prefer small, reviewable changes; run `pytest` and `python check.py` after server changes.
- Don't change the protocol's required fields or the CLI. Don't edit instructor harness files.
- When a choice affects another day's work (e.g. the `ClientChannel` interface), note it in `HANDOFF.md`.

## Grading reminders

A 15 app/demo · B 15 protocol correctness · C 10 public experiments · **D 30 hidden performance** · E 10 repo/report · F 20 individual viva (+3 optional ASR bonus).
Hidden D is capped at 15/30 if final completeness < 99%, 5/30 if < 95%. The two 6-mark advanced challenges (slow-reader backpressure, constrained bandwidth) reward behavior clearly better than a plain queue.

Report must cover: architecture + protocol, revision/recovery semantics, backpressure/overload policy, public experiments (T1–T5, 3 runs, median), ≥1 before/after comparison, P50/P95/P99 + freshness + completeness, failures/trade-offs, limitations, future work/FYP direction.
