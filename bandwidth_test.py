#!/usr/bin/env python3
"""Constrained-bandwidth experiment (advanced challenge 2): before/after on a rate-limited loopback.

The public profile (network_profiles/bandwidth_2mbit.sh: tbf 2 Mbit/s on lo) does not constrain
100 clients (Day 1 measured ~0.6 Mbit/s). This script drives the *unmodified* harness
(run_demo.py -> load_clients.py / caption_source.py / analyse_results.py) with enough clients to
saturate the link, once with the plain-FIFO baseline (LIVERECALL_NO_COALESCE=1, "before") and
once with the real server ("after"), interleaved so both see the same machine conditions, and
reports both side by side together with what the link and the server actually did:
  * link usage: bytes through the interface (/proc/net/dev, both directions, all flows) -> Mbit/s;
  * tbf's own counters (tc -s qdisc show): packets dropped / over limit during the run;
  * the server's [stats] lines: lagging clients, coalesced provisionals, hopeless closes, max_pending;
  * the server's RSS over the run (memory stays bounded).

Usage (tc needs root; the measurement itself does not):
  python bandwidth_test.py --apply --clients 500 --duration 60 --runs 3     # shape, run, reset
  python bandwidth_test.py --unlimited --clients 500 --duration 30          # the demand with no limit
  python bandwidth_test.py --clients 500 --duration 60   # a tbf you applied yourself (reset it afterwards!)

--apply shapes ONLY the test port, with the public profile's parameters (rate 2mbit burst 32kbit
latency 400ms; 32kbit = 4000 bytes):
  tc qdisc add dev lo root handle 1: prio bands 3 priomap 2 2 2 2 2 2 2 2 2 2 2 2 2 2 2 2
  tc qdisc add dev lo parent 1:1 handle 10: tbf rate 2mbit burst 4000 latency 400ms
  tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip dport <port> 0xffff flowid 1:1
  tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip sport <port> 0xffff flowid 1:1
The one difference from network_profiles/bandwidth_2mbit.sh (a root tbf on all of lo) is the port
filter: an SSH/VS Code tunnel or DNS on the same machine keep working while the experiment
saturates the link. Caveat for loopback: sch_tbf drops every packet larger than its burst, and
lo's MTU is 65536, so a single message over 4000 bytes (a bulk transfer, a big uncompressed
HISTORY) stalls its connection for ever (measured: a 2 MB transfer stalls at 0 B; with --burst
66536 it runs at 1.84 Mbit/s). The caption stream is unaffected: per-connection backlogs stay a
few hundred bytes, so segments stay small. --burst 66536 gives the MTU-safe variant.

Results: results/bandwidth_<limited|unlimited><tag>_c<clients>_<before|after>_runNN.{csv,json,log} and
results/bandwidth_<mode><tag>_c<clients>_summary.json (gitignored). Copy the table into HANDOFF.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from check import TIMER_DRIFT_WARN, timer_drift

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
RESET = HERE / "network_profiles" / "reset.sh"
RATE, LATENCY, PROFILE_BURST = "2mbit", "400ms", 4000  # the public profile's parameters (32kbit = 4000 B)
CONFIGS = {"before": "plain FIFO (--no-coalesce)", "after": "lag-aware coalescing"}
SAMPLE_S = 0.5


# ------------------------------------------------------------------------------ shaping

def tc(*args: str) -> None:
    cmd = ["sudo", "tc", *args]
    print("$ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def apply_shaping(iface: str, port: int, burst: int) -> None:
    """The public profile's tbf, restricted to the test port (see the module docstring)."""
    tc("qdisc", "add", "dev", iface, "root", "handle", "1:", "prio", "bands", "3",
       "priomap", *(["2"] * 16))  # unmatched traffic -> band 1:3, unshaped
    tc("qdisc", "add", "dev", iface, "parent", "1:1", "handle", "10:", "tbf",
       "rate", RATE, "burst", str(burst), "latency", LATENCY)
    for direction in ("dport", "sport"):  # client -> server and server -> client
        tc("filter", "add", "dev", iface, "parent", "1:", "protocol", "ip", "prio", "1",
           "u32", "match", "ip", direction, str(port), "0xffff", "flowid", "1:1")


# ------------------------------------------------------------------ link + server probes

