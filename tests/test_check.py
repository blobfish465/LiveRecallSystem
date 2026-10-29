"""Pure parts of check.py: scenario selection, server-log parsing and the gate rules.

The gate itself drives run_demo.py for minutes, so it is exercised by running
`python check.py` before every handoff, not here.
"""

import time
from pathlib import Path

import check

STATS_LOG = (
    "=== Run 1/1 ===\n"
    "Clients=500, duration=40s, source segments=50\n"
    "LiveRecall server listening on ws://0.0.0.0:8790\n"
    "[stats 21:39:36] clients=500 lagging=490 history=10 active=0 max_pending=5 "
    "coalesced_total=7470 lag_entries_total=490 hopeless_closes_total=0\n"
    "client overflow, closing: id=7\n"
    "[stats 21:39:46] clients=499 lagging=300 history=23 active=0 max_pending=2 "
    "coalesced_total=20210 lag_entries_total=490 hopeless_closes_total=1\n"
)


def test_default_gate_excludes_stress_and_stress_excludes_the_rest():
    default = check.scenario_names(None, stress=False)
    stress = check.scenario_names(None, stress=True)
    assert default == ["t1", "t2", "t3", "t4", "late"]
    assert stress == ["s300", "s500", "s500slow"]
    assert set(default) | set(stress) == set(check.SCENARIOS)


def test_only_wins_over_stress():
    assert check.scenario_names("t1,s500", stress=True) == ["t1", "s500"]


def test_stats_lines_give_peaks_and_final_totals():
    assert check.parse_stats(STATS_LOG) == {
        "stats_lines": 2, "lagging_max": 490, "max_pending_max": 5,
        "coalesced_total": 20210, "lag_entries_total": 490, "hopeless_closes_total": 1,
    }


def test_no_stats_lines_in_a_short_run():
    assert check.parse_stats("LiveRecall server listening on ws://0.0.0.0:8791\n")["stats_lines"] == 0


def test_hopeless_closes_after_the_last_stats_line_are_counted():
    text = (
        "[stats 1] clients=5 lagging=0 history=1 active=0 max_pending=0 "
        "coalesced_total=0 lag_entries_total=0 hopeless_closes_total=1\n"
        "client overflow, closing: id=1\nclient overflow, closing: id=2\n"
    )
    assert check.parse_stats(text)["hopeless_closes_total"] == 2


def test_a_stats_line_with_another_process_text_glued_in_front_still_counts():
    text = ("source disconnected[stats 1] clients=5 lagging=4 history=1 active=0 max_pending=2 "
            "coalesced_total=9 lag_entries_total=4 hopeless_closes_total=0\n"
            "SOURCE_DONEclient overflow, closing: id=3\n")
    out = check.parse_stats(text)
    assert (out["stats_lines"], out["lagging_max"], out["coalesced_total"]) == (1, 4, 9)
    assert out["hopeless_closes_total"] == 1


def test_runs_are_split_at_the_harness_headers_and_the_preamble_dropped():
    text = ("Clients=20 ...\n=== Run 1/2 ===\nLiveRecall server listening on ws://0.0.0.0:8790\n"
            "[stats 1] lagging=3\n=== Run 2/2 ===\n[stats 2] lagging=1\n")
    pieces = check.split_runs(text)
    assert len(pieces) == 2
    assert check.server_listened(pieces[0], 8790) and not check.server_listened(pieces[1], 8790)
    assert check.parse_stats(pieces[0])["lagging_max"] == 3
    assert check.parse_stats(pieces[1])["lagging_max"] == 1
    assert check.split_runs("no runs at all\n") == []


def test_listening_check_is_per_port():
    assert check.server_listened(STATS_LOG, 8790)
    assert not check.server_listened(STATS_LOG, 8791)


def perfect_summary() -> dict:
    return {"final_completeness_pct": 100.0, "duplicates": 0, "stale": 0,
            "invalid_history_nonfinal": 0, "connection_errors": 0,
            "normal_update_coverage_pct": 100.0,
            "normal_clients": {"p95_ms": 40.0}, "slow_clients": {"p95_ms": 9000.0},
            "server": {"listened": True, "rss_max_mb": 80.0}}


def test_gate_passes_a_perfect_stress_run():
    assert check.gate("s500slow", perfect_summary(), check.SCENARIOS["s500slow"]) == []


def test_gate_fails_on_rss_over_the_ceiling_and_a_stale_server():
    s = perfect_summary()
    s["server"] = {"listened": False, "rss_max_mb": 250.0}
    problems = check.gate("s500", s, check.SCENARIOS["s500"])
    assert any("never listened" in m for m in problems)
    assert any("RSS max 250.0 MB" in m for m in problems)


def test_gate_treats_missing_rss_as_a_failure_only_where_a_ceiling_is_set():
    s = perfect_summary()
    s["server"] = {"listened": True, "rss_max_mb": None}
    assert check.gate("t1", s, check.SCENARIOS["t1"]) == []
    assert any("RSS not measured" in m for m in check.gate("s300", s, check.SCENARIOS["s300"]))


def test_public_scenarios_are_unchanged_by_the_stress_additions():
    for name in ("t1", "t2", "t3", "t4", "late"):
        assert "rss_max_mb" not in check.SCENARIOS[name]
        assert not check.SCENARIOS[name].get("stress")


def test_rss_sampler_keeps_one_peak_per_server_in_order_of_appearance(monkeypatch):
    # run_demo.py starts a fresh server per run: peaks[i] must be run i+1's maximum.
    samples = iter([(100, 10.0), (100, 30.0), (100, 12.0), (None, None), (200, 20.0), (200, 25.0)])
    current = {"rss": None}

    def fake_pid(port):
        pid, current["rss"] = next(samples, (None, None))
        return pid

    monkeypatch.setattr(check, "server_pid", fake_pid)
    monkeypatch.setattr(check, "rss_mb", lambda pid: current["rss"])
    monkeypatch.setattr(check, "RSS_SAMPLE_S", 0.001)
    sampler = check.RssSampler(8790)
    sampler.start()
    time.sleep(0.1)
    assert sampler.stop() == [30.0, 25.0]


def test_rss_sampler_reports_nothing_rather_than_zero_without_a_server(monkeypatch):
    monkeypatch.setattr(check, "server_pid", lambda port: None)
    monkeypatch.setattr(check, "RSS_SAMPLE_S", 0.001)
    sampler = check.RssSampler(8790)
    sampler.start()
    time.sleep(0.02)
    assert sampler.stop() == []


def test_no_proc_means_no_pid_instead_of_a_crash(monkeypatch):
    # Windows / macOS: no /proc. The sampler must not die with a traceback.
    monkeypatch.setattr(check, "Path", lambda p: Path("/nonexistent-proc-for-test") if p == "/proc" else Path(p))
    assert check.server_pid(8790) is None
