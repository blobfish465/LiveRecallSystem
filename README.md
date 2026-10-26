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

TODO (Day 4): exact commands for T1–T5 with `--runs 3`.

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
