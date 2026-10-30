#!/usr/bin/env python3
"""Regression gate: run public-harness scenarios and fail on wrong numbers.

Drives the unmodified instructor harness (run_demo.py) and reads the JSON
summary it writes. Scenarios marked "info" in the table are run and reported
but not gated yet. Every gated scenario must have:
  * 100% final-caption completeness
  * 0 duplicate, 0 stale, 0 non-final HISTORY items
  * 0 connection errors
  * ~100% Normal Update Coverage where no client is slow/disconnected/late
  * P95 Caption Age limits for normal / slow clients, where a scenario sets them
  * a server RSS ceiling, where a scenario sets one (boundedness at scale)
  * our server really listening on the port (a stale server would be measured instead)

Usage:
  python check.py                # quick gate (~3 min), run before every handoff
  python check.py --full         # public-test durations from PUBLIC_TESTS.md
  python check.py --only t1,t4   # subset
  python check.py --runs 3       # repeat each scenario, gate every run
  python check.py --stress       # scale scenarios (300/500 clients, ~2 min), not in the default gate

Results land in results/check_<scenario>[_runNN].{csv,json} and
results/check_<scenario>.log (run_demo.py's and the server's output), gitignored.
The table also shows what the harness cannot see: the server's peak RSS and its
[stats] peaks (lagging clients, max_pending, coalesced provisionals, hopeless closes).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# name -> (quick args, full args, gates)
# Gates: "coverage" = require ~100% Normal Update Coverage.
#        "gated": False = run and report only (no pass/fail yet).
#        "normal_p95_max_ms" / "slow_p95_max_ms" = P95 Caption Age limits per role.
#        "rss_max_mb" = ceiling on the server's peak RSS during the run.
#        "stress": True = only with --stress (or --only); too slow for the default gate.
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
        # Measured (WSL2, Day 2): normal P95 ~10 ms, slow P95 ~9 s; the
        # --no-coalesce baseline has slow P95 ~28 s and 95% finals. The slow
        # floor is the client library's 16-message read-ahead (see HANDOFF.md).
        "normal_p95_max_ms": 100.0,
        "slow_p95_max_ms": 15000.0,
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
    # --stress (Day 3): scale beyond the public tests. One load_clients.py
    # process drives all clients, so 500 is about what one core can read at
    # full fidelity; the harness, not the server, is the limit beyond that.
    # Limits are ~3x what the Mininet VM (4 vCPU) measured (P95 31/42/40 ms,
    # RSS 45/59 MB, median of 3); see HANDOFF.md.
    "s300": {
        "desc": "stress: 300 clients",
        "quick": ["--clients", "300", "--duration", "40"],
        "full": ["--clients", "300", "--duration", "60"],
        "coverage": True,
        "stress": True,
        "normal_p95_max_ms": 100.0,
        "rss_max_mb": 200.0,
    },
    "s500": {
        "desc": "stress: 500 clients",
        "quick": ["--clients", "500", "--duration", "40"],
        "full": ["--clients", "500", "--duration", "60"],
        "coverage": True,
        "stress": True,
        "normal_p95_max_ms": 150.0,
        "rss_max_mb": 200.0,
    },
    "s500slow": {
        "desc": "stress: 500 clients, 10% slow at 500 ms/msg",
        "quick": ["--clients", "500", "--duration", "40",
                  "--slow-percent", "10", "--slow-ms", "500"],
        "full": ["--clients", "500", "--duration", "60",
                 "--slow-percent", "10", "--slow-ms", "500"],
        "coverage": False,
        "stress": True,
        "normal_p95_max_ms": 150.0,
        "slow_p95_max_ms": 15000.0,
        "rss_max_mb": 200.0,
    },
}

COVERAGE_MIN_PCT = 99.0
TIMER_DRIFT_WARN = 0.10
RSS_SAMPLE_S = 0.5


def timer_drift() -> float:
    """Fraction by which asyncio sleeps overrun on this machine.

    The source, load clients and server are all paced by asyncio timers. On
    VirtualBox running on top of Hyper-V ("NEM" mode, e.g. Windows with WSL2 or
    Docker installed) timers fire 13-200% late, the source falls behind the
    clients' fixed test window, and every metric becomes meaningless.
    """
    import asyncio
    import time

    async def measure() -> float:
        start = time.monotonic()
        for _ in range(10):
            await asyncio.sleep(0.1)
        return time.monotonic() - start

    return asyncio.run(measure()) / 1.0 - 1.0


def scenario_names(only: str | None, stress: bool) -> list[str]:
    """--only wins; otherwise the default gate or, with --stress, the stress scenarios."""
    if only:
        return only.split(",")
    return [n for n, sc in SCENARIOS.items() if bool(sc.get("stress")) == stress]


# ---------------------------------------------------------------- server evidence
# Also used by bandwidth_test.py. The harness measures the clients' view only;
# these read the server side: its process (RSS) and its log ([stats] lines).

def server_pid(port: int) -> int | None:
    """PID of the solution_server.py that run_demo.py started on `port` (same idea as monitor_server.py).

    None where there is no /proc (Windows, macOS): the RSS column then shows n/a
    and only the stress scenarios, which gate on RSS, fail, with a message saying why.
    """
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"solution_server.py" in cmd and b"--port" in cmd and str(port).encode() in cmd:
            return int(entry.name)
    return None


def rss_mb(pid: int) -> float | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return None


class RssSampler(threading.Thread):
    """Peak RSS of every solution_server.py seen on `port` while run_demo.py runs.

    run_demo.py starts a fresh server per run, so peaks are kept per PID in
    order of appearance: stop()[i] belongs to run i+1.
    """

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.peaks: dict[int, float] = {}  # pid -> max RSS MB, insertion order = run order
        self._done = threading.Event()  # not _stop: Thread has a private _stop() that join() calls

    def run(self) -> None:
        while not self._done.is_set():
            pid = server_pid(self.port)
            if pid is not None:
                value = rss_mb(pid)
                if value is not None:
                    self.peaks[pid] = max(self.peaks.get(pid, 0.0), value)
            self._done.wait(RSS_SAMPLE_S)

    def stop(self) -> list[float]:
        self._done.set()
        self.join()
        return list(self.peaks.values())


def split_runs(text: str) -> list[str]:
    """run_demo.py's output cut at its '=== Run i/n ===' headers: one piece per run."""
    return re.split(r"^=== Run \d+/\d+ ===$", text, flags=re.MULTILINE)[1:]


