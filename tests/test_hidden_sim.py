import time

import pytest

import hidden_sim as hs


def test_scenario_names_unique_and_components_known():
    names = [s.name for s in hs.SCENARIOS]
    assert len(names) == len(set(names))
    assert {s.component for s in hs.SCENARIOS} == set(hs.COMPONENTS)


def test_every_scenario_stays_inside_the_teaching_team_bounds():
    """No reader slower than the finals stream; roles fit the client count."""
    for sc in hs.SCENARIOS:
        # Outside bursts (bursts are temporary, which the bounds allow).
        finals_per_s = 1.0 / ((sc.revisions - 1) * sc.revision_delay + sc.segment_gap)
        if sc.slow_pct:
            assert 1000.0 / sc.slow_ms > finals_per_s, sc.name
        hs.roles(sc)  # raises if roles exceed clients


def test_select_by_component_and_name():
    assert {s.component for s in hs.select("adv1")} == {"adv1"}
    assert [s.name for s in hs.select("mass_all,slow30")] == ["slow30", "mass_all"]
    assert hs.select(None) == hs.SCENARIOS
    with pytest.raises(SystemExit):
        hs.select("nope")


def test_segments_match_public_pace_and_scale_with_rate():
    public = hs.Scenario("x", "adv1", "", duration=60)
    assert hs.segments_for(public) == 76  # (60 - 1 - 2) / 0.75, same as run_demo.py
    faster = hs.Scenario("y", "adv1", "", duration=60, revision_delay=0.0667, segment_gap=0.1167)
    assert hs.segments_for(faster) == pytest.approx(3 * 76, abs=2)


def test_roles_are_disjoint_and_counted():
    sc = hs.Scenario("r", "recon", "", clients=10, slow_pct=20, disc_pct=30, late_pct=10)
    r = hs.roles(sc)
    assert sum(s for s, _, _ in r) == 2 and sum(d for _, d, _ in r) == 3 and sum(l for *_, l in r) == 1
    assert all(sum(flags) <= 1 for flags in r)


def test_tc_commands_chain_netem_then_tbf_on_the_port_only():
    cmds = hs.tc_commands(hs.Shape(rate="2mbit", delay_ms=20, jitter_ms=5, loss_pct=1), "lo", 8793)
    joined = [" ".join(c) for c in cmds]
    assert joined[0].startswith("qdisc add dev lo root handle 1: prio")
    assert "parent 1:1 handle 10: netem delay 20ms 5ms distribution normal loss 1%" in joined[1]
    assert "parent 10:1 handle 20: tbf rate 2mbit burst 4000 latency 400ms" in joined[2]
    assert any("dport 8793" in c for c in joined) and any("sport 8793" in c for c in joined)


def test_tc_commands_rate_only_and_loss_only():
    rate = [" ".join(c) for c in hs.tc_commands(hs.Shape(rate="1mbit"), "lo", 1)]
    assert not any("netem" in c for c in rate) and any("parent 1:1 handle 20: tbf" in c for c in rate)
    loss = [" ".join(c) for c in hs.tc_commands(hs.Shape(loss_pct=3), "lo", 1)]
    assert any("netem loss 3%" in c for c in loss) and not any("tbf" in c for c in loss)


def row(ts, lat, final="1", slow="0", status="accepted", event="CAPTION_UPDATE"):
    return {"event_type": event, "status": status, "slow": slow, "final": final,
            "receive_ts": str(ts), "latency_ms": str(lat)}


def test_tail_metrics_use_only_normal_clients_in_the_last_window():
    end = time.time()
    rows = [row(end - 100, 9999),                       # before the tail window
            row(end - 10, 100, final="0"), row(end - 5, 200), row(end - 1, 900),
            row(end - 2, 5000, slow="1"),                # slow client: excluded
            row(end - 3, 50, status="duplicate")]        # not accepted: excluded
    m = hs.tail_metrics(rows, end, 500.0)
    assert m["tail_messages"] == 3
    assert m["tail_fresh_pct"] == pytest.approx(200 / 3)
    assert m["tail_provisional_pct"] == pytest.approx(100 / 3)


def test_problems_flag_lost_finals_and_bad_state_but_not_connection_errors():
    ok = {"final_completeness_pct": 100.0, "finals_due_pct": 100.0, "duplicates": 0, "stale": 0,
          "connection_errors": 7}
    assert hs.problems_of(ok) == []
    bad = {"final_completeness_pct": 99.5, "finals_due_pct": 99.5, "duplicates": 1, "stale": 0}
    assert hs.problems_of(bad) == ["finals due 99.50%", "duplicates=1"]
    # Only finals sent in the last GRACE_S missing: the public metric < 100, the verdict ok.
    late = {"final_completeness_pct": 98.0, "finals_due_pct": 100.0, "duplicates": 0, "stale": 0}
    assert hs.problems_of(late) == []


def frow(client, seg, sent, status="accepted", event="CAPTION_UPDATE"):
    return {"client_id": str(client), "event_type": event, "final": "1", "segment": str(seg),
            "source_ts": str(sent), "status": status}


