"""Pure parts of convergence_test.py (the experiment itself runs the server and the source for minutes)."""

import time

import convergence_test as ct


def test_slow_until_is_true_before_the_switch_and_false_after():
    assert bool(ct.SlowUntil(time.monotonic() + 10))
    assert not bool(ct.SlowUntil(time.monotonic() - 1))
    assert int(ct.SlowUntil(time.monotonic() + 10)) == 1  # logged as the slow role either way


def upd(client, t, latency_ms, final, slow="1"):
    return {"client_id": client, "slow": slow, "event_type": "CAPTION_UPDATE", "receive_ts": f"{t:.6f}",
            "latency_ms": f"{latency_ms:.3f}", "final": "1" if final else "0", "status": "accepted"}


def test_metrics_relative_to_the_switch():
    switch, end = 1000.0, 1090.0
    rows = [
        upd("1", 990.0, 8000, True), upd("1", 995.0, 9000, True),        # slow phase: stale finals only
        upd("1", 1000.05, 9500, True), upd("1", 1000.10, 8600, True),    # backlog drains right after the switch
        upd("1", 1000.20, 40, True), upd("1", 1012.0, 30, True),         # fresh from here on
        upd("1", 1035.0, 20, False), upd("1", 1035.2, 25, False), upd("1", 1035.4, 30, True),  # full delivery back
        upd("1", 1038.0, 20, False), upd("1", 1042.0, 20, False), upd("1", 1046.0, 20, False),
    upd("1", 1050.0, 20, False), upd("1", 1054.0, 20, False), upd("1", 1058.0, 20, False),
    upd("1", 1062.0, 20, False), upd("1", 1066.0, 20, False),
    upd("1", 1070.0, 20, False), upd("1", 1075.0, 30, True),         # tail (from 1060): 3 provisionals of 4
        upd("7", 1005.0, 10, True, slow="0"),                            # a normal client: ignored
        {"client_id": "1", "slow": "1", "event_type": "HISTORY", "receive_ts": "999.0"},  # not an update
    ]
    m = ct.convergence_metrics(rows, switch, end, threshold_ms=500.0)
    assert m["converging_clients"] == 1 and m["never_full_delivery"] == 0
    assert m["backlog_at_switch_ms"] == 9500.0
    assert abs(m["fresh_after_s"] - 0.10) < 1e-6
    assert abs(m["drain_s"] - 0.05) < 1e-6  # from the first read after the switch (1000.05)
    assert abs(m["full_delivery_after_s"] - 35.0) < 1e-6
    assert m["slow_phase_p95_ms"] > 8000
    assert m["settled_p95_ms"] <= 30.0  # only updates from switch+10 s count
    assert m["provisional_share_tail_pct"] == 75.0
    assert m["clients"][0]["updates_after"] == 17


def test_client_that_never_gets_a_provisional_back_is_counted():
    rows = [upd("1", 1001.0, 9000, True), upd("1", 1002.0, 100, True), upd("2", 1001.0, 50, False)]
    m = ct.convergence_metrics(rows, 1000.0, 1100.0, 500.0)
    assert m["converging_clients"] == 2
    assert m["never_full_delivery"] == 1
    assert m["full_delivery_after_s"] == 1.0  # median over the clients that did (client 2 only)
    assert m["fresh_after_s"] == 0.5  # median of 1.0 (client 1: last stale update at +1 s) and 0.0 (client 2)


def test_a_leftover_provisional_followed_by_finals_only_is_not_full_delivery():
    # The 200 s run: one provisional just after the switch (left over from a brief
    # lag exit), then finals only until the end. A first-provisional rule says 0.4 s.
    rows = [upd("1", 1000.4, 300, False)] + [upd("1", 1001.0 + 0.75 * i, 20, True) for i in range(100)]
    m = ct.convergence_metrics(rows, 1000.0, 1100.0, 500.0)
    assert m["full_delivery_after_s"] is None and m["never_full_delivery"] == 1


