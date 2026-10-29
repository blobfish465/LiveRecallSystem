#!/usr/bin/env python3
"""Slow-then-fast convergence: how quickly does a reader that stops being slow get
fresh captions and full delivery back?

T3 shows what happens *while* a client is slow. The Day 2 policy also has to let
go: once the reader catches up, the server must return to full-update delivery
(Normal Update Coverage is graded) without flooding it back into lag. Nothing in
the public harness exercises that transition, so this tool does.

Setup: our server (`solution_server.py --port`), the public source
(`caption_source.py`), and the public client code (`load_clients.simulated_client`,
imported, not modified) for N normal clients plus K "converging" clients. Their
`slow` flag is a SlowUntil object: the harness tests `if slow:` before every
pause, so the client pauses `--slow-ms` per message until the switch time and
reads at full speed from then on. The CSV is the harness's own, and
`analyse_results.analyse_file` gives the public metrics (the converging clients
show up as its "slow" role). On top of that, per converging client, relative to
the switch:
  * backlog at switch: caption age of the first message read after the switch
  * fresh after: seconds until the last non-fresh message (> threshold); every
    message after that is fresh
  * drain: the same, but from the client's first read after the switch. The
    harness pauses *after* each message, so the first read comes up to --slow-ms
    after the switch; "fresh after" and "full delivery after" include that
    leftover pause (same in both configs), "drain" does not
  * full delivery after: seconds until provisionals flow again *for good*: the
    start of the last stretch of provisionals with no gap over GAP_S that lasts
    to the client's last update; "never" if provisionals stopped before the end.
    (A first-provisional rule is fooled by a provisional left over from a brief
    lag exit during the slow phase; seen in the 200 s run.)
  * settled P95: caption age P95 from 10 s after the switch to the end
  * provisional share, last 30 s: provisionals / updates (~67% in full delivery
    with the public source's 3 revisions, 0% while finals-only)
"before" is the plain FIFO (`LIVERECALL_NO_COALESCE=1`), "after" the lag-aware
server; runs are interleaved and the table shows medians.

Usage:
  python convergence_test.py                       # 100 normal + 10 converging, slow for 30 s, 120 s, before + after
  python convergence_test.py --runs 3 --only after
  python convergence_test.py --slow-until 90 --duration 230   # slow longer than RECOVER_S: exercises the relapse backoff

Exit status 1 when a run is invalid (server not ours / wrong mode) or an "after"
run (our server) fails: 100% finals, 0 duplicates / stale / connection errors, and
every converging client back to full delivery before the end. "Never back" is
only judged when the fast phase is longer than RECOVER_MAX_S + 10 s (the longest
wait the policy may impose after a relapse); otherwise it is reported, not failed.
The baseline's problems are printed but do not fail the run: it is the reference,
not the product.
Results: results/convergence<tag>_c<N>_k<K>_s<slow-until>_d<duration>_m<slow-ms>_<config>_runNN.{csv,json,log}
+ ..._summary.json (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import load_clients  # instructor harness, imported unmodified
from analyse_results import analyse_file, percentile
from check import TIMER_DRIFT_WARN, parse_stats, server_listened, timer_drift
from liverecall.channel import RECOVER_MAX_S
from run_demo import default_source_segments, terminate_process, wait_for_port

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CONFIGS = {"before": "plain FIFO (--no-coalesce)", "after": "lag-aware coalescing"}
SETTLE_S = 10.0  # "settled" window starts this long after the switch
GAP_S = 5.0      # full delivery sends ~2 provisionals per 0.75 s segment; none for 5 s means it stopped
TAIL_S = 30.0    # provisional share is measured over the last TAIL_S of the run


class SlowUntil:
    """A `slow` flag for load_clients.simulated_client that turns false at `switch`
    (loop-monotonic time). The harness evaluates `if slow:` before each pause and
    records `int(slow)` once, so the client is logged as the slow role."""

    def __init__(self, switch: float) -> None:
        self.switch = switch

    def __bool__(self) -> bool:
        return time.monotonic() < self.switch

    def __int__(self) -> int:
        return 1


# ------------------------------------------------------------------- analysis

def sustained_from(times: list[float], last_ts: float, gap_s: float = GAP_S) -> float | None:
    """Earliest time in sorted `times` from which every gap, including the one to
    `last_ts`, is at most gap_s; None when the stream stopped before `last_ts`."""
    if not times or last_ts - times[-1] > gap_s:
        return None
    i = len(times) - 1
    while i > 0 and times[i] - times[i - 1] <= gap_s:
        i -= 1
    return times[i]


def convergence_metrics(rows: list[dict], switch_ts: float, end_ts: float,
                        threshold_ms: float) -> dict:
    """Per converging client (role slow=1), relative to the switch (epoch seconds); medians across them."""
    per_client: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("slow") == "1" and r.get("event_type") == "CAPTION_UPDATE":
            per_client.setdefault(r["client_id"], []).append(r)
    results = []
    for client_id, updates in sorted(per_client.items()):
        updates.sort(key=lambda r: float(r["receive_ts"]))
        before = [r for r in updates if float(r["receive_ts"]) < switch_ts]
        after = [r for r in updates if float(r["receive_ts"]) >= switch_ts]
        lat = lambda r: float(r["latency_ms"])  # noqa: E731
        not_fresh = [r for r in after if lat(r) > threshold_ms]
        resumed = sustained_from([float(r["receive_ts"]) for r in after if r.get("final") == "0"],
                                 float(after[-1]["receive_ts"])) if after else None
        settled = [lat(r) for r in after if float(r["receive_ts"]) >= switch_ts + SETTLE_S]
        tail = [r for r in after if float(r["receive_ts"]) >= end_ts - TAIL_S]
        results.append({
            "client_id": client_id,
            "slow_phase_p95_ms": percentile([lat(r) for r in before], 95) if before else None,
            "backlog_at_switch_ms": lat(after[0]) if after else None,
            "fresh_after_s": (float(not_fresh[-1]["receive_ts"]) - switch_ts) if not_fresh else 0.0,
            "drain_s": (float(not_fresh[-1]["receive_ts"]) - float(after[0]["receive_ts"])) if not_fresh else 0.0,
            "full_delivery_after_s": None if resumed is None else max(0.0, resumed - switch_ts),
            "settled_p95_ms": percentile(settled, 95) if settled else None,
            "provisional_share_tail_pct": (100.0 * sum(r.get("final") == "0" for r in tail) / len(tail)) if tail else None,
            "updates_after": len(after),
        })

    def med(key):
        vals = [c[key] for c in results if c[key] is not None]
        return statistics.median(vals) if vals else None

    return {
        "converging_clients": len(results),
        "never_full_delivery": sum(c["full_delivery_after_s"] is None for c in results),
        **{k: med(k) for k in ("slow_phase_p95_ms", "backlog_at_switch_ms", "fresh_after_s", "drain_s",
                               "full_delivery_after_s", "settled_p95_ms", "provisional_share_tail_pct")},
        "clients": results,
    }


def full_delivery_observable(fast_phase_s: float) -> bool:
    """After a relapse the policy may wait up to RECOVER_MAX_S of fast pongs before full
    delivery; a shorter fast phase cannot tell "slow to recover" from "never"."""
    return fast_phase_s > RECOVER_MAX_S + 10.0


def problems_of(summary: dict, judge_full_delivery: bool = True) -> list[str]:
    out = []
    if (summary.get("final_completeness_pct") or 0) < 100.0 - 1e-9:
        out.append(f"final completeness {summary.get('final_completeness_pct')}%")
    for key in ("duplicates", "stale", "invalid_history_nonfinal", "connection_errors"):
        if summary.get(key, 0):
            out.append(f"{key}={summary[key]}")
    never = summary["convergence"]["never_full_delivery"]
    if never and judge_full_delivery:
        out.append(f"{never} converging client(s) never returned to full delivery")
    return out


# -------------------------------------------------------------------- one run

def setting_stem(args) -> str:
    """Every setting that changes the numbers is in the name, so runs never overwrite or mix."""
    return (f"convergence{args.tag}_c{args.clients}_k{args.converging}"
            f"_s{args.slow_until:g}_d{args.duration:g}_m{args.slow_ms:g}")


SETTINGS = ("clients", "converging", "slow_until", "duration", "slow_ms", "fresh_threshold_ms", "source_start_delay")


def merge_previous(previous: dict, args, results: dict) -> dict:
    """--only adds one config to an earlier summary; refuse if that summary was made with other settings."""
    old_args = previous.get("args", {})
    differ = [k for k in SETTINGS if k in old_args and old_args[k] != getattr(args, k)]
    if differ:
        raise SystemExit(f"the existing summary was made with other settings ({', '.join(differ)}); "
                         "use --tag or rerun both configs")
    return {**previous.get("results", {}), **results}


def client_namespace(args, uri: str, output: Path) -> argparse.Namespace:
    """What load_clients.simulated_client reads from its `args` (its own parser's defaults otherwise)."""
    return argparse.Namespace(
        uri=uri, session="L1", clients=args.clients + args.converging, duration=float(args.duration),
        output=str(output), seed=3103, history_seconds=30,
        slow_percent=0.0, slow_ms=float(args.slow_ms),
        disconnect_percent=0.0, disconnect_at=30.0, disconnect_seconds=5.0,
        late_percent=0.0, late_seconds=10.0,
    )


async def run_clients(args, uri: str, csv_path: Path, log_fp) -> tuple[float, float, int]:
    """The harness's clients in-process plus the source as a subprocess. Returns (switch epoch, end epoch, segments)."""
    segments = default_source_segments(args.duration, args.source_start_delay)
    ns = client_namespace(args, uri, csv_path)
    logger = load_clients.CSVLogger(str(csv_path))
    await logger.open()
    ns._global_start = time.monotonic()
    switch_mono = ns._global_start + args.slow_until
    switch_epoch = time.time() + (switch_mono - time.monotonic())
    end_epoch = time.time() + args.duration
    tasks = [
        asyncio.create_task(load_clients.simulated_client(
            i, ns, logger,
            slow=SlowUntil(switch_mono) if i < args.converging else False,
            disconnecting=False, late=False))
        for i in range(args.clients + args.converging)
    ]
    await asyncio.sleep(args.source_start_delay)
    source = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "caption_source.py", "--uri", uri, "--segments", str(segments), "--quiet",
        cwd=HERE, stdout=log_fp, stderr=subprocess.STDOUT)
    rc = await source.wait()
    if rc != 0:
        raise SystemExit(f"caption_source.py failed ({rc}); see the .log")
    await asyncio.gather(*tasks)
    await logger.close()
    return switch_epoch, end_epoch, segments


