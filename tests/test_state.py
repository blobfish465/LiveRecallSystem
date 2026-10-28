import json
import math

import pytest

from liverecall.state import CaptionState, history_message, make_caption


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def upd(seg, rev, final=False, text=None, ts=1.0, **extra):
    msg = {
        "type": "CAPTION_UPDATE", "session": "L1", "segment": seg, "revision": rev,
        "source_ts": ts, "final": final, "text": text or f"s{seg} r{rev}",
    }
    msg.update(extra)
    return msg


def feed_segment(state, seg, revisions=3):
    for rev in range(1, revisions + 1):
        assert state.apply(upd(seg, rev, final=rev == revisions)) is not None


# ------------------------------------------------------------ revision rules
def test_newer_revisions_accepted_older_and_equal_rejected():
    s = CaptionState()
    assert s.apply(upd(1, 1)) is not None
    assert s.apply(upd(1, 3)) is not None
    assert s.apply(upd(1, 2)) is None  # older
    assert s.apply(upd(1, 3)) is None  # duplicate


def test_final_is_durable():
    s = CaptionState()
    assert s.apply(upd(1, 2, final=True)) is not None
    assert s.apply(upd(1, 1)) is None
    assert s.apply(upd(1, 2, final=True)) is None
    assert s.apply(upd(1, 5)) is None  # even a "newer" revision after final


@pytest.mark.parametrize("revisions", [1, 2, 3, 8])
def test_variable_revision_counts(revisions):
    s = CaptionState()
    feed_segment(s, 1, revisions)
    feed_segment(s, 2, revisions)
    items = s.history_for(0, 0, 60)
    assert [(c.segment, c.revision) for c in items] == [(1, revisions), (2, revisions)]
    assert s.active_len == 0


def test_unknown_fields_ignored_and_not_forwarded():
    s = CaptionState()
    cap = s.apply(upd(1, 1, phase="burst", extra={"x": 1}))
    assert cap is not None
    msg = json.loads(cap.wire)
    assert set(msg) == {"type", "session", "segment", "revision", "source_ts", "final", "text"}


@pytest.mark.parametrize("bad", [
    {"segment": True},
    {"segment": 0},
    {"segment": -3},
    {"segment": "1"},
    {"revision": None},
    {"final": "true"},
    {"final": 1},
    {"text": 42},
    {"source_ts": math.nan},
    {"source_ts": "now"},
])
def test_malformed_updates_rejected(bad):
    msg = upd(1, 1)
    msg.update(bad)
    s = CaptionState()
    assert s.apply(msg) is None


def test_missing_fields_rejected():
    s = CaptionState()
    for key in ("segment", "revision", "source_ts", "final", "text"):
        msg = upd(1, 1)
        del msg[key]
        assert s.apply(msg) is None


def test_integer_valued_float_segment_accepted():
    c = make_caption(upd(2.0, 1.0), 0.0)
    assert c is not None and c.segment == 2


# ----------------------------------------------------------------- bounds
def test_history_bounded_by_count():
    s = CaptionState(history_max_items=5)
    for seg in range(1, 21):
        feed_segment(s, seg)
    assert s.history_len == 5
    assert [c.segment for c in s.history_for(1, 3, 60)] == [16, 17, 18, 19, 20]


def test_history_bounded_by_time_and_evicted_segments_rejected():
    clock = FakeClock()
    s = CaptionState(history_window_s=90, clock=clock)
    feed_segment(s, 1)
    clock.t += 100
    feed_segment(s, 2)
    assert s.history_len == 1
    # Segment 1 was evicted; a straggler update for it must not be re-accepted.
    assert s.apply(upd(1, 9)) is None
    assert s.apply(upd(1, 9, final=True)) is None


def test_active_segments_capped():
    s = CaptionState(max_active=4)
    for seg in range(1, 11):
        assert s.apply(upd(seg, 1)) is not None
    assert s.active_len == 4
    assert s.apply(upd(1, 2)) is None  # abandoned + evicted


def test_long_lecture_stays_bounded():
    clock = FakeClock()
    s = CaptionState(clock=clock)
    for seg in range(1, 5001):  # ~1 hour at 0.75 s/segment
        feed_segment(s, seg)
        clock.t += 0.75
    assert s.history_len <= 121  # ~90 s window
    assert s.active_len == 0


# --------------------------------------------------------------- recovery
def test_fresh_join_gets_only_requested_window():
    clock = FakeClock()
    s = CaptionState(clock=clock)
    for seg in range(1, 81):  # 80 segments, 1 s apart
        feed_segment(s, seg)
        clock.t += 1.0
    segs = [c.segment for c in s.history_for(0, 0, 30)]
    assert segs == list(range(51, 81))
    assert len(s.history_for(0, 0, 999)) == 60  # clamped to 60 s
    # Deliberately the wrong type: client hellos are untrusted JSON.
    assert len(s.history_for(0, 0, "junk")) == 30  # type: ignore[arg-type]  # invalid -> default 30
    assert len(s.history_for(0, 0, None)) == 30


def test_resume_excludes_held_finals_and_is_ordered():
    s = CaptionState()
    for seg in range(1, 11):
        feed_segment(s, seg)
    items = s.history_for(6, 3, 30)
    assert [c.segment for c in items] == [7, 8, 9, 10]
    assert all(c.final for c in items)
    assert s.history_for(10, 3, 30) == []


def test_resume_beyond_history_seconds_still_complete():
    """A client away longer than history_seconds gets everything retained."""
    clock = FakeClock()
    s = CaptionState(clock=clock)
    for seg in range(1, 61):
        feed_segment(s, seg)
        clock.t += 1.0
    items = s.history_for(10, 3, 5)
    assert [c.segment for c in items] == list(range(11, 61))


def test_resume_same_segment_uses_revision():
    s = CaptionState()
    feed_segment(s, 1)
    assert s.history_for(1, 2, 30)[0].segment == 1  # client held an older rev
    assert s.history_for(1, 3, 30) == []


def test_history_never_contains_provisionals():
    s = CaptionState()
    feed_segment(s, 1)
    s.apply(upd(2, 1))
    s.apply(upd(2, 2))
    assert [c.segment for c in s.history_for(0, 0, 60)] == [1]


def test_history_message_is_valid_json():
    s = CaptionState()
    feed_segment(s, 1)
    feed_segment(s, 2)
    msg = json.loads(history_message(s.history_for(0, 0, 30)))
    assert msg["type"] == "HISTORY"
    assert [i["segment"] for i in msg["items"]] == [1, 2]
    assert all(i["type"] == "CAPTION_UPDATE" and i["final"] for i in msg["items"])
    assert json.loads(history_message([])) == {"type": "HISTORY", "items": []}
