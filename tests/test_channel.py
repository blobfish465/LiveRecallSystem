import asyncio
import json
import time

import pytest

import liverecall.channel as channel_mod
from liverecall.channel import ClientChannel
from liverecall.state import make_caption


def cap(seg, rev, final=False, recv_ts=None):
    c = make_caption({
        "segment": seg, "revision": rev, "final": final,
        "source_ts": 1.0, "text": f"s{seg}r{rev}",
    }, time.time() if recv_ts is None else recv_ts)
    assert c is not None  # test inputs are always valid
    return c


class Recorder:
    def __init__(self, delay=0.0, fail_after=None):
        self.sent = []
        self.delay = delay
        self.fail_after = fail_after

    async def __call__(self, wire):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionError("gone")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(json.loads(wire))


def keys(msgs):
    return [(m["segment"], m["revision"]) for m in msgs if m["type"] == "CAPTION_UPDATE"]


async def drain(ch, task):
    for _ in range(100):
        if ch.pending == 0:
            break
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    ch.close()
    await task


def test_delivers_control_first_then_updates_in_order():
    async def main():
        rec = Recorder()
        ch = ClientChannel(rec)
        ch.offer_control('{"type":"HISTORY","items":[]}')
        for c in [cap(1, 1), cap(1, 2), cap(1, 3, True)]:
            ch.offer(c)
        task = asyncio.create_task(ch.run())
        await drain(ch, task)
        assert rec.sent[0]["type"] == "HISTORY"
        assert keys(rec.sent) == [(1, 1), (1, 2), (1, 3)]

    asyncio.run(main())


def test_full_delivery_when_not_over_cap():
    ch = ClientChannel(Recorder(), max_pending=100)
    for seg in range(1, 21):
        for rev in (1, 2, 3):
            ch.offer(cap(seg, rev, rev == 3))
    assert ch.pending == 60 and ch.coalesced == 0


def test_over_cap_compacts_only_superseded_provisionals():
    ch = ClientChannel(Recorder(), max_pending=25)
    for seg in range(1, 21):
        for rev in (1, 2, 3):
            ch.offer(cap(seg, rev, rev == 3))
    caps = [o.caption for o in ch._queue if o.caption]
    assert len(caps) <= 25
    assert not ch.overflowed
    # Finals are never dropped while the channel is open.
    assert [c.segment for c in caps if c.final] == list(range(1, 21))
    # Only provisionals were dropped, and each had a newer item of its segment.
    assert ch.coalesced == 60 - len(caps)


def test_latest_provisional_of_active_segment_kept():
    ch = ClientChannel(Recorder(), max_pending=3)
    ch.offer(cap(1, 3, True))
    ch.offer(cap(2, 3, True))
    ch.offer(cap(3, 1))
    ch.offer(cap(3, 2))  # over cap -> compacts away (3,1), keeps newest (3,2)
    assert queued(ch) == [(1, 3), (2, 3), (3, 2)]


def test_overflow_when_backlog_is_all_finals():
    hits = []
    ch = ClientChannel(Recorder(), max_pending=5, on_overflow=hits.append)
    for seg in range(1, 7):
        ch.offer(cap(seg, 1, True))
    assert ch.overflowed and ch.closed and hits == [ch]
    assert ch.pending == 0
    ch.offer(cap(7, 1, True))  # ignored once closed
    assert ch.pending == 0


def test_age_cap_closes_hopeless_client():
    now = [1000.0]
    hits = []
    ch = ClientChannel(Recorder(), hopeless_s=30.0, clock=lambda: now[0], on_overflow=hits.append)
    ch.offer_control('{"type":"HISTORY","items":[]}')  # ignored by the age check
    ch.offer(cap(1, 1, True, recv_ts=1000.0))
    now[0] = 1030.0  # exactly at the limit: not over
    ch.offer(cap(2, 1, True, recv_ts=1030.0))
    assert not ch.closed and hits == []
    now[0] = 1030.5
    ch.offer(cap(3, 1, True, recv_ts=1030.5))
    assert ch.overflowed and ch.closed and hits == [ch]
    ch.offer(cap(4, 1, True, recv_ts=1031.0))
    assert hits == [ch]