def run_once(config: str, run_no: int, args) -> dict:
    stem = f"results/{setting_stem(args)}_{config}_run{run_no:02d}"
    csv_path, json_path, log_path = (HERE / f"{stem}.{ext}" for ext in ("csv", "json", "log"))
    uri = f"ws://127.0.0.1:{args.port}"
    env = os.environ.copy()
    env.pop("LIVERECALL_NO_COALESCE", None)  # never inherit the baseline switch into "after"
    if config == "before":
        env["LIVERECALL_NO_COALESCE"] = "1"
    try:
        with socket.create_connection(("127.0.0.1", args.port), timeout=0.25):
            pass
    except OSError:
        pass
    else:
        raise SystemExit(f"port {args.port} is already in use; stop that process or pass --port")

    print(f"\n=== {config} ({CONFIGS[config]}), run {run_no}/{args.runs}: {args.clients} normal + "
          f"{args.converging} converging clients, slow {args.slow_ms:.0f} ms/msg for {args.slow_until:.0f} s, "
          f"{args.duration:.0f} s ===", flush=True)
    server = None
    with log_path.open("w", encoding="utf-8") as log_fp:
        try:
            server = subprocess.Popen([sys.executable, "-u", "solution_server.py", "--port", str(args.port)],
                                      cwd=HERE, stdout=log_fp, stderr=subprocess.STDOUT, env=env)
            wait_for_port(args.port, server)
            switch_epoch, end_epoch, segments = asyncio.run(run_clients(args, uri, csv_path, log_fp))
        finally:
            terminate_process(server, "solution_server.py")
            time.sleep(0.25)

    text = log_path.read_text(encoding="utf-8", errors="replace")
    if not server_listened(text, args.port):
        raise SystemExit(f"the server never listened on {args.port}; see {stem}.log")
    if ("baseline mode: --no-coalesce" in text) != (config == "before"):
        raise SystemExit(f"the server ran in the wrong mode for '{config}'; see {stem}.log")

    summary = analyse_file(csv_path, expected_finals=segments, expected_updates=segments * 3,
                           fresh_threshold_ms=args.fresh_threshold_ms)
    with csv_path.open(newline="", encoding="utf-8") as fp:
        rows = list(csv.DictReader(fp))
    summary["convergence"] = convergence_metrics(rows, switch_epoch, end_epoch, args.fresh_threshold_ms)
    summary["server"] = parse_stats(text)
    summary["config"] = config
    summary["switch_epoch"], summary["end_epoch"] = switch_epoch, end_epoch
    summary["problems"] = problems_of(summary, full_delivery_observable(args.duration - args.slow_until))
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    c = summary["convergence"]
    print(f"finals {fmt(summary.get('final_completeness_pct'))}%  dup {summary.get('duplicates')}  "
          f"stale {summary.get('stale')}  coverage {fmt(summary.get('normal_update_coverage_pct'))}%  |  converging: "
          f"slow-phase P95 {fmt(c['slow_phase_p95_ms'], 0)} ms, backlog at switch {fmt(c['backlog_at_switch_ms'], 0)} ms, "
          f"fresh after {fmt(c['fresh_after_s'], 2)} s (drain {fmt(c['drain_s'], 3)} s), full delivery after {fmt(c['full_delivery_after_s'], 1)} s, "
          f"settled P95 {fmt(c['settled_p95_ms'], 0)} ms, tail provisionals {fmt(c['provisional_share_tail_pct'], 0)}%"
          + (f"  PROBLEMS: {summary['problems']}" if summary["problems"] else ""), flush=True)
    return summary


