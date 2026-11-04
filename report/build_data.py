"""Collect the final-code results (4 Nov re-run, commit 0e8982e) into a small bundle for the charts/report.

Run from the repo root: python report/build_data.py report/data
Reads only results/ (gitignored raw output); writes JSON/CSV summaries (report/data/README.md describes them).
Only reproducible on the machine that holds those results/ files (the reference VM).
"""
import csv
import glob
import json
import os
import re
import statistics
import sys

R = "results"
D = "results/final"
out = sys.argv[1]
os.makedirs(out, exist_ok=True)


def load(path):
    with open(path) as f:
        return json.load(f)


def runs(pattern):
    files = sorted(glob.glob(pattern))
    return [dict(load(f), _file=os.path.basename(f)) for f in files]


def dump(name, obj):
    with open(os.path.join(out, name), "w") as f:
        json.dump(obj, f, indent=1)


# 1. Public tests T1-T4 + late join, final code (check.py --full --runs 3).
dump("public_tests.json", {s: runs(f"{D}/check/check_{s}_run0*.json") for s in ("t1", "t2", "t3", "t4", "late")})

# 2. T3 before/after, final code, interleaved.
dump("t3_before_after.json", {c: runs(f"{D}/t3ba/t3_{c}_r?.json") for c in ("before", "after")})

# 3. T5 network impairment.
dump("t5.json", {
    "delay_loss_final": runs(f"{D}/t5/t5_delay_loss_run0*.json"),
    "delay_loss_fifo": runs(f"{D}/t5/t5_delay_loss_fifo_run0*.json"),
    "delay_100ms": runs(f"{D}/t5/t5_delay_100ms_run0*.json"),
    "loss_3pct": runs(f"{D}/t5/t5_loss_3pct_run0*.json"),
    "reorder": runs(f"{D}/t5/t5_reorder_run0*.json"),
})

# 4. Stress (check.py --stress --runs 3, final code).
dump("stress.json", {s: runs(f"{D}/check_stress/check_{s}_run0*.json") for s in ("s300", "s500", "s500slow")})

# 5. Bandwidth before/after (summaries hold every run + link/tbf/server stats).
dump("bandwidth.json", {
    "final_code": load(f"{R}/bandwidth_limited_final_c500_summary.json"),
})

# 6. Convergence.
dump("convergence.json", {
    "slow30_of_120": load(f"{R}/convergence_final_c100_k10_s30_d120_m500_summary.json"),
    "slow90_of_230": load(f"{R}/convergence_final_c100_k10_s90_d230_m500_summary.json"),
})

# 6b. Faster / bursty source (burst_test.py, 3 runs each).
dump("burst.json", {
    "pace": {"fast5_mixed": "revision-delay 0.05, segment-gap 0.05, mixed 1/3-revision transcript",
             "fast12": "revision-delay 0.02, segment-gap 0.02",
             "burst": "revision-delay 0.0, segment-gap 0.35"},
    "runs": {s: runs(f"{D}/burst/{s}_run0*.json") for s in ("fast5_mixed", "fast12", "burst")},
})

# 7. 5-minute memory run: summaries, RSS series, [stats] series.
dump("memory_5min.json", {"after": load(f"{D}/mem/mem5min_after.json"), "before_fifo": load(f"{D}/mem/mem5min_fifo.json")})
STATS = re.compile(r"^\[stats (\S+)\] (.*)$")
for cfg, stem in (("after", "mem/mem5min_after"), ("before_fifo", "mem/mem5min_fifo")):
    with open(f"{D}/{stem}_rss.csv") as f, open(os.path.join(out, f"memory_5min_rss_{cfg}.csv"), "w") as g:
        g.write(f.read())
    rows = []
    for line in open(f"{D}/{stem}.log"):
        m = STATS.match(line.strip())
        if m:
            kv = dict(p.split("=", 1) for p in m.group(2).split())
            rows.append({"clock": m.group(1), **kv})
    if rows:
        with open(os.path.join(out, f"memory_5min_stats_{cfg}.csv"), "w", newline="") as g:
            w = csv.DictWriter(g, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


# 8. Caption-age distributions (percentiles 1..99 + max) and 10 s windows, from raw CSVs.
def ages(path, slow=None):
    out_ages, ts = [], []
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["event_type"] != "CAPTION_UPDATE" or not r["latency_ms"]:
                continue
            if slow is not None and r["slow"] != slow:
                continue
            out_ages.append(float(r["latency_ms"]))
            ts.append(float(r["receive_ts"]))
    return out_ages, ts


def pct(vals, p):
    vals = sorted(vals)
    k = (len(vals) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


CDF = {
    "t3_after_normal": (f"{D}/t3ba/t3_after_r1.csv", "0"),
    "t3_before_normal": (f"{D}/t3ba/t3_before_r1.csv", "0"),
    "t3_after_slow": (f"{D}/t3ba/t3_after_r1.csv", "1"),
    "t3_before_slow": (f"{D}/t3ba/t3_before_r1.csv", "1"),
    "bandwidth_after": (f"{R}/bandwidth_limited_final_c500_after_run01.csv", None),
    "bandwidth_before": (f"{R}/bandwidth_limited_final_c500_before_run01.csv", None),
    "t5_delay_loss_final": (f"{D}/t5/t5_delay_loss_run01.csv", None),
    "t5_delay_loss_fifo": (f"{D}/t5/t5_delay_loss_fifo_run01.csv", None),
}
cdf = {}
for name, (path, slow) in CDF.items():
    a, _ = ages(path, slow)
    cdf[name] = [round(pct(a, p), 2) for p in range(1, 100)] + [round(max(a), 2)]
with open(os.path.join(out, "caption_age_percentiles.csv"), "w", newline="") as g:
    w = csv.writer(g)
    w.writerow(["percentile"] + list(cdf))
    for i, p in enumerate(list(range(1, 100)) + ["max"]):
        w.writerow([p] + [cdf[n][i] for n in cdf])

with open(os.path.join(out, "bandwidth_windows_10s.csv"), "w", newline="") as g:
    w = csv.writer(g)
    w.writerow(["config", "run", "window_start_s", "messages", "p50_ms", "p95_ms"])
    for cfg in ("before", "after"):
        for run in (1, 2, 3):
            a, ts = ages(f"{R}/bandwidth_limited_final_c500_{cfg}_run0{run}.csv")
            t0 = min(ts)
            buckets = {}
            for age, t in zip(a, ts):
                buckets.setdefault(int((t - t0) // 10) * 10, []).append(age)
            for start in sorted(buckets):
                b = buckets[start]
                w.writerow([cfg, run, start, len(b), round(statistics.median(b), 1), round(pct(b, 95), 1)])
print("bundle written to", out)
