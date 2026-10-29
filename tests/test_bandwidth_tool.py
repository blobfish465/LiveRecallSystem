"""Pure parts of bandwidth_test.py (the experiment itself needs tc and root, see TESTING.md).
The server-log parsing it shares with check.py is tested in test_check.py."""

import bandwidth_test as bt

TC_STATS = """qdisc prio 1: root refcnt 2 bands 3 priomap 2 2 2 2 2 2 2 2 2 2 2 2 2 2 2 2
 Sent 99 bytes 9 pkt (dropped 1, overlimits 2 requeues 0)
 backlog 0b 0p requeues 0
qdisc tbf 10: parent 1:1 rate 2Mbit burst 4000b lat 400ms
 Sent 2238905 bytes 145 pkt (dropped 7, overlimits 341 requeues 0)
 backlog 0b 0p requeues 0
"""


def test_tbf_stats_come_from_the_tbf_block_not_the_root():
    assert bt.parse_tbf_stats(TC_STATS) == {"bytes": 2238905, "pkts": 145, "dropped": 7, "overlimits": 341}


def test_no_tbf_means_no_counters():
    assert bt.parse_tbf_stats("qdisc noqueue 0: root refcnt 2\n") is None


def test_link_rates_average_and_sustained_peak():
    # 0.5 s samples: 10 s at 1 MB/s, then 10 s at 3 MB/s
    samples = [(0.5 * i, int(1e6 * 0.5 * i)) for i in range(21)]
    samples += [(10 + 0.5 * i, int(10e6 + 3e6 * 0.5 * i)) for i in range(1, 21)]
    rates = bt.link_rates(samples)
    assert rates["link_mbit_avg"] == 16.0
    assert rates["link_mbit_peak5s"] == 24.0


def test_link_rates_need_two_samples():
    assert bt.link_rates([(0.0, 0)]) == {}


def test_run_log_must_show_our_server_in_the_requested_mode():
    import pytest
    listening = "LiveRecall server listening on ws://0.0.0.0:8791\n"
    bt.check_run_log(listening, "after", 8791)
    bt.check_run_log("baseline mode: --no-coalesce (plain FIFO)\n" + listening, "before", 8791)
    with pytest.raises(SystemExit):  # an exported LIVERECALL_NO_COALESCE leaked into "after"
        bt.check_run_log("baseline mode: --no-coalesce (plain FIFO)\n" + listening, "after", 8791)
    with pytest.raises(SystemExit):  # a stale server answered run_demo's readiness probe instead of ours
        bt.check_run_log("source connected\n", "after", 8791)