def test_sustained_from_finds_the_start_of_the_last_unbroken_stretch():
    assert ct.sustained_from([], 10.0) is None
    assert ct.sustained_from([1.0, 2.0, 3.0], 20.0) is None        # stopped 17 s before the end
    assert ct.sustained_from([1.0, 2.0, 3.0], 4.0) == 1.0          # unbroken from the start
    assert ct.sustained_from([1.0, 30.0, 31.0, 33.0], 35.0) == 30.0  # a 29 s hole: counts from 30
    assert ct.sustained_from([1.0, 6.0], 6.0) == 1.0               # exactly GAP_S is still unbroken


def test_problems_cover_correctness_and_convergence():
    good = {"final_completeness_pct": 100.0, "duplicates": 0, "stale": 0, "invalid_history_nonfinal": 0,
            "connection_errors": 0, "convergence": {"never_full_delivery": 0}}
    assert ct.problems_of(good) == []
    bad = dict(good, final_completeness_pct=99.0, stale=2, convergence={"never_full_delivery": 3})
    msgs = ct.problems_of(bad)
    assert len(msgs) == 3 and any("never returned" in m for m in msgs)


def test_median_of_handles_nested_and_missing_values():
    runs = [{"a": 1, "b": {"x": 10}}, {"a": 3, "b": {"x": None}}, {"a": None, "b": "junk"}]
    assert ct.median_of(runs, "a", None) == 2
    assert ct.median_of(runs, "b", "x") == 10
    assert ct.median_of(runs, "c", None) is None


def test_settled_window_starts_exactly_settle_s_after_the_switch_and_uses_p95():
    switch = 1000.0
    rows = [upd("1", switch + 1.0, 9000, True),
            upd("1", switch + ct.SETTLE_S - 0.01, 3000, True)]           # just before the window: excluded
    rows += [upd("1", switch + ct.SETTLE_S + i, v, True) for i, v in enumerate([20, 20, 20, 20, 200])]
    m = ct.convergence_metrics(rows, switch, switch + 60, 500.0)
    assert m["settled_p95_ms"] == ct.percentile([20, 20, 20, 20, 200], 95)
    assert m["settled_p95_ms"] < 200  # P95 interpolates; a max() would give 200


def test_never_back_is_only_judged_when_the_fast_phase_can_show_it():
    s = {"final_completeness_pct": 100.0, "convergence": {"never_full_delivery": 10}}
    assert ct.problems_of(s, judge_full_delivery=True)
    assert ct.problems_of(s, judge_full_delivery=False) == []
    assert not ct.full_delivery_observable(90.0)           # default: 120 s run, switch at 30 s
    assert ct.full_delivery_observable(ct.RECOVER_MAX_S + 11.0)


class Args:
    def __init__(self, **kw):
        self.tag, self.clients, self.converging = "", 100, 10
        self.slow_until, self.duration, self.slow_ms = 30.0, 120.0, 500.0
        self.fresh_threshold_ms, self.source_start_delay = 500.0, 1.0
        self.__dict__.update(kw)


def test_file_names_carry_every_timing_setting():
    assert ct.setting_stem(Args()) == "convergence_c100_k10_s30_d120_m500"
    assert ct.setting_stem(Args(slow_until=90.0, duration=230.0)) != ct.setting_stem(Args())


def test_only_merge_refuses_a_summary_made_with_other_settings():
    import pytest
    previous = {"args": {"slow_until": 30.0, "duration": 120.0}, "results": {"before": [1]}}
    assert ct.merge_previous(previous, Args(), {"after": [2]}) == {"before": [1], "after": [2]}
    with pytest.raises(SystemExit, match="slow_until"):
        ct.merge_previous(previous, Args(slow_until=90.0), {"after": [2]})


def test_client_namespace_has_everything_the_harness_client_reads():
    import inspect
    import re
    from pathlib import Path
    import load_clients
    wanted = set(re.findall(r"args\.(\w+)", inspect.getsource(load_clients.simulated_client))) - {"_global_start"}
    ns = ct.client_namespace(Args(), "ws://127.0.0.1:1", Path("x.csv"))
    assert wanted and all(hasattr(ns, name) for name in wanted), wanted - set(vars(ns))