def qdisc(iface: str) -> str:
    """The tbf line of `tc qdisc show` (e.g. 'qdisc tbf 10: parent 1:1 rate 2Mbit burst 66Kb lat 400ms'),
    or the root line when no tbf is applied."""
    out = subprocess.run(["tc", "qdisc", "show", "dev", iface], capture_output=True, text=True)
    lines = out.stdout.strip().splitlines() or ["(none)"]
    return next((l.strip() for l in lines if l.startswith("qdisc tbf")), lines[0].strip())


def parse_tbf_stats(text: str) -> dict[str, int] | None:
    """The tbf block of `tc -s qdisc show`: what the shaper sent, dropped and found over its limit."""
    if "qdisc tbf" not in text:
        return None
    block = text[text.index("qdisc tbf"):]  # the stats lines follow their qdisc line
    m = re.search(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+)", block)
    if not m:
        return None
    return {"bytes": int(m[1]), "pkts": int(m[2]), "dropped": int(m[3]), "overlimits": int(m[4])}


def tbf_counters(iface: str) -> dict[str, int] | None:
    out = subprocess.run(["tc", "-s", "qdisc", "show", "dev", iface], capture_output=True, text=True)
    return parse_tbf_stats(out.stdout)


def iface_tx_bytes(iface: str) -> int:
    for line in Path("/proc/net/dev").read_text().splitlines():
        name, _, rest = line.partition(":")
        if name.strip() == iface:
            return int(rest.split()[8])  # tx bytes: what the qdisc actually carried
    raise SystemExit(f"interface {iface} not in /proc/net/dev")


def server_pid(port: int) -> int | None:
    """PID of the solution_server.py that run_demo.py started on `port` (same idea as monitor_server.py)."""
    for entry in Path("/proc").iterdir():
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


class Sampler(threading.Thread):
    """Every SAMPLE_S: bytes through the interface and the server's RSS, until stop() is called."""

    def __init__(self, iface: str, port: int) -> None:
        super().__init__(daemon=True)
        self.iface, self.port = iface, port
        self.samples: list[tuple[float, int]] = []  # (monotonic time, tx bytes)
        self.rss: list[float] = []
        self.error: BaseException | None = None  # a thread's exception is otherwise lost
        self._done = threading.Event()  # not _stop: Thread has a private _stop() that join() calls

    def run(self) -> None:
        try:
            pid = None
            while not self._done.is_set():
                self.samples.append((time.monotonic(), iface_tx_bytes(self.iface)))
                pid = pid or server_pid(self.port)
                if pid is not None:
                    value = rss_mb(pid)
                    if value is not None:
                        self.rss.append(value)
                self._done.wait(SAMPLE_S)
        except BaseException as exc:  # noqa: BLE001 - re-raised by stop()
            self.error = exc

    def stop(self) -> dict:
        self._done.set()
        self.join()
        if self.error is not None:
            raise self.error
        return {**link_rates(self.samples),
                "server_rss_mb_max": max(self.rss) if self.rss else None,
                "server_rss_mb_min": min(self.rss) if self.rss else None}


def link_rates(samples: list[tuple[float, int]], window_s: float = 5.0) -> dict[str, float]:
    """Mbit/s from (time, byte counter) samples: the run average and the peak over any `window_s`
    (the sustained offered load, which a 0.5 s sample would overstate because tbf lets bursts through)."""
    if len(samples) < 2:
        return {}
    (t0, b0), (t1, b1) = samples[0], samples[-1]
    peak, j = 0.0, 0
    for i, (tb, bb) in enumerate(samples):
        while j + 1 < i and tb - samples[j + 1][0] >= window_s:
            j += 1  # j: the latest sample at least window_s before sample i
        ta, ba = samples[j]
        if tb - ta >= window_s:
            peak = max(peak, (bb - ba) * 8 / (tb - ta) / 1e6)
    return {"link_mbit_avg": (b1 - b0) * 8 / (t1 - t0) / 1e6, "link_mbit_peak5s": peak}


def parse_stats(log: Path) -> dict[str, int]:
    """Server [stats] lines (one every 10 s): peaks of lagging/max_pending, final values of the totals."""
    out = {"stats_lines": 0, "lagging_max": 0, "max_pending_max": 0,
           "coalesced_total": 0, "lag_entries_total": 0, "hopeless_closes_total": 0}
    overflows = 0
    for line in log.read_text(errors="replace").splitlines():
        if line.startswith("client overflow, closing:"):
            overflows += 1  # every hopeless close logs this line; the totals lag by up to 10 s
        if not line.startswith("[stats"):
            continue
        out["stats_lines"] += 1
        kv = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line)}
        out["lagging_max"] = max(out["lagging_max"], kv.get("lagging", 0))
        out["max_pending_max"] = max(out["max_pending_max"], kv.get("max_pending", 0))
        for key in ("coalesced_total", "lag_entries_total", "hopeless_closes_total"):
            out[key] = kv.get(key, out[key])
    out["hopeless_closes_total"] = max(out["hopeless_closes_total"], overflows)
    return out