def server_listened(text: str, port: int) -> bool:
    """False when something else held the port: run_demo.py would then measure that process, not ours."""
    return f"LiveRecall server listening on ws://0.0.0.0:{port}" in text


def parse_stats(text: str) -> dict[str, int]:
    """Server [stats] lines (one every 10 s): peaks of lagging/max_pending, final values of the totals."""
    out = {"stats_lines": 0, "lagging_max": 0, "max_pending_max": 0,
           "coalesced_total": 0, "lag_entries_total": 0, "link_lag_entries_total": 0,
           "hopeless_closes_total": 0}
    overflows = 0
    # The harness, the source, the clients and the server all print into the same
    # log, and print() writes the text and the newline separately, so another
    # process's text can land in front of a server line (seen in a real log:
    # "source disconnectedSOURCE_DONE: ..."). Hence find(), not startswith().
    for line in text.splitlines():
        if "client overflow, closing:" in line:
            overflows += 1  # every hopeless close logs this line; the totals lag by up to 10 s
        start = line.find("[stats")
        if start < 0:
            continue
        line = line[start:]
        out["stats_lines"] += 1
        kv = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line)}
        out["lagging_max"] = max(out["lagging_max"], kv.get("lagging", 0))
        out["max_pending_max"] = max(out["max_pending_max"], kv.get("max_pending", 0))
        for key in ("coalesced_total", "lag_entries_total", "link_lag_entries_total", "hopeless_closes_total"):
            out[key] = kv.get(key, out[key])
    out["hopeless_closes_total"] = max(out["hopeless_closes_total"], overflows)
    return out


# ------------------------------------------------------------------------- gate

