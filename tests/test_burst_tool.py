"""burst_test.py: how many segments fit a run at a given pace, and how many updates they make."""

import burst_test
from caption_source import DEFAULT_SENTENCES, make_revisions


def test_public_pace_matches_run_demo_budget():
    # run_demo.py: floor((duration - 1 - 2) / 0.75) segments at the public pace.
    segments, updates = burst_test.plan(DEFAULT_SENTENCES, 0.20, 0.35, 40)
    assert segments == 49
    assert updates == sum(len(make_revisions(DEFAULT_SENTENCES[i % len(DEFAULT_SENTENCES)]))
                          for i in range(segments))


def test_mixed_transcript_has_one_and_three_revision_segments():
    counts = {len(make_revisions(s)) for s in burst_test.MIXED}
    assert counts == {1, 3}
    segments, updates = burst_test.plan(burst_test.MIXED, 0.05, 0.05, 15)
    assert segments < updates < 3 * segments


def test_faster_pace_fits_more_segments():
    slow, _ = burst_test.plan(DEFAULT_SENTENCES, 0.20, 0.35, 40)
    fast, _ = burst_test.plan(DEFAULT_SENTENCES, 0.02, 0.02, 40)
    assert fast > 10 * slow