def check_run_log(text: str, config: str, port: int) -> None:
    """The run is only valid if *our* server bound the port and ran in the requested mode."""
    if f"LiveRecall server listening on ws://0.0.0.0:{port}" not in text:
        raise SystemExit(f"the server never listened on {port} (port busy?); see the .log")
    if ("baseline mode: --no-coalesce" in text) != (config == "before"):
        raise SystemExit(f"the server ran in the wrong mode for '{config}' (LIVERECALL_NO_COALESCE exported?)")


# ----------------------------------------------------------------------------- one run

def run_once(mode: str, config: str, run_no: int, args) -> dict:
    stem = f"results/bandwidth_{mode}{args.tag}_c{args.clients}_{config}_run{run_no:02d}"
    log = HERE / f"{stem}.log"
    env = dict(os.environ)
    env.pop("LIVERECALL_NO_COALESCE", None)  # an exported baseline switch must not leak into "after"
    if config == "before":
        env["LIVERECALL_NO_COALESCE"] = "1"  # run_demo.py starts the server with --port only
    cmd = [sys.executable, "-u", "run_demo.py", "--clients", str(args.clients),
           "--duration", str(args.duration), "--runs", "1", "--port", str(args.port),
           "--output-stem", stem]
    print(f"\n### {mode} / {config} ({CONFIGS[config]}), run {run_no}\n$ "
          + ("LIVERECALL_NO_COALESCE=1 " if config == "before" else "") + " ".join(cmd[2:]), flush=True)

    try:  # a stale server on the port would be measured instead of ours (run_demo.py only waits for *a* listener)
        with socket.create_connection(("127.0.0.1", args.port), timeout=0.25):
            raise SystemExit(f"port {args.port} is already in use; stop that process or pass --port")
    except OSError:
        pass
    before = tbf_counters(args.iface)
    sampler = Sampler(args.iface, args.port)
    sampler.start()
    with log.open("w", encoding="utf-8") as fp:  # run_demo's and the server's stdout, for the [stats] lines
        rc = subprocess.run(cmd, cwd=HERE, env=env, stdout=fp, stderr=subprocess.STDOUT).returncode
    link = sampler.stop()
    after = tbf_counters(args.iface)
    if rc != 0:
        raise SystemExit(f"run_demo.py exited with {rc}; see {log}")
    check_run_log(log.read_text(errors="replace"), config, args.port)

    summary = json.loads((HERE / f"{stem}.json").read_text(encoding="utf-8"))
    summary.update(link)
    summary["server"] = parse_stats(log)
    summary["qdisc"] = qdisc(args.iface)
    if before and after:
        summary["tbf_dropped"] = after["dropped"] - before["dropped"]
        summary["tbf_overlimits"] = after["overlimits"] - before["overlimits"]
    return summary


# ------------------------------------------------------------------------------ report

def median_of(summaries: list[dict], key: str, sub: str | None = None):
    vals = []
    for s in summaries:
        v = s.get(key) if sub is None else s.get(key, {}).get(sub)
        if v is not None:
            vals.append(float(v))
    return statistics.median(vals) if vals else None


def fmt(v, digits=1):
    return "n/a" if v is None else f"{v:.{digits}f}"


ROWS = [  # (label, key, sub-key or None, digits)
    ("final completeness %", "final_completeness_pct", None, 1),
    ("normal update coverage %", "normal_update_coverage_pct", None, 1),
    ("P50 caption age ms", "p50_ms", None, 0),
    ("P95 caption age ms", "p95_ms", None, 0),
    ("P99 caption age ms", "p99_ms", None, 0),
    ("fresh captions % (<=500 ms)", "fresh_pct", None, 1),
    ("fresh finals % (<=500 ms)", "fresh_final_pct", None, 1),
    ("duplicates", "duplicates", None, 0),
    ("stale", "stale", None, 0),
    ("connection errors", "connection_errors", None, 0),
    ("link Mbit/s, run average", "link_mbit_avg", None, 2),
    ("link Mbit/s, peak 5 s", "link_mbit_peak5s", None, 2),
    ("tbf packets dropped", "tbf_dropped", None, 0),
    ("lagging clients, peak", "server", "lagging_max", 0),
    ("provisionals coalesced", "server", "coalesced_total", 0),
    ("hopeless closes", "server", "hopeless_closes_total", 0),
    ("max_pending, peak", "server", "max_pending_max", 0),
    ("server RSS MB, max", "server_rss_mb_max", None, 1),
]


