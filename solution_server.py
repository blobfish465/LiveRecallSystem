#!/usr/bin/env python3
"""
CS3103 Assignment 4 - LiveRecall server.

Required evaluator command:
    python solution_server.py --port 8765

Architecture (see HANDOFF.md for the full write-up):

  caption_source --SOURCE_HELLO/CAPTION_UPDATE--> handle_source
        |  STATE.apply(): reject stale/duplicate/after-final, store, bound history
        v
  for every connected client: ClientChannel.offer()   (never awaits)
        |
        v  one sender task per client drains its own bounded queue
  student client <--HISTORY, then live CAPTION_UPDATE--

On CLIENT_HELLO, building the HISTORY snapshot and registering the client's
channel happen with no `await` in between. asyncio only switches tasks at an
await, so no CAPTION_UPDATE can slip between the snapshot and the
registration: each final reaches the client exactly once (in HISTORY or live).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any

import websockets

from liverecall.channel import ClientChannel
from liverecall.state import CaptionState, history_message

STATE = CaptionState()
CHANNELS: set[ClientChannel] = set()
# Strong references to fire-and-forget tasks so they are not garbage-collected.
_BACKGROUND: set[asyncio.Task] = set()

STATS_INTERVAL_S = 10.0
# Baseline for before/after comparisons: plain FIFO per client, no lag
# detection, coalescing or hopeless cap. Set by --no-coalesce, or by
# LIVERECALL_NO_COALESCE=1 because run_demo.py starts us with --port only.
NO_COALESCE = False


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def broadcast(caption) -> None:
    for channel in list(CHANNELS):
        channel.offer(caption)


async def handle_source(ws, hello: dict[str, Any]) -> None:
    print(f"source connected: session={hello.get('session')}", flush=True)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue

            msg_type = msg.get("type")
            if msg_type == "CAPTION_UPDATE":
                caption = STATE.apply(msg)
                if caption is not None:
                    broadcast(caption)
            elif msg_type == "SOURCE_DONE":
                # Test-control message; not forwarded to clients.
                print("source finished", flush=True)
            # Anything else is ignored for forward compatibility.
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        print("source disconnected", flush=True)


def _int_field(hello: dict[str, Any], key: str) -> int:
    value = hello.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


async def handle_client(ws, hello: dict[str, Any]) -> None:
    client_id = hello.get("client_id")
    last_segment = _int_field(hello, "last_segment")
    last_revision = _int_field(hello, "last_revision")

    def on_overflow(channel: ClientChannel) -> None:
        # Too far behind with only finals queued: drop the connection. The
        # client reconnects with its finalized state and recovers via HISTORY.
        CHANNELS.discard(channel)
        print(f"client overflow, closing: id={client_id}", flush=True)
        _spawn(ws.close(code=1013, reason="client too far behind; reconnect to resume"))

    if NO_COALESCE:
        # No ping means lag mode (and so coalescing) never turns on.
        channel = ClientChannel(ws.send, client_id=client_id, on_overflow=on_overflow,
                                hopeless_s=float("inf"))
    else:
        channel = ClientChannel(ws.send, client_id=client_id, on_overflow=on_overflow,
                                ping=ws.ping)

    # --- atomic section: no await between snapshot and registration ---------
    items = STATE.history_for(last_segment, last_revision, hello.get("history_seconds"))
    channel.offer_control(history_message(items))
    CHANNELS.add(channel)
    # ------------------------------------------------------------------------

    sender = asyncio.create_task(channel.run())
    print(
        f"client connected: id={client_id} resume=({last_segment},{last_revision}) "
        f"history_items={len(items)} clients={len(CHANNELS)}",
        flush=True,
    )
    try:
        # Clients don't need to send anything after CLIENT_HELLO; keep reading
        # so we notice the close.
        async for _ in ws:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        CHANNELS.discard(channel)
        channel.close()
        sender.cancel()
        print(f"client disconnected: id={client_id}; clients={len(CHANNELS)}", flush=True)


async def handler(ws) -> None:
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        hello = json.loads(raw)
        if not isinstance(hello, dict):
            raise ValueError("hello must be a JSON object")
    except (asyncio.TimeoutError, ValueError, TypeError):
        await ws.close(code=1008, reason="Expected a valid hello message")
        return
    except websockets.exceptions.ConnectionClosed:
        return

    msg_type = hello.get("type")
    if msg_type == "SOURCE_HELLO":
        await handle_source(ws, hello)
    elif msg_type == "CLIENT_HELLO":
        await handle_client(ws, hello)
    else:
        await ws.close(code=1008, reason="Expected SOURCE_HELLO or CLIENT_HELLO")


async def report_stats() -> None:
    """Periodic one-line summary; useful when watching memory/queue bounds."""
    while True:
        await asyncio.sleep(STATS_INTERVAL_S)
        pending = [c.pending for c in CHANNELS]
        print(
            f"[stats {time.strftime('%H:%M:%S')}] clients={len(CHANNELS)} "
            f"history={STATE.history_len} active={STATE.active_len} "
            f"max_pending={max(pending, default=0)} "
            f"coalesced={sum(c.coalesced for c in CHANNELS)}",
            flush=True,
        )


async def main_async(port: int) -> None:
    async with websockets.serve(
        handler,
        "0.0.0.0",
        port,
        max_size=2**20,
        # Default 32 KiB. Smaller makes send() block sooner on a congested link,
        # so the backlog stays in our outbox, where it can be coalesced, instead
        # of in the socket's write buffer, where it can't.
        write_limit=4096,
        ping_interval=20,
        ping_timeout=20,
    ):
        print(f"LiveRecall server listening on ws://0.0.0.0:{port}", flush=True)
        _spawn(report_stats())
        await asyncio.Future()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-coalesce", action="store_true",
                        default=os.environ.get("LIVERECALL_NO_COALESCE") == "1",
                        help="baseline: plain FIFO delivery (for before/after runs)")
    args = parser.parse_args()
    global NO_COALESCE
    NO_COALESCE = args.no_coalesce
    if NO_COALESCE:
        print("baseline mode: --no-coalesce (plain FIFO)", flush=True)
    try:
        asyncio.run(main_async(args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
