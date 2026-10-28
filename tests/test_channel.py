import asyncio
import json

from liverecall.channel import ClientChannel
from liverecall.state import make_caption


def cap(seg, rev, final=False):
    return make_caption({
        "segment": seg, "revision": rev, "final": final,
        "source_ts": 1.0, "text": f"s{seg}r{rev}",
    }, 0.0)


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
    assert ch.pending == 60 and ch.dropped == 0


def test_over_cap_drops_only_superseded_provisionals():
    ch = ClientChannel(Recorder(), max_pending=25)
    for seg in range(1, 21):
        for rev in (1, 2, 3):
            ch.offer(cap(seg, rev, rev == 3))
    queued = [o.caption for o in ch._queue]
    assert len(queued) <= 25
    assert not ch.overflowed
    # Finals are never dropped while the channel is open.
    assert [c.segment for c in queued if c.final] == list(range(1, 21))
    # Only provisionals were dropped, and each had a newer item of its segment.
    assert ch.dropped == 60 - len(queued) == 35


def test_latest_provisional_of_active_segment_kept():
    ch = ClientChannel(Recorder(), max_pending=3)
    ch.offer(cap(1, 3, True))
    ch.offer(cap(2, 3, True))
    ch.offer(cap(3, 1))
    ch.offer(cap(3, 2))  # over cap -> drops (3,1), keeps newest (3,2)
    assert [(o.caption.segment, o.caption.revision) for o in ch._queue] == [(1, 3), (2, 3), (3, 2)]


def test_overflow_when_backlog_is_all_finals():
    hits = []
    ch = ClientChannel(Recorder(), max_pending=5, on_overflow=hits.append)
    for seg in range(1, 7):
        ch.offer(cap(seg, 1, True))
    assert ch.overflowed and ch.closed and hits == [ch]
    assert ch.pending == 0
    ch.offer(cap(7, 1, True))  # ignored once closed
    assert ch.pending == 0


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
    assert ch._queue[0].caption.final
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