def print_table(mode: str, args, results: dict[str, list[dict]]) -> None:
    configs = [c for c in CONFIGS if results.get(c)]
    n = max(len(results[c]) for c in configs)
    print(f"\n=== {mode}: {args.clients} clients, {args.duration:.0f} s, "
          f"{'median of %d runs' % n if n > 1 else '1 run'}, qdisc: {qdisc(args.iface)} ===")
    print("| metric | " + " | ".join(f"{c} ({CONFIGS[c]})" for c in configs) + " |")
    print("|---|" + "---:|" * len(configs))
    for label, key, sub, digits in ROWS:
        cells = [fmt(median_of(results[c], key, sub), digits) for c in configs]
        print(f"| {label} | " + " | ".join(cells) + " |")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--clients", type=int, default=500, help="500 puts both configurations over 2 Mbit/s; 100 never does")
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--runs", type=int, default=1, help="runs per configuration (report the median of 3)")
    p.add_argument("--only", choices=list(CONFIGS), help="run only this configuration")
    p.add_argument("--unlimited", action="store_true", help="no rate limit: measure the demand")
    p.add_argument("--apply", action="store_true",
                   help="shape the test port first (sudo tc, see the docstring) and reset afterwards")
    p.add_argument("--burst", type=int, default=PROFILE_BURST,
                   help="tbf burst in bytes (default: the public profile's 4000; 66536 is MTU-safe on lo)")
    p.add_argument("--tag", default="", help="suffix for the result file names, e.g. _burst66536")
    p.add_argument("--iface", default="lo")
    p.add_argument("--port", type=int, default=8791)
    args = p.parse_args()
    iface_tx_bytes(args.iface)  # fail fast on a typo

    drift = timer_drift()
    print(f"asyncio timer drift: {drift:+.0%}", flush=True)
    if drift > TIMER_DRIFT_WARN:
        print("WARNING: timers on this machine are unreliable; see HANDOFF.md 'Test environments'.")

    mode = "unlimited" if args.unlimited else "limited"
    applied = False
    try:
        if args.apply and not args.unlimited:
            if "tbf" in qdisc(args.iface):
                raise SystemExit(f"a tbf is already applied; run: sudo {RESET.relative_to(HERE)} {args.iface}")
            applied = True  # set before the tc calls so a half-applied setup is still reset
            apply_shaping(args.iface, args.port, args.burst)
        current = qdisc(args.iface)
        print(f"qdisc on {args.iface}: {current}")
        if args.unlimited and "tbf" in current:
            raise SystemExit(f"a rate limit is still applied; run: sudo {RESET.relative_to(HERE)} {args.iface}")
        if not args.unlimited and "tbf" not in current:
            raise SystemExit(f"no rate limit on {args.iface}: pass --apply (or apply a tbf yourself)")

        RESULTS.mkdir(exist_ok=True)
        results: dict[str, list[dict]] = {c: [] for c in CONFIGS}
        for run_no in range(1, args.runs + 1):  # interleaved: before, after, before, after, ...
            for config in CONFIGS:
                if args.only and config != args.only:
                    continue
                results[config].append(run_once(mode, config, run_no, args))
        print_table(mode, args, results)
        out = RESULTS / f"bandwidth_{mode}{args.tag}_c{args.clients}_summary.json"
        if args.only and out.exists():  # keep the other configuration's runs from the previous summary
            previous = json.loads(out.read_text(encoding="utf-8")).get("runs", {})
            results = {c: results[c] or previous.get(c, []) for c in CONFIGS}
        out.write_text(json.dumps({"clients": args.clients, "duration": args.duration,
                                   "timer_drift": drift, "runs": results}, indent=2), encoding="utf-8")
        print(f"\nwritten: {out.relative_to(HERE)}")
    finally:
        if applied:
            subprocess.run([str(RESET), args.iface], cwd=HERE)  # the public reset script (deletes the root qdisc)
        elif not args.unlimited:
            print(f"reminder: sudo {RESET.relative_to(HERE)} {args.iface}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