def test_age_check_ignores_control_items():
    now = [5000.0]
    ch = ClientChannel(Recorder(), hopeless_s=30.0, clock=lambda: now[0])
    ch.offer_control('{"type":"HISTORY","items":[]}')
    ch.offer(cap(1, 1, True, recv_ts=5000.0))
    assert not ch.closed and ch.pending == 2


def test_send_failure_closes_channel():
    async def main():
        rec = Recorder(fail_after=1)
        ch = ClientChannel(rec)
        ch.offer(cap(1, 1))
        ch.offer(cap(1, 2))
        await asyncio.wait_for(ch.run(), timeout=1)
        assert ch.closed and len(rec.sent) == 1

    asyncio.run(main())


def test_slow_channel_does_not_block_offer():
    """offer() is synchronous: a stalled consumer never blocks the producer."""
    async def main():
        ch = ClientChannel(Recorder(delay=10), max_pending=50)
        task = asyncio.create_task(ch.run())
        await asyncio.sleep(0)
        for seg in range(1, 1001):
            for rev in (1, 2, 3):
                ch.offer(cap(seg, rev, rev == 3))
                if ch.closed:
                    break
        assert ch.pending <= 50
        ch.close()
        task.cancel()

    asyncio.run(main())


def queued(ch):
    return [(o.caption.segment, o.caption.revision) for o in ch._queue if o.caption]


def test_coalescing_collapses_same_segment_into_one_entry():
    ch = ClientChannel(Recorder())
    ch.coalescing = True
    for c in [cap(1, 1), cap(1, 2), cap(1, 3, True)]:
        ch.offer(c)
    assert queued(ch) == [(1, 3)]
    assert ch.coalesced == 2


def test_coalescing_preserves_queue_order():
    ch = ClientChannel(Recorder())
    ch.coalescing = True
    for c in [cap(1, 3, True), cap(2, 1), cap(2, 2), cap(3, 1)]:
        ch.offer(c)
    assert queued(ch) == [(1, 3), (2, 2), (3, 1)]


def test_coalescing_never_replaces_a_final():
    ch = ClientChannel(Recorder())
    ch.coalescing = True
    ch.offer(cap(1, 3, True))
    ch.offer(cap(1, 4))
    assert queued(ch) == [(1, 3), (1, 4)]
    first = ch._queue[0].caption
    assert first is not None and first.final
    assert ch.coalesced == 0


def test_coalescing_off_by_default_appends_everything():
    ch = ClientChannel(Recorder())
    assert ch.coalescing is False
    for c in [cap(1, 1), cap(1, 2), cap(1, 3, True)]:
        ch.offer(c)
    assert queued(ch) == [(1, 1), (1, 2), (1, 3)]
    assert ch.coalesced == 0


def test_sent_entry_is_not_coalesced_into():
    async def main():
        rec = Recorder()
        ch = ClientChannel(rec)
        task = asyncio.create_task(ch.run())
        ch.offer(cap(1, 1))
        for _ in range(10):
            await asyncio.sleep(0)
        assert keys(rec.sent) == [(1, 1)]
        ch.coalescing = True
        ch.offer(cap(1, 2))
        await drain(ch, task)
        assert keys(rec.sent) == [(1, 1), (1, 2)]
        assert ch.coalesced == 0

    asyncio.run(main())


def test_control_stays_first_with_coalescing():
    async def main():
        rec = Recorder()
        ch = ClientChannel(rec)
        ch.coalescing = True
        ch.offer_control('{"type":"HISTORY","items":[]}')
        for c in [cap(1, 1), cap(1, 2), cap(2, 1)]:
            ch.offer(c)
        task = asyncio.create_task(ch.run())
        await drain(ch, task)
        assert rec.sent[0]["type"] == "HISTORY"
        assert keys(rec.sent) == [(1, 2), (2, 1)]

    asyncio.run(main())


class FakePing:
    """Stands in for ws.ping: each call returns a future the test resolves by hand."""

    def __init__(self, auto=False):
        self.auto = auto
        self.futures = []

    async def __call__(self):
        fut = asyncio.get_running_loop().create_future()
        if self.auto:
            fut.set_result(None)
        self.futures.append(fut)
        return fut


async def settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