# ---------------------------------------------------------------------- table

ROWS = [  # (label, key, sub-key or None, digits)
    ("final completeness %", "final_completeness_pct", None, 1),
    ("normal update coverage %", "normal_update_coverage_pct", None, 1),
    ("duplicates", "duplicates", None, 0),
    ("stale", "stale", None, 0),
    ("connection errors", "connection_errors", None, 0),
    ("normal clients P95 ms", "normal_clients", "p95_ms", 1),
    ("converging clients P95 ms, whole run", "slow_clients", "p95_ms", 0),
    ("converging: slow-phase P95 ms", "convergence", "slow_phase_p95_ms", 0),
    ("converging: backlog at switch ms", "convergence", "backlog_at_switch_ms", 0),
    ("converging: fresh after (s)", "convergence", "fresh_after_s", 2),
    ("converging: drain from first read after switch (s)", "convergence", "drain_s", 3),
    ("converging: full delivery after (s)", "convergence", "full_delivery_after_s", 1),
    ("converging: settled P95 ms (switch+10 s .. end)", "convergence", "settled_p95_ms", 0),
    ("converging: provisional share, last 30 s %", "convergence", "provisional_share_tail_pct", 0),
    ("converging: never back to full delivery", "convergence", "never_full_delivery", 0),
    ("server: lag entries / coalesced / hopeless closes", "server", "lag_entries_total", 0),
    ("server: lagging clients, peak", "server", "lagging_max", 0),
]


