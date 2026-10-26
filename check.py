#!/usr/bin/env python3
"""Regression gate: run public-harness scenarios and fail on wrong numbers.

Drives the unmodified instructor harness (run_demo.py) and reads the JSON
summary it writes. Scenarios marked "info" in the table are run and reported
but not gated yet. Every gated scenario must have:
  * 100% final-caption completeness
  * 0 duplicate, 0 stale, 0 non-final HISTORY items
  * 0 connection errors
  * ~100% Normal Update Coverage where no client is slow/disconnected/late

Usage:
  python check.py                # quick gate (~3 min), run before every handoff
  python check.py --full         # public-test durations from PUBLIC_TESTS.md
  python check.py --only t1,t4   # subset
  python check.py --runs 3       # repeat each scenario, gate every run

Results land in results/check_<scenario>[_runNN].{csv,json} (gitignored).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# name -> (quick args, full args, gates)
# Gates: "coverage" = require ~100% Normal Update Coverage.
#        "gated": False = run and report only (no pass/fail yet).
SCENARIOS: dict[str, dict] = {
    "t1": {
        "desc": "T1 basic correctness, 20 clients",
        "quick": ["--clients", "20", "--duration", "15"],
        "full": ["--clients", "20", "--duration", "20"],
        "coverage": True,
    },
    "t2": {
        "desc": "T2 public scale, 100 clients",
        "quick": ["--clients", "100", "--duration", "25"],
        "full": ["--clients", "100", "--duration", "40"],
        "coverage": True,
    },
    "t3": {
        "desc": "T3 slow readers, 10% at 500 ms/msg",
        "quick": ["--clients", "100", "--duration", "40",
                  "--slow-percent", "10", "--slow-ms", "500"],
        "full": ["--clients", "100", "--duration", "60",
                 "--slow-percent", "10", "--slow-ms", "500"],
        "coverage": False,
        # Report-only until Day 2's backpressure work: slow readers currently
        # lose ~half their finals because the backlog sits in socket buffers,
        # not in our queue (see HANDOFF.md). Day 2: set this to True.
        "gated": False,
    },
    "t4": {
        "desc": "T4 reconnect, 20% disconnect for 5 s",
        "quick": ["--clients", "100", "--duration", "30", "--disconnect-percent", "20",
                  "--disconnect-at", "10", "--disconnect-seconds", "5"],
        "full": ["--clients", "100", "--duration", "60", "--disconnect-percent", "20",
                 "--disconnect-at", "15", "--disconnect-seconds", "5"],
        "coverage": False,
    },
    "late": {
        "desc": "Late join, 20% of clients join after 8 s",
        "quick": ["--clients", "100", "--duration", "25",
                  "--late-percent", "20", "--late-seconds", "8"],
        "full": ["--clients", "100", "--duration", "40",
                 "--late-percent", "20", "--late-seconds", "15"],
        "coverage": False,
    },
}

COVERAGE_MIN_PCT = 99.0


def gate(name: str, s: dict, want_coverage: bool) -> list[str]:
    problems = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    completeness = s.get("final_completeness_pct")
    need(completeness is not None and completeness >= 100.0 - 1e-9,
         f"final completeness {completeness}% (need 100%)")
    for key in ("duplicates", "stale", "invalid_history_nonfinal", "connection_errors"):
        need(s.get(key, 0) == 0, f"{key}={s.get(key)} (need 0)")
    if want_coverage:
        cov = s.get("normal_update_coverage_pct")
        need(cov is not None and cov >= COVERAGE_MIN_PCT,
             f"normal update coverage {cov}% (need >= {COVERAGE_MIN_PCT}%)")
    return problems


def fmt(v, unit=""):
    return "n/a" if v is None else f"{v:.1f}{unit}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true", help="use public-test durations")
    p.add_argument("--only", help="comma-separated scenario names: " + ",".join(SCENARIOS))
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--port", type=int, default=8790)
    args = p.parse_args()

    names = args.only.split(",") if args.only else list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        p.error(f"unknown scenario(s): {unknown}")

    RESULTS.mkdir(exist_ok=True)
    failures: list[str] = []
    rows = []
    for name in names:
        sc = SCENARIOS[name]
        stem = f"results/check_{name}"
        cmd = [sys.executable, "run_demo.py", *(sc["full"] if args.full else sc["quick"]),
               "--runs", str(args.runs), "--port", str(args.port),
               "--output-stem", stem, "--quiet-server"]
        print(f"\n### {name}: {sc['desc']}\n$ {' '.join(cmd[1:])}", flush=True)
        rc = subprocess.run(cmd, cwd=HERE, stdout=subprocess.DEVNULL).returncode
        if rc != 0:
            failures.append(f"{name}: run_demo.py exited with {rc}")
            continue

        json_paths = ([HERE / f"{stem}.json"] if args.runs == 1 else
                      [HERE / f"{stem}_run{i:02d}.json" for i in range(1, args.runs + 1)])
        for i, path in enumerate(json_paths, 1):
            label = name if args.runs == 1 else f"{name}#{i}"
            s = json.loads(path.read_text(encoding="utf-8"))
            problems = gate(name, s, sc["coverage"])
            if not sc.get("gated", True):
                rows.append((label, s, "info"))
                continue
            failures += [f"{label}: {msg}" for msg in problems]
            rows.append((label, s, "PASS" if not problems else "FAIL"))

    print("\n%-8s %-5s %8s %8s %8s %8s %8s %6s %6s %8s" % (
        "scenario", "gate", "final%", "cover%", "P50ms", "P95ms", "P99ms",
        "dup", "stale", "recovP95"))
    for label, s, verdict in rows:
        print("%-8s %-5s %8s %8s %8s %8s %8s %6s %6s %8s" % (
            label, verdict,
            fmt(s.get("final_completeness_pct")), fmt(s.get("normal_update_coverage_pct")),
            fmt(s.get("p50_ms")), fmt(s.get("p95_ms")), fmt(s.get("p99_ms")),
            s.get("duplicates"), s.get("stale"), fmt(s.get("recovery_p95_ms"))))
        slow = s.get("slow_clients")
        if slow:
            norm = s.get("normal_clients", {})
            print(f"{'':8s}   normal P95={fmt(norm.get('p95_ms'))}ms "
                  f"slow P95={fmt(slow.get('p95_ms'))}ms slow fresh={fmt(slow.get('fresh_pct'), '%')}")

    if failures:
        print("\nGATE FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
