# Results data for the report and charts (handoff to muhdbhz)

From Frankfurters, 2 Nov. These are the final numbers behind the results section (`report/results.md`) and `HANDOFF.md` Day 4 part 2. The raw harness output (`results/`, ~300 MB, gitignored) stays on my VM. This folder is the small curated copy (160 KB) that the charts should use. Ask me if you need anything else from the raw runs.

## Read this first

- **Environment label for every chart:** "Ubuntu 26.04 VM (Apple Silicon, 2 vCPU), timer drift +0–1%, median of 3 runs". The team agreed on 2 Nov that this VM is our one reference environment (see `CLAUDE.md`), so use this label on every chart.
- **Before / after:** "before" = plain FIFO (`LIVERECALL_NO_COALESCE=1`, our overload policy off); "after" = the submitted server. Same command, interleaved runs.
- **Medians:** the JSON files hold **every run**, not the median. Take the median across the runs of a scenario for each metric (that is what the report tables show). For example, T2 P95 = median of the three `p95_ms` values in `public_tests.json["t2"]`.
- **Units:** caption age in ms; `*_pct` in percent; RSS in MB.
- **Code version:** final policy (commit `0f026e5`). Exceptions: three T5 profiles (`delay_100ms`, `loss_3pct`, `reorder`) and convergence ran one revision earlier. That makes no difference because they never hit the code that changed (slow-link detection).

## Files

| File | Contents | Suggested chart |
|---|---|---|
| `public_tests.json` | `{"t1"\|"t2"\|"t3"\|"t4"\|"late": [run, run, run]}`. Each run is the analyser's summary: `p50_ms`, `p95_ms`, `p99_ms`, `fresh_pct`, `fresh_final_pct`, `final_completeness_pct`, `normal_update_coverage_pct`, `duplicates`, `stale`, `connection_errors`; `normal_clients` / `slow_clients` sub-dicts (T3); `recovery_p50_ms` / `recovery_p95_ms` (T4) | **Fig 1:** grouped bars of P50/P95/P99 per test, log y-axis (T3's P99 is 7.6 s vs ms elsewhere) |
| `t3_before_after.json` | `{"before": [3 runs], "after": [3 runs]}`, same fields, T3 public command | **Fig 5 / before-after:** bars for final completeness (95.13% vs 100%) and slow-client P95 (28.1 s vs 9.3 s) |
| `t5.json` | `delay_loss_final`, `delay_loss_part1_code`, `delay_loss_fifo`, `delay_100ms`, `loss_3pct`, `reorder`, 3 runs each | **Fig 2:** P50/P95/P99 per netem profile. Optionally `delay_loss` final vs FIFO coverage. Use `delay_loss_final` for the main bars; `part1_code` is only for the "bug we fixed" story |
| `bandwidth.json` | `final_code` / `part1_code` → `runs.before` / `runs.after` (3 each). Each run also has `link_mbit_avg`, `link_mbit_peak5s`, `tbf_dropped`, `server_rss_mb_max`, and `server` (`[stats]` peaks) | **Main before/after:** bars for final completeness, P50, P95, fresh finals %, coverage. Use `final_code` |
| `bandwidth_windows_10s.csv` | `config, run, window_start_s, messages, p50_ms, p95_ms`: caption age per 10 s window, before vs after, 3 runs | **Fig 3:** line chart over time. FIFO flat at ~1.1 s, ours lower |
| `caption_age_percentiles.csv` | Percentiles 1–99 + max of caption age (ms), **run 1 only**, columns `t3_{after,before}_{normal,slow}`, `bandwidth_{after,before}`, `t5_delay_loss_{final,fifo}` | CDF-style curves (x = caption age, y = percentile), e.g. T3 slow clients before vs after |
| `burst.json` | Faster / bursty source (`burst_test.py`): `fast5_mixed`, `fast12`, `burst`, 3 runs each; pacing in `pace` | Optional: one more row in the T1–T5 bar chart, or a table |
| `stress.json` | `s300`, `s500`, `s500slow`, 3 runs each (same fields) | Optional: P95 vs client count (20 / 100 / 300 / 500, using T1/T2 for the small ones) |
| `convergence.json` | `slow30_of_120` (3 runs per config) and `slow90_of_230` (1 run): `convergence_test.py` summaries incl. per-switch metrics | Optional; the report uses a table |
| `memory_5min.json` | Analyser summaries of the 5-minute run, `after` and `before_fifo` | – |
| `memory_5min_rss_{after,before_fifo}.csv` | `elapsed_s, rss_mb`, one sample per second for 300 s (from `monitor_server.py`) | **Fig 4:** RSS over time, both configs (flat at ~37 MB). Set the y-axis from 0 so flat looks flat |
| `memory_5min_stats_{after,before_fifo}.csv` | The server's `[stats]` line every 10 s: `clients, lagging, history, max_pending, coalesced_total, ...` | **Fig 4 (second axis or panel):** `history` (finals kept), rising then capped at 120 |

## Numbers to sanity-check your charts against (medians)

- T2: P50 6.6 / P95 14.3 / P99 17.5 ms, 100% finals.
- T3 before/after: finals 95.13% vs 100%; slow-client P95 28.1 s vs 9.3 s; normal-client P95 8.4 vs 9.7 ms.
- Bandwidth before/after: finals 98.1% vs 100%; P50 1117 vs 535 ms; P95 1593 vs 1295 ms; fresh finals 0% vs 50.1%; coverage 97.9% vs 57.9%.
- T5 `delay_loss`: P50 262 / P95 770 / P99 1103 ms, coverage 99.93%.
- Memory: 36.9 → 37.1 MB (ours), 36.9 → 37.4 MB (FIFO).

## Notes

- `matplotlib` is not installed in our venv. If you plot from the repo, add it to `requirements-dev.txt` (dev only), never to `requirements.txt`.
- Figure numbers match the `[Figure N]` slots in `report/results.md`; rename freely if the report orders them differently.
- `report/build_data.py` regenerated this folder from `results/`. It only works on my VM, where the raw runs are.