def test_fast_client_never_lags():
    async def main():
        rec, ping = Recorder(), FakePing(auto=True)
        ch = ClientChannel(rec, ping=ping, lag_s=0.05)
        task = asyncio.create_task(ch.run())
        want = []
        for seg in range(1, 4):
            for rev in (1, 2, 3):
                ch.offer(cap(seg, rev, rev == 3))
                want.append((seg, rev))
        await drain(ch, task)
        assert keys(rec.sent) == want
        assert not ch.lagging and ch.coalesced == 0 and ch.lag_entries == 0

    asyncio.run(main())


def test_stuck_pong_enters_lag_mode_and_holds_provisionals():
    async def main():
        rec, ping = Recorder(), FakePing()
        ch = ClientChannel(rec, ping=ping, lag_s=0.05)
        task = asyncio.create_task(ch.run())
        ch.offer(cap(1, 1))
        await settle()
        assert keys(rec.sent) == [(1, 1)] and len(ping.futures) == 1
        await asyncio.sleep(0.1)
        ch.offer(cap(2, 1))
        await settle()
        assert ch.lagging and ch.coalescing and ch.lag_entries == 1
        ch.offer(cap(2, 2))
        ch.offer(cap(2, 3))
        await settle()
        assert ch.coalesced >= 1
        assert keys(rec.sent) == [(1, 1)]  # nothing sent while the pong is outstanding
        assert queued(ch) == [(2, 3)]
        ch.close()
        await task

    asyncio.run(main())


def test_lag_mode_releases_one_final_per_pong():
    async def main():
        rec, ping = Recorder(), FakePing()
        ch = ClientChannel(rec, ping=ping, lag_s=0.05)
        task = asyncio.create_task(ch.run())
        ch.offer(cap(1, 1))
        await settle()
        await asyncio.sleep(0.1)
        ch.offer(cap(9, 1, True))  # wakes the sender, which notices the overdue pong
        await settle()
        assert ch.lagging
        for seg in (2, 3, 4):
            ch.offer(cap(seg, 1))
            ch.offer(cap(seg, 2))
            ch.offer(cap(seg, 3, True))
        await settle()
        expect = [(1, 1)]
        for nxt in [(9, 1), (2, 3), (3, 3), (4, 3)]:
            assert keys(rec.sent) == expect
            ping.futures[-1].set_result(None)
            await settle()
            expect = expect + [nxt]
        assert keys(rec.sent) == expect
        # No provisional of a segment that has a final was sent after lag began.
        assert all(m["final"] for m in rec.sent[1:])
        assert queued(ch) == []
        ch.close()
        await task

    asyncio.run(main())


def test_entering_lag_drops_provisionals_queued_before_it():
    # Queued before coalescing was on, so each revision got its own entry.
    ch = ClientChannel(Recorder())
    for c in [cap(2, 1), cap(2, 2), cap(2, 3, True), cap(3, 1), cap(3, 2)]:
        ch.offer(c)
    ch._compact()
    assert queued(ch) == [(2, 3), (3, 2)]
    assert ch.coalesced == 3


def test_lag_with_only_provisionals_blocks_and_close_is_prompt():
    async def main():
        rec, ping = Recorder(), FakePing()
        ch = ClientChannel(rec, ping=ping, lag_s=0.05)
        task = asyncio.create_task(ch.run())
        ch.offer(cap(1, 1))
        await settle()
        await asyncio.sleep(0.1)
        ch.offer(cap(2, 1))
        await settle()
        assert ch.lagging
        ping.futures[-1].set_result(None)  # pong arrives; only a provisional is queued
        await settle()
        count = len(rec.sent)
        await settle(200)
        assert len(rec.sent) == count == 1 and queued(ch) == [(2, 1)]
        ch.close()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(main())


def resolve_all(ping):
    for f in ping.futures:
        if not f.done():
            f.set_result(None)


