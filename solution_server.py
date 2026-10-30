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
import socket
import struct
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
# Per-client send buffers for the lag-aware server (not the baseline, so before/after
# runs keep Day 3's baseline). At 500 clients on a 2 Mbit/s link each client's share
# is ~500 B/s, so every KiB queued below our channel is ~2 s of caption age, and our
# pings wait behind it too (late pongs, needless lag). TCP_NOTSENT_LOWAT caps the
# bytes the kernel holds unsent; WRITE_HIGH is asyncio's write-buffer high-water
# mark (send() waits above it). Measured on the 500-client run (1 run each): with
# them 100% finals, P50 213 ms; without (kernel default, 4 KiB) 98.7%, P50 547 ms.
NOTSENT_LOWAT = 512
WRITE_HIGH = 512
# Counters of channels that have closed, so the [stats] totals survive disconnects.
CLOSED_TOTALS = {"coalesced": 0, "lag_entries": 0, "link_lag_entries": 0, "hopeless_closes": 0}


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def tighten_send_path(ws) -> None:
    """Small kernel + asyncio send buffers for this client (see NOTSENT_LOWAT)."""
    transport = ws.transport
    sock = transport.get_extra_info("socket")
    lowat = getattr(socket, "TCP_NOTSENT_LOWAT", None)  # Linux and macOS
    if sock is not None and lowat is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, lowat, NOTSENT_LOWAT)
        except OSError:
            pass  # e.g. not a TCP socket; flow control then works as before
    transport.set_write_buffer_limits(high=WRITE_HIGH)


_TCP_INFO = getattr(socket, "TCP_INFO", None)  # Linux only


def kernel_rtt(ws):
    """Callable giving the socket's smoothed TCP RTT in seconds, or None if unknown.

    The kernel measures it from ACKs, which the client's kernel sends whether or not
    the client app reads, so it tells a slow link (high RTT) from a slow reader
    (late pong, low RTT). No network cost: one getsockopt per lag decision.
    """
    sock = ws.transport.get_extra_info("socket")
    if sock is None or _TCP_INFO is None:
        return None

    def rtt() -> float | None:
        info = sock.getsockopt(socket.IPPROTO_TCP, _TCP_INFO, 104)
        if len(info) < 72:
            return None
        # struct tcp_info: 8 bytes of u8 fields, then u32s; tcpi_rtt (µs) is the 16th.
        return struct.unpack_from("I", info, 68)[0] / 1e6

    return rtt


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
        tighten_send_path(ws)
        channel = ClientChannel(ws.send, client_id=client_id, on_overflow=on_overflow,
                                ping=ws.ping, rtt=kernel_rtt(ws))

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
        CLOSED_TOTALS["coalesced"] += channel.coalesced
        CLOSED_TOTALS["lag_entries"] += channel.lag_entries
        CLOSED_TOTALS["link_lag_entries"] += channel.link_lag_entries
        CLOSED_TOTALS["hopeless_closes"] += channel.overflowed
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
        print(stats_line(), flush=True)


def stats_line() -> str:
    """Live values for connected clients; *_total also counts closed ones."""
    live = list(CHANNELS)
    coalesced = CLOSED_TOTALS["coalesced"] + sum(c.coalesced for c in live)
    lag_entries = CLOSED_TOTALS["lag_entries"] + sum(c.lag_entries for c in live)
    link_lag = CLOSED_TOTALS["link_lag_entries"] + sum(c.link_lag_entries for c in live)
    return (
        f"[stats {time.strftime('%H:%M:%S')}] clients={len(live)} "
        f"lagging={sum(c.lagging for c in live)} "
        f"history={STATE.history_len} active={STATE.active_len} "
        f"max_pending={max((c.pending for c in live), default=0)} "
        f"coalesced_total={coalesced} lag_entries_total={lag_entries} "
        f"link_lag_entries_total={link_lag} "
        f"hopeless_closes_total={CLOSED_TOTALS['hopeless_closes']}"
    )


async def main_async(port: int) -> None:
    async with websockets.serve(
        handler,
        "0.0.0.0",
        port,
        max_size=2**20,
        # Default 32 KiB. Day 2 lowered it so a congested link's backlog stays
        # in our outbox (Day 3: no measurable effect on its own). Kept for the
        # --no-coalesce baseline; lag-aware clients get tighten_send_path().
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