def median_of(summaries: list[dict], key: str, sub: str | None):
    vals = []
    for s in summaries:
        v = s.get(key)
        if sub is not None:
            v = v.get(sub) if isinstance(v, dict) else None
        if isinstance(v, (int, float)):
            vals.append(v)
    return statistics.median(vals) if vals else None


def fmt(v, digits=1):
    return "n/a" if v is None else f"{v:.{digits}f}"


def print_table(args, results: dict[str, list[dict]]) -> None:
    n = {c: len(results.get(c, [])) for c in CONFIGS}
    print(f"\n| {args.clients} normal + {args.converging} converging clients, slow {args.slow_ms:.0f} ms/msg "
          f"for {args.slow_until:.0f} s of {args.duration:.0f} s; median of "
          f"{' / '.join(str(n[c]) for c in CONFIGS)} runs | before: {CONFIGS['before']} | after: {CONFIGS['after']} |")
    print("|---|---:|---:|")
    for label, key, sub, digits in ROWS:
        cells = []
        for config in CONFIGS:
            runs = results.get(config, [])
            if key == "server" and sub == "lag_entries_total":
                cells.append(" / ".join(fmt(median_of(runs, "server", k), 0)
                                        for k in ("lag_entries_total", "coalesced_total", "hopeless_closes_total")))
            else:
                cells.append(fmt(median_of(runs, key, sub), digits))
        print(f"| {label} | {' | '.join(cells)} |")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--clients", type=int, default=100, help="normal (fast) clients")
    p.add_argument("--converging", type=int, default=10, help="slow-then-fast clients")
    p.add_argument("--slow-ms", type=float, default=500.0, help="pause per message while slow (T3: 500)")
    p.add_argument("--slow-until", type=float, default=30.0, help="seconds after start when they become fast")
    p.add_argument("--duration", type=float, default=120.0,
                   help="run length; > --slow-until + 60 (RECOVER_S) to see recovery, + 130 to judge a relapse")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--only", choices=list(CONFIGS))
    p.add_argument("--fresh-threshold-ms", type=float, default=500.0)
    p.add_argument("--source-start-delay", type=float, default=1.0)
    p.add_argument("--port", type=int, default=8792)
    p.add_argument("--tag", default="", help="suffix for the result file names")
    args = p.parse_args()
    if args.converging < 1:
        p.error("--converging must be >= 1")
    if args.duration <= args.slow_until:
        p.error("--duration must be longer than --slow-until")
    if args.tag and not args.tag.startswith("_"):
        args.tag = "_" + args.tag
    if not full_delivery_observable(args.duration - args.slow_until):
        print(f"note: the fast phase ({args.duration - args.slow_until:.0f} s) is not longer than RECOVER_MAX_S + 10 s "
              f"({RECOVER_MAX_S + 10:.0f} s): after a relapse 'never back to full delivery' is reported, not failed")
    # `kill` (SIGTERM) must still run the finally blocks that stop our server;
    # by default SIGTERM ends Python without them and leaves the port taken.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))

    drift = timer_drift()
    print(f"asyncio timer drift: {drift:+.0%}", flush=True)
    if drift > TIMER_DRIFT_WARN:
        print("WARNING: timers on this machine are unreliable; results will not be meaningful (see HANDOFF.md).")
    RESULTS.mkdir(exist_ok=True)

    configs = [args.only] if args.only else list(CONFIGS)
    results: dict[str, list[dict]] = {c: [] for c in configs}
    failures: list[str] = []
    notes: list[str] = []  # baseline problems: reported, not failing
    for run_no in range(1, args.runs + 1):
        for config in configs:  # interleaved, so drift over time hits both alike
            s = run_once(config, run_no, args)
            results[config].append(s)
            if config == "after":
                failures += [f"{config} run {run_no}: {msg}" for msg in s["problems"]]
            else:
                notes += [f"{config} run {run_no}: {msg}" for msg in s["problems"]]

    summary_path = RESULTS / f"{setting_stem(args)}_summary.json"
    previous = json.loads(summary_path.read_text(encoding="utf-8")) if (args.only and summary_path.exists()) else {}
    merged = merge_previous(previous, args, results)
    summary_path.write_text(json.dumps({"args": vars(args), "timer_drift": drift, "results": merged}, indent=2),
                            encoding="utf-8")
    print_table(args, merged)
    print(f"\nwritten: {summary_path.relative_to(HERE)}")
    if notes:
        print("\nbaseline (plain FIFO) problems, for the report:")
        for n in notes:
            print(" -", n)
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nOK: every 'after' run complete (100% finals, 0 dup/stale/errors) and every converging client back to full delivery")
    return 0


if __name__ == "__main__":
    sys.exit(main())