async def until(cond, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "condition not reached"
        await asyncio.sleep(0.01)


async def enter_lag(ch, ping, seg):
    """Send (seg,1), let its pong go overdue, then wake the sender with (seg+1,1)."""
    ch.offer(cap(seg, 1))
    await settle()
    await asyncio.sleep(0.1)
    ch.offer(cap(seg + 1, 1))
    await settle()
    assert ch.lagging


async def recover(ch, ping):
    ping.auto = True
    resolve_all(ping)
    await until(lambda: not ch.lagging)


def test_recovers_to_full_delivery_after_pongs_turn_fast():
    async def main():
        rec, ping = Recorder(), FakePing()
        ch = ClientChannel(rec, ping=ping, lag_s=0.05, recover_s=0.1)
        task = asyncio.create_task(ch.run())
        await enter_lag(ch, ping, 1)
        assert ch.lag_exits == 0
        await recover(ch, ping)
        assert not ch.coalescing and ch.lag_exits == 1 and ch.lag_entries == 1
        for rev in (1, 2, 3):
            ch.offer(cap(3, rev, rev == 3))
        await until(lambda: ch.pending == 0)
        await settle()
        assert keys(rec.sent) == [(1, 1), (2, 1), (3, 1), (3, 2), (3, 3)]
        ch.close()
        await task

    asyncio.run(main())


def test_no_stale_revision_across_lag_entry_and_exit():
    async def main():
        rec, ping = Recorder(), FakePing()
        ch = ClientChannel(rec, ping=ping, lag_s=0.05, recover_s=0.1)
        task = asyncio.create_task(ch.run())
        ch.offer(cap(1, 1))
        await settle()
        await asyncio.sleep(0.1)
        # Queued before the sender notices the overdue pong (compacted on entry).
        for c in [cap(2, 1), cap(2, 2), cap(2, 3, True), cap(4, 1), cap(4, 2)]:
            ch.offer(c)
        await settle()
        assert ch.lagging
        for c in [cap(5, 1), cap(5, 2), cap(4, 3)]:  # coalesced in place
            ch.offer(c)
        await recover(ch, ping)
        for c in [cap(5, 3, True), cap(4, 4, True), cap(6, 1), cap(6, 2), cap(6, 3, True)]:
            ch.offer(c)
        await until(lambda: ch.pending == 0)
        await settle()
        newest: dict[int, int] = {}
        for seg, rev in keys(rec.sent):
            assert rev > newest.get(seg, 0), (seg, rev, keys(rec.sent))
            newest[seg] = rev
        assert newest[2] == 3 and newest[4] == 4 and newest[5] == 3 and newest[6] == 3
        ch.close()
        await task

    asyncio.run(main())


def test_relapse_doubles_recover_wait_up_to_cap(monkeypatch):
    monkeypatch.setattr(channel_mod, "RECOVER_MAX_S", 0.3)

    async def main():
        rec, ping = Recorder(), FakePing()
        ch = ClientChannel(rec, ping=ping, lag_s=0.05, recover_s=0.1)
        task = asyncio.create_task(ch.run())
        await enter_lag(ch, ping, 1)
        assert ch._recover_s == 0.1
        await recover(ch, ping)
        for seg, want in [(3, 0.2), (5, 0.3)]:
            ping.auto = False
            await enter_lag(ch, ping, seg)
            assert ch._recover_s == pytest.approx(want)
            await recover(ch, ping)
        assert ch.lag_exits == 3
        ch.close()
        await task

    asyncio.run(main())


def test_long_calm_period_resets_recover_wait(monkeypatch):
    monkeypatch.setattr(channel_mod, "REARM_S", 0.0)

    async def main():
        rec, ping = Recorder(), FakePing()
        ch = ClientChannel(rec, ping=ping, lag_s=0.05, recover_s=0.1)
        task = asyncio.create_task(ch.run())
        await enter_lag(ch, ping, 1)
        await recover(ch, ping)
        ping.auto = False
        await enter_lag(ch, ping, 3)
        assert ch._recover_s == 0.1
        ch.close()
        await task

    asyncio.run(main())


def test_late_pong_in_lag_resets_calm_timer():
    async def main():
        rec, ping = Recorder(), FakePing()
        ch = ClientChannel(rec, ping=ping, lag_s=0.05, recover_s=0.5)
        task = asyncio.create_task(ch.run())
        await enter_lag(ch, ping, 1)
        ping.futures[0].set_result(None)  # the late pong that caused lag
        await settle()
        assert ch._calm_since is None
        await until(lambda: len(ping.futures) == 2)
        ping.futures[1].set_result(None)  # probe answered at once
        await settle()
        assert ch._calm_since is not None
        await until(lambda: len(ping.futures) == 3)
        await asyncio.sleep(0.1)  # next probe answered late
        ping.futures[2].set_result(None)
        await settle()
        assert ch._calm_since is None and ch.lagging and ch.lag_exits == 0
        ch.close()
        await task

    asyncio.run(main())