def test_finals_due_excuses_only_the_grace_window():
    end = 100.0
    rows = [frow(0, 1, 80), frow(0, 2, 90), frow(0, 3, 96), frow(0, 4, 98.5),  # client 0 has 1-4
            frow(1, 1, 80), frow(1, 3, 96, status="recovered", event="HISTORY_ITEM"),  # client 1 lacks 2 and 4
            {"client_id": "2", "event_type": "CONNECT"}]  # client 2 got nothing
    m = hs.finals_due(rows, segments=5, end_epoch=end, grace_s=3.0)
    # Due = segments sent by end - 3 = 97: 1, 2, 3 (seg 4 sent at 98.5, seg 5 never).
    assert m["finals_due_segments"] == 3
    assert m["finals_due_pct"] == pytest.approx(100 * (3 + 2 + 0) / 9)


def test_finals_due_counts_a_segment_nobody_received_before_a_due_one():
    rows = [frow(0, 1, 10), frow(0, 3, 30)]  # nobody got segment 2, but 3 was due
    m = hs.finals_due(rows, segments=3, end_epoch=100, grace_s=3)
    assert m["finals_due_segments"] == 3 and m["finals_due_pct"] == pytest.approx(200 / 3)


def test_unique_transcript_never_repeats_and_is_deterministic(tmp_path):
    a = hs.unique_transcript(tmp_path / "a.txt", 200).read_text(encoding="utf-8").splitlines()
    b = hs.unique_transcript(tmp_path / "b.txt", 200).read_text(encoding="utf-8").splitlines()
    assert a == b and len(a) == len(set(a)) == 200
    assert all(len(line.split()) >= 4 for line in a)  # caption_source makes 3 revisions from >= 4 words


def test_segments_scale_with_revision_count():
    eight = hs.Scenario("r8", "adv1", "", duration=60, revisions=8, revision_delay=0.1, segment_gap=0.35)
    assert hs.segments_for(eight) == 54  # (60 - 3) / (7 * 0.1 + 0.35)


def test_source_command_public_unless_more_revisions_or_bursts():
    assert "caption_source.py" in hs.source_command(hs.Scenario("p", "adv1", ""), "ws://x", 10, None)
    custom = hs.source_command(hs.Scenario("c", "adv1", "", revisions=8), "ws://x", 10, None)
    assert "custom_source.py" in custom and custom[custom.index("--revisions") + 1] == "8"
    bursty = hs.source_command(hs.Scenario("b", "adv1", "", burst_every=10, burst_len=3, burst_factor=8), "ws://x", 10, None)
    assert "custom_source.py" in bursty and "--burst-factor" in bursty


def test_flappers_come_from_the_right_pool():
    slow = hs.Scenario("f", "recon", "", clients=20, slow_pct=10, flap_pct=10, flap_slow=True)
    r = hs.roles(slow)
    assert hs.flappers(slow) == {i for i, (s, _, _) in enumerate(r) if s}
    plain = hs.Scenario("g", "recon", "", clients=20, slow_pct=10, disc_pct=10, flap_pct=20)
    r = hs.roles(plain)
    assert len(hs.flappers(plain)) == 4 and all(not any(r[i]) for i in hs.flappers(plain))
    with pytest.raises(ValueError):
        hs.flappers(hs.Scenario("h", "recon", "", clients=10, slow_pct=10, flap_pct=50, flap_slow=True))


def test_flapping_connect_aborts_and_delays_reconnects(monkeypatch):
    import asyncio

    class Transport:
        aborted = 0

        def abort(self):
            Transport.aborted += 1

    class Inner:
        async def __aenter__(self):
            ws = type("WS", (), {})()
            ws.transport = Transport()
            return ws

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(hs.random, "uniform", lambda a, b: 1.0)

    async def main():
        plan = hs.FlapPlan(every=0.05, down=0.1)
        loop = asyncio.get_running_loop()
        async with hs.FlappingConnect(Inner(), plan):
            await asyncio.sleep(0.08)
        assert Transport.aborted == 1 and plan.connects == 1
        t = loop.time()
        async with hs.FlappingConnect(Inner(), plan):  # a reconnect waits `down` first
            pass
        assert loop.time() - t >= 0.09 and plan.connects == 2
        assert Transport.aborted == 1  # left early: the timer was cancelled

    asyncio.run(main())


def test_merge_csvs_keeps_one_header_and_all_rows(tmp_path):
    parts = []
    for i in range(3):
        p = tmp_path / f"p{i}.csv"
        p.write_text("client_id,event_type\n" + f"{i},CONNECT\n{i},HISTORY\n", encoding="utf-8")
        parts.append(p)
    out = tmp_path / "all.csv"
    hs.merge_csvs(parts, out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "client_id,event_type" and len(lines) == 7
    assert not any(p.exists() for p in parts)


def test_custom_source_revisions_grow_to_the_sentence():
    from caption_source import DEFAULT_SENTENCES
    from custom_source import in_burst, revisions_of
    for n in (1, 3, 8):
        for sentence in DEFAULT_SENTENCES:
            r = revisions_of(sentence, n)
            assert len(r) == n and r[-1] == sentence
            assert all(len(a) < len(b) and b.startswith(a) for a, b in zip(r, r[1:]))
    with pytest.raises(ValueError):
        revisions_of("ab", 3)
    assert [in_burst(t, 10, 3) for t in (0, 2.9, 3.1, 10.5)] == [True, True, False, True]
    assert not in_burst(1.0, 0, 3)
