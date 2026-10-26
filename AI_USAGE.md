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
- **What I checked myself:** TODO (lihloway: fill in what you read/verified so you can defend it in the viva).

## Tools used

TODO (final): list all tools across the team.

## One useful suggestion

TODO (final).

## One suggestion that was wrong, inefficient, or required modification

TODO (final).

## Responsibility statement

We understand that the team remains responsible for the submitted code, experimental results, and claims, and that every member may be asked to explain or modify relevant code during the viva.