def gate(name: str, s: dict, sc: dict) -> list[str]:
    problems = []

    def need(cond: bool, msg: str) -> None:
        if not cond:
            problems.append(msg)

    completeness = s.get("final_completeness_pct")
    need(completeness is not None and completeness >= 100.0 - 1e-9,
         f"final completeness {completeness}% (need 100%)")
    for key in ("duplicates", "stale", "invalid_history_nonfinal", "connection_errors"):
        need(s.get(key, 0) == 0, f"{key}={s.get(key)} (need 0)")
    if sc["coverage"]:
        cov = s.get("normal_update_coverage_pct")
        need(cov is not None and cov >= COVERAGE_MIN_PCT,
             f"normal update coverage {cov}% (need >= {COVERAGE_MIN_PCT}%)")
    for role, key in (("normal", "normal_p95_max_ms"), ("slow", "slow_p95_max_ms")):
        if key in sc:
            p95 = s.get(f"{role}_clients", {}).get("p95_ms")
            need(p95 is not None and p95 <= sc[key],
                 f"{role} P95 {p95} ms (need <= {sc[key]:.0f} ms)")
    server = s.get("server", {})
    need(server.get("listened", True), "our server never listened on the port (stale server?)")
    if "rss_max_mb" in sc:
        rss = server.get("rss_max_mb")
        if rss is None:
            problems.append("server RSS not measured (needs Linux /proc; run the stress gate on Ubuntu)")
        else:
            need(rss <= sc["rss_max_mb"], f"server RSS max {fmt(rss)} MB (need <= {sc['rss_max_mb']:.0f} MB)")
    return problems


def fmt(v, unit=""):
    return "n/a" if v is None else f"{v:.1f}{unit}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--full", action="store_true", help="use public-test durations")
    p.add_argument("--only", help="comma-separated scenario names: " + ",".join(SCENARIOS))
    p.add_argument("--stress", action="store_true", help="run the 300/500-client scenarios instead")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--port", type=int, default=8790)
    args = p.parse_args()

    names = scenario_names(args.only, args.stress)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        p.error(f"unknown scenario(s): {unknown}")

    drift = timer_drift()
    print(f"asyncio timer drift: {drift:+.0%}", flush=True)
    if drift > TIMER_DRIFT_WARN:
        print("WARNING: timers on this machine are unreliable (e.g. VirtualBox on "
              "Hyper-V). Results below will not be meaningful; see HANDOFF.md.",
              flush=True)

    RESULTS.mkdir(exist_ok=True)
    failures: list[str] = []
    rows = []
    for name in names:
        sc = SCENARIOS[name]
        stem = f"results/check_{name}"
        cmd = [sys.executable, "-u", "run_demo.py", *(sc["full"] if args.full else sc["quick"]),
               "--runs", str(args.runs), "--port", str(args.port), "--output-stem", stem]
        print(f"\n### {name}: {sc['desc']}\n$ {' '.join(cmd[2:])}", flush=True)
        # The server's output goes through run_demo.py's stdout into the log
        # (no --quiet-server), so the [stats] lines can be read back per run.
        log_path = HERE / f"{stem}.log"
        sampler = RssSampler(args.port)
        sampler.start()
        try:
            with log_path.open("w", encoding="utf-8") as log:
                rc = subprocess.run(cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT).returncode
        finally:
            peaks = sampler.stop()
        if rc != 0:
            failures.append(f"{name}: run_demo.py exited with {rc}; see {stem}.log")
            continue
        pieces = split_runs(log_path.read_text(encoding="utf-8", errors="replace"))

        json_paths = ([HERE / f"{stem}.json"] if args.runs == 1 else
                      [HERE / f"{stem}_run{i:02d}.json" for i in range(1, args.runs + 1)])
        for i, path in enumerate(json_paths, 1):
            label = name if args.runs == 1 else f"{name}#{i}"
            s = json.loads(path.read_text(encoding="utf-8"))
            piece = pieces[i - 1] if i <= len(pieces) else ""
            s["server"] = {**parse_stats(piece),
                           "listened": server_listened(piece, args.port),
                           "rss_max_mb": peaks[i - 1] if i <= len(peaks) else None}
            problems = gate(name, s, sc)
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
        srv = s.get("server", {})
        print(f"{'':8s}   server: RSS max {fmt(srv.get('rss_max_mb'))} MB, "
              f"lagging peak {srv.get('lagging_max')}, max_pending {srv.get('max_pending_max')}, "
              f"coalesced {srv.get('coalesced_total')}, hopeless closes {srv.get('hopeless_closes_total')}")

    if failures:
        print("\nGATE FAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
