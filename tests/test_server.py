"""End-to-end tests against the real handler over real WebSockets (in-process)."""

import asyncio
import json
import random

import pytest
import websockets

import solution_server
from liverecall.state import CaptionState


@pytest.fixture(autouse=True)
def fresh_server_state(monkeypatch):
    monkeypatch.setattr(solution_server, "STATE", CaptionState())
    monkeypatch.setattr(solution_server, "CLOSED_TOTALS",
                        {"coalesced": 0, "lag_entries": 0, "hopeless_closes": 0})
    solution_server.CHANNELS.clear()
    yield
    solution_server.CHANNELS.clear()


async def start_server():
    server = await websockets.serve(solution_server.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


def upd(seg, rev, final):
    return json.dumps({
        "type": "CAPTION_UPDATE", "session": "L1", "segment": seg, "revision": rev,
        "source_ts": 1.0, "final": final, "text": f"s{seg}r{rev}",
    })


async def send_segments(src, first, last, revisions=3, gap=0.0):
    for seg in range(first, last + 1):
        for rev in range(1, revisions + 1):
            await src.send(upd(seg, rev, rev == revisions))
            if gap:
                await asyncio.sleep(gap)


async def hello(ws, last_seg=0, last_rev=0, history_seconds=60):
    await ws.send(json.dumps({
        "type": "CLIENT_HELLO", "client_id": 1, "session": "L1",
        "last_segment": last_seg, "last_revision": last_rev,
        "history_seconds": history_seconds,
    }))


async def collect(ws, until_final, timeout=5.0):
    """Receive messages until the final of segment `until_final` arrives."""
    msgs = []

    async def loop():
        while True:
            msg = json.loads(await ws.recv())
            msgs.append(msg)
            items = msg["items"] if msg["type"] == "HISTORY" else [msg]
            if any(i["segment"] == until_final and i["final"] for i in items):
                return msgs

    # wait_for rather than asyncio.timeout: the latter needs Python 3.11 and
    # Ubuntu 22.04 ships 3.10.
    return await asyncio.wait_for(loop(), timeout)


def finals_seen(msgs):
    out = []
    for m in msgs:
        for i in (m["items"] if m["type"] == "HISTORY" else [m]):
            if i["final"]:
                out.append(i["segment"])
    return out


async def wait_for_state(predicate, timeout=2.0):
    async def loop():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(loop(), timeout)


def test_late_join_gets_history_then_live():
    async def main():
        server, uri = await start_server()
        async with websockets.connect(uri) as src:
            await src.send(json.dumps({"type": "SOURCE_HELLO", "session": "L1"}))
            await send_segments(src, 1, 5)
            await wait_for_state(lambda: solution_server.STATE.history_len == 5)
            async with websockets.connect(uri) as cl:
                await hello(cl)
                first = json.loads(await cl.recv())
                assert first["type"] == "HISTORY"
                assert [i["segment"] for i in first["items"]] == [1, 2, 3, 4, 5]
                await send_segments(src, 6, 7)
                rest = await collect(cl, 7)
                assert finals_seen([first] + rest) == [1, 2, 3, 4, 5, 6, 7]
                # Live updates carry every revision when the client keeps up.
                assert [(m["segment"], m["revision"]) for m in rest] == [
                    (6, 1), (6, 2), (6, 3), (7, 1), (7, 2), (7, 3)]
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def test_resume_only_sends_missed_finals():
    async def main():
        server, uri = await start_server()
        async with websockets.connect(uri) as src:
            await src.send(json.dumps({"type": "SOURCE_HELLO", "session": "L1"}))
            await send_segments(src, 1, 10)
            await wait_for_state(lambda: solution_server.STATE.history_len == 10)
            async with websockets.connect(uri) as cl:
                await hello(cl, last_seg=7, last_rev=3, history_seconds=30)
                first = json.loads(await cl.recv())
                assert [i["segment"] for i in first["items"]] == [8, 9, 10]
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def test_joins_during_burst_see_every_final_exactly_once():
    """Atomic snapshot+register: no final lost or duplicated across HISTORY/live."""
    async def main():
        server, uri = await start_server()
        last = 150
        async with websockets.connect(uri) as src:
            await src.send(json.dumps({"type": "SOURCE_HELLO", "session": "L1"}))

            async def client(delay):
                await asyncio.sleep(delay)
                async with websockets.connect(uri) as cl:
                    await hello(cl, history_seconds=60)
                    return await collect(cl, last, timeout=10)

            rng = random.Random(7)
            clients = [asyncio.create_task(client(rng.uniform(0, 0.4))) for _ in range(30)]
            await send_segments(src, 1, last, gap=0.001)
            results = await asyncio.gather(*clients)

        for msgs in results:
            seen = finals_seen(msgs)
            assert len(seen) == len(set(seen)), "duplicate final"
            assert seen == list(range(1, last + 1)), "missing or out-of-order final"
            # No older revision after a newer one of the same segment.
            best = {}
            for m in msgs:
                if m["type"] == "CAPTION_UPDATE":
                    assert m["revision"] > best.get(m["segment"], 0)
                    best[m["segment"]] = m["revision"]
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def test_source_garbage_is_ignored():
    async def main():
        server, uri = await start_server()
        async with websockets.connect(uri) as cl:
            await hello(cl)
            assert json.loads(await cl.recv())["items"] == []
            async with websockets.connect(uri) as src:
                await src.send(json.dumps({"type": "SOURCE_HELLO", "session": "L1"}))
                for junk in ["not json", "[1,2]", "null", '{"type":"WHAT"}',
                             '{"type":"CAPTION_UPDATE","segment":"x"}',
                             '{"type":"SOURCE_DONE","session":"L1"}']:
                    await src.send(junk)
                await src.send(upd(1, 1, True))
                await src.send(upd(1, 1, True))  # duplicate final: not forwarded
                await src.send(upd(2, 1, True))
                msgs = await collect(cl, 2)
                assert [(m["segment"], m["revision"]) for m in msgs] == [(1, 1), (2, 1)]
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def test_bad_hello_is_rejected_and_server_survives():
    async def main():
        server, uri = await start_server()
        async with websockets.connect(uri) as bad:
            await bad.send("garbage")
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc:
                await bad.recv()
            rcvd = exc.value.rcvd
            assert rcvd is not None and rcvd.code == 1008
        async with websockets.connect(uri) as cl:
            await hello(cl)
            assert json.loads(await cl.recv())["type"] == "HISTORY"
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def test_client_dropping_mid_stream_does_not_affect_others():
    async def main():
        server, uri = await start_server()
        async with websockets.connect(uri) as src, websockets.connect(uri) as good:
            await src.send(json.dumps({"type": "SOURCE_HELLO", "session": "L1"}))
            await hello(good)
            await good.recv()
            flaky = await websockets.connect(uri)
            await hello(flaky)
            await flaky.recv()
            await send_segments(src, 1, 3)
            flaky.transport.abort()  # abrupt drop, no close handshake
            await send_segments(src, 4, 6)
            msgs = await collect(good, 6)
            assert finals_seen(msgs) == [1, 2, 3, 4, 5, 6]
            await wait_for_state(lambda: len(solution_server.CHANNELS) == 1)
        server.close()
        await server.wait_closed()

    asyncio.run(main())


@pytest.mark.parametrize("no_coalesce", [False, True])
def test_no_coalesce_selects_plain_fifo_channel(monkeypatch, no_coalesce):
    monkeypatch.setattr(solution_server, "NO_COALESCE", no_coalesce)

    async def main():
        server, uri = await start_server()
        async with websockets.connect(uri) as cl:
            await hello(cl)
            await wait_for_state(lambda: len(solution_server.CHANNELS) == 1)
            (channel,) = solution_server.CHANNELS
            # Without a ping the channel can never detect lag, so never coalesces.
            assert (channel._ping is None) == no_coalesce
            assert (channel.hopeless_s == float("inf")) == no_coalesce
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def test_stats_totals_survive_disconnect():
    async def main():
        server, uri = await start_server()
        async with websockets.connect(uri) as cl:
            await hello(cl)
            await wait_for_state(lambda: len(solution_server.CHANNELS) == 1)
            (channel,) = solution_server.CHANNELS
            channel.coalesced, channel.lag_entries = 5, 2
            assert "coalesced_total=5 lag_entries_total=2" in solution_server.stats_line()
        await wait_for_state(lambda: solution_server.CLOSED_TOTALS["coalesced"] == 5)
        line = solution_server.stats_line()
        assert "clients=0 " in line
        assert "coalesced_total=5 lag_entries_total=2 hopeless_closes_total=0" in line
        server.close()
        await server.wait_closed()

    asyncio.run(main())
