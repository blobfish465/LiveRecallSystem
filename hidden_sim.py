#!/usr/bin/env python3
"""Hidden-evaluation simulator: more settings for the harder hidden marks.

The public tests run each hard challenge at one setting (T3: 10% readers at
500 ms; bandwidth_test.py: 500 clients on 2 Mbit/s; T4: 20% drop for 5 s). The
hidden evaluator "may vary and combine" client count, delay/jitter/loss, slow
reader share and rate, disconnects and simultaneous reconnects, caption rate and
burstiness, and bandwidth. This tool runs a matrix of such variations for the
three harder components:

  adv1  slow-reader semantic backpressure      (6 marks)
  adv2  constrained-bandwidth semantic delivery (6 marks)
  recon reconnect and catch-up recovery         (4 marks)

Every scenario stays inside the teaching team's stated bounds (2 Nov): no reader
is permanently slower than the finals stream alone (~1.33 finals/s at the public
pace) and no link is narrower than what finals alone need.

Setup, as in convergence_test.py: our server as a subprocess, the public source
(`caption_source.py`, with its own --revision-delay/--segment-gap flags for
faster captions) as a subprocess, and the public client code
(`load_clients.simulated_client`, imported unmodified) with roles assigned per
client. Metrics come from `analyse_results.analyse_file`. Network impairment is
applied with tc to the test port only (prio -> [netem] -> [tbf], like
bandwidth_test.py --apply), optionally only for part of the run, and is always
removed afterwards.

"before" is the plain FIFO (LIVERECALL_NO_COALESCE=1), "after" our server,
interleaved. A scenario's "after" run fails when finals < 100% or any
duplicate/stale/non-final HISTORY item appears. Connection errors are reported,
not failed (hopeless closes reconnect and recover through HISTORY by design).

Usage (scenarios with a network shape need root for tc; run as root or with
passwordless `sudo tc`):
  python hidden_sim.py --list
  python hidden_sim.py                         # whole matrix, before + after (~30 min)
  python hidden_sim.py --only adv1             # one component (or scenario names, comma-separated)
  python hidden_sim.py --only mass_all --configs after --runs 3
Results: results/hidden<tag>_<scenario>_<config>_runNN.{csv,json,log} and
results/hidden<tag>_summary.json (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import load_clients  # instructor harness, imported unmodified
from analyse_results import analyse_file, percentile
from check import TIMER_DRIFT_WARN, parse_stats, rss_mb, server_listened, timer_drift
from run_demo import terminate_process, wait_for_port

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CONFIGS = {"before": "plain FIFO (--no-coalesce)", "after": "lag-aware (our server)"}
SOURCE_START_S = 1.0  # as run_demo.py: clients connect first
TAIL_PAD_S = 2.0      # as run_demo.py: source stops ~2 s before the clients
TAIL_S = 30.0         # "after impairment" window: the last 30 s of the run
# The evaluator allows "a short bounded grace period for in-flight/recovery traffic"
# before judging completeness (teaching team, 2 Nov). Our clients stop at a fixed time,
# and on a shaped link the source itself can start a few seconds late (its handshake
# crosses the same saturated link), so its last finals may be sent just as the clients
# stop. "Finals due" excuses only finals the source sent in the last GRACE_S; anything
# sent earlier and not delivered still counts as lost.
GRACE_S = 3.0
PUBLIC_REVISION_DELAY, PUBLIC_SEGMENT_GAP = 0.20, 0.35


@dataclass(frozen=True)
class Shape:
    """Impairment on the test port. Any of rate / delay / loss; tbf burst as the public profile."""
    rate: str | None = None    # e.g. "2mbit"
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    burst: int = 4000          # 32kbit, the public profile's burst
    latency: str = "400ms"


@dataclass(frozen=True)
class Scenario:
    name: str
    component: str             # adv1 | adv2 | recon
    desc: str
    clients: int = 100
    duration: float = 60.0
    slow_pct: float = 0.0
    slow_ms: float = 500.0
    disc_pct: float = 0.0
    disc_at: float = 15.0
    disc_s: float = 5.0
    late_pct: float = 0.0
    late_at: float = 10.0
    history_seconds: int = 30
    revision_delay: float = PUBLIC_REVISION_DELAY
    segment_gap: float = PUBLIC_SEGMENT_GAP
    shape: Shape | None = None
    shape_until: float | None = None  # seconds after the clients start; None = the whole run


SCENARIOS: list[Scenario] = [
    # ---- adv1: slow readers (public T3 is 10% at 500 ms) --------------------------------
    Scenario("slow30", "adv1", "30% of clients slow at 500 ms/msg", slow_pct=30),
    Scenario("slow250", "adv1", "10% slow at 250 ms/msg (~the full stream rate)", slow_ms=250, slow_pct=10),
    Scenario("slow700", "adv1", "10% slow at 700 ms/msg (1.43 msg/s, just above the finals rate)",
             slow_ms=700, slow_pct=10),
    Scenario("burst3x_slow", "adv1",
             "source 3x faster (12 msg/s, 4 finals/s), 10% readers at 150 ms (6.7 msg/s: finals yes, all no)",
             revision_delay=0.0667, segment_gap=0.1167, slow_pct=10, slow_ms=150),
    # ---- adv2: constrained bandwidth on the server port (bandwidth_test.py: 500 @ 2 Mbit/s) -
    Scenario("bw1m_250", "adv2", "1 Mbit/s, 250 clients", clients=250, shape=Shape(rate="1mbit")),
    Scenario("bw2m_slow", "adv2", "2 Mbit/s, 500 clients, 10% slow readers at 500 ms",
             clients=500, slow_pct=10, shape=Shape(rate="2mbit")),
    Scenario("bw2m_loss", "adv2", "2 Mbit/s + 20 ms delay + 1% loss, 500 clients",
             clients=500, shape=Shape(rate="2mbit", delay_ms=20, loss_pct=1)),
    Scenario("bw2m_lifted", "adv2", "2 Mbit/s for the first 30 s, then unlimited (catch-up), 500 clients, 90 s",
             clients=500, duration=90, shape=Shape(rate="2mbit"), shape_until=30),
    Scenario("bw2m_reconnect", "adv2",
             "2 Mbit/s, 300 clients, 20% drop at 20 s for 5 s, 20% join late at 15 s asking 60 s",
             clients=300, disc_pct=20, disc_at=20, late_pct=20, late_at=15, history_seconds=60,
             shape=Shape(rate="2mbit")),
    # ---- recon: reconnect and catch-up (public T4 is 20% for 5 s) -------------------------
    Scenario("mass_all", "recon", "100% of clients drop at 15 s for 5 s (simultaneous reconnect)", disc_pct=100),
    Scenario("mass_half_30s", "recon", "50% drop at 15 s for 30 s", disc_pct=50, disc_s=30, duration=70),
    Scenario("outage_75s", "recon", "20% drop at 10 s for 75 s (beyond the 60 s they may ask for)",
             disc_pct=20, disc_at=10, disc_s=75, duration=100),
    Scenario("reconnect_loss", "recon", "20% drop for 5 s on the public T5 link (120+-30 ms, 3% loss)",
             disc_pct=20, shape=Shape(delay_ms=120, jitter_ms=30, loss_pct=3)),
]
COMPONENTS = {"adv1": "slow-reader backpressure", "adv2": "constrained bandwidth", "recon": "reconnect and catch-up"}


# ---------------------------------------------------------------------- pure helpers

def select(only: str | None) -> list[Scenario]:
    if not only:
        return list(SCENARIOS)
    wanted = [w.strip() for w in only.split(",") if w.strip()]
    out = [s for s in SCENARIOS if s.name in wanted or s.component in wanted]
    unknown = [w for w in wanted if w not in COMPONENTS and w not in {s.name for s in SCENARIOS}]
    if unknown:
        raise SystemExit(f"unknown scenario/component: {', '.join(unknown)} (see --list)")
    return out


def segments_for(sc: Scenario) -> int:
    """Segments that fit the run, as run_demo.default_source_segments but for any pace (3 revisions each)."""
    per_segment = 2 * sc.revision_delay + sc.segment_gap
    active = max(3.0, sc.duration - SOURCE_START_S - TAIL_PAD_S)
    return max(4, math.floor(active / per_segment))


def roles(sc: Scenario) -> list[tuple[bool, bool, bool]]:
    """(slow, disconnecting, late) per client index; disjoint, in that order, as load_clients does."""
    n = sc.clients
    n_slow, n_disc = round(n * sc.slow_pct / 100), round(n * sc.disc_pct / 100)
    n_late = round(n * sc.late_pct / 100)
    if n_slow + n_disc + n_late > n:
        raise ValueError(f"{sc.name}: roles exceed the client count")
    return [(i < n_slow, n_slow <= i < n_slow + n_disc, n_slow + n_disc <= i < n_slow + n_disc + n_late)
            for i in range(n)]


def tc_commands(shape: Shape, iface: str, port: int) -> list[list[str]]:
    """prio root (unmatched traffic unshaped) -> [netem] -> [tbf] on the test port, both directions."""
    cmds = [["qdisc", "add", "dev", iface, "root", "handle", "1:", "prio", "bands", "3",
             "priomap", *(["2"] * 16)]]
    parent = "1:1"
    if shape.delay_ms or shape.loss_pct:
        netem = ["qdisc", "add", "dev", iface, "parent", parent, "handle", "10:", "netem"]
        if shape.delay_ms:
            netem += ["delay", f"{shape.delay_ms:g}ms"]
            if shape.jitter_ms:
                netem += [f"{shape.jitter_ms:g}ms", "distribution", "normal"]
        if shape.loss_pct:
            netem += ["loss", f"{shape.loss_pct:g}%"]
        cmds.append(netem)
        parent = "10:1"
    if shape.rate:
        cmds.append(["qdisc", "add", "dev", iface, "parent", parent, "handle", "20:", "tbf",
                     "rate", shape.rate, "burst", str(shape.burst), "latency", shape.latency])
    for direction in ("dport", "sport"):
        cmds.append(["filter", "add", "dev", iface, "parent", "1:", "protocol", "ip", "prio", "1",
                     "u32", "match", "ip", direction, str(port), "0xffff", "flowid", "1:1"])
    return cmds


def tail_metrics(rows: list[dict], end_epoch: float, threshold_ms: float) -> dict:
    """Normal clients over the last TAIL_S of the run: did delivery converge once impairment ended?"""
    tail = [r for r in rows if r.get("event_type") == "CAPTION_UPDATE" and r.get("status") == "accepted"
            and r.get("slow") == "0" and float(r["receive_ts"]) >= end_epoch - TAIL_S]
    lat = [float(r["latency_ms"]) for r in tail]
    return {
        "tail_messages": len(tail),
        "tail_p95_ms": percentile(lat, 95) if lat else None,
        "tail_fresh_pct": 100.0 * sum(x <= threshold_ms for x in lat) / len(lat) if lat else None,
        "tail_provisional_pct": 100.0 * sum(r.get("final") == "0" for r in tail) / len(tail) if tail else None,
    }


def finals_due(rows: list[dict], segments: int, end_epoch: float, grace_s: float = GRACE_S) -> dict:
    """Completeness over the finals the source sent at least grace_s before the clients
    stopped (per client, finals received live or via HISTORY). Segment send times come
    from source_ts of any received copy; the source sends segments in order, so a
    segment nobody received counts as due when a later segment was due."""
    sent_at: dict[int, float] = {}
    got: dict[str, set[int]] = {}
    for r in rows:
        if r.get("client_id", "") != "":
            got.setdefault(r["client_id"], set())
        if r.get("event_type") in ("CAPTION_UPDATE", "HISTORY_ITEM") and r.get("final") == "1":
            seg = int(r["segment"])
            sent_at.setdefault(seg, float(r["source_ts"]))
            if r.get("status") in ("accepted", "recovered"):
                got[r["client_id"]].add(seg)
    deadline = end_epoch - grace_s
    due_known = [s for s, ts in sent_at.items() if ts <= deadline]
    last_due = max(due_known, default=0)
    due = set(range(1, min(last_due, segments) + 1))
    if not got or not due:
        return {"finals_due_pct": None, "finals_due_segments": len(due)}
    have = sum(len(g & due) for g in got.values())
    return {"finals_due_pct": 100.0 * have / (len(due) * len(got)), "finals_due_segments": len(due)}


def problems_of(s: dict) -> list[str]:
    out = []
    due = s.get("finals_due_pct")
    if due is not None and due < 100.0 - 1e-9:
        out.append(f"finals due {due:.2f}%")
    elif due is None and (s.get("final_completeness_pct") or 0) < 100.0 - 1e-9:
        out.append(f"finals {s.get('final_completeness_pct'):.2f}%")
    for key in ("duplicates", "stale", "invalid_history_nonfinal"):
        if s.get(key, 0):
            out.append(f"{key}={s[key]}")
    return out


# --------------------------------------------------------------------------- run one

def tc(args_list: list[str]) -> None:
    prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo"]
    subprocess.run([*prefix, "tc", *args_list], check=True, capture_output=True)


def reset_tc(iface: str) -> None:
    prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo"]
    subprocess.run([*prefix, "tc", "qdisc", "del", "dev", iface, "root"], capture_output=True)


def client_namespace(sc: Scenario, uri: str, csv_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        uri=uri, session="L1", clients=sc.clients, duration=float(sc.duration), output=str(csv_path),
        seed=3103, history_seconds=sc.history_seconds, slow_percent=sc.slow_pct, slow_ms=float(sc.slow_ms),
        disconnect_percent=sc.disc_pct, disconnect_at=float(sc.disc_at), disconnect_seconds=float(sc.disc_s),
        late_percent=sc.late_pct, late_seconds=float(sc.late_at),
    )


async def drive(sc: Scenario, args, uri: str, csv_path: Path, log_fp, server_pid: int) -> tuple[float, int, list[float]]:
    segments = segments_for(sc)
    ns = client_namespace(sc, uri, csv_path)
    logger = load_clients.CSVLogger(str(csv_path))
    await logger.open()
    ns._global_start = time.monotonic()
    end_epoch = time.time() + sc.duration
    rss: list[float] = []

    async def sample_rss():
        while True:
            value = rss_mb(server_pid)
            if value is not None:
                rss.append(value)
            await asyncio.sleep(0.5)

    async def lift_shape():
        await asyncio.sleep(sc.shape_until)
        reset_tc(args.iface)
        print(f"  [{sc.name}] impairment lifted at {sc.shape_until:g} s", file=log_fp, flush=True)

    helpers = [asyncio.create_task(sample_rss())]
    if sc.shape is not None and sc.shape_until is not None:
        helpers.append(asyncio.create_task(lift_shape()))
    tasks = [asyncio.create_task(load_clients.simulated_client(i, ns, logger, slow=s, disconnecting=d, late=l))
             for i, (s, d, l) in enumerate(roles(sc))]
    await asyncio.sleep(SOURCE_START_S)
    source = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "caption_source.py", "--uri", uri, "--segments", str(segments),
        "--revision-delay", str(sc.revision_delay), "--segment-gap", str(sc.segment_gap), "--quiet",
        cwd=HERE, stdout=log_fp, stderr=subprocess.STDOUT)
    if await source.wait() != 0:
        raise SystemExit("caption_source.py failed; see the .log")
    await asyncio.gather(*tasks)
    for h in helpers:
        h.cancel()
    await logger.close()
    return end_epoch, segments, rss


def run_once(sc: Scenario, config: str, run_no: int, args) -> dict:
    stem = f"results/hidden{args.tag}_{sc.name}_{config}_run{run_no:02d}"
    csv_path, json_path, log_path = (HERE / f"{stem}.{ext}" for ext in ("csv", "json", "log"))
    uri = f"ws://127.0.0.1:{args.port}"
    env = os.environ.copy()
    env.pop("LIVERECALL_NO_COALESCE", None)
    if config == "before":
        env["LIVERECALL_NO_COALESCE"] = "1"
    try:
        with socket.create_connection(("127.0.0.1", args.port), timeout=0.25):
            raise SystemExit(f"port {args.port} is already in use; stop that process or pass --port")
    except OSError:
        pass

    print(f"\n=== {sc.name} [{sc.component}] {config}, run {run_no}: {sc.desc}", flush=True)
    server = None
    with log_path.open("w", encoding="utf-8") as log_fp:
        try:
            if sc.shape is not None:
                reset_tc(args.iface)
                for cmd in tc_commands(sc.shape, args.iface, args.port):
                    tc(cmd)
            server = subprocess.Popen([sys.executable, "-u", "solution_server.py", "--port", str(args.port)],
                                      cwd=HERE, stdout=log_fp, stderr=subprocess.STDOUT, env=env)
            wait_for_port(args.port, server)
            end_epoch, segments, rss = asyncio.run(drive(sc, args, uri, csv_path, log_fp, server.pid))
        finally:
            if sc.shape is not None:
                reset_tc(args.iface)
            terminate_process(server, "solution_server.py")
            time.sleep(0.25)

    text = log_path.read_text(encoding="utf-8", errors="replace")
    if not server_listened(text, args.port):
        raise SystemExit(f"the server never listened on {args.port}; see {stem}.log")
    if ("baseline mode: --no-coalesce" in text) != (config == "before"):
        raise SystemExit(f"the server ran in the wrong mode for '{config}'; see {stem}.log")

    s = analyse_file(csv_path, expected_finals=segments, expected_updates=segments * 3,
                     fresh_threshold_ms=args.fresh_threshold_ms)
    with csv_path.open(newline="", encoding="utf-8") as fp:
        rows = list(csv.DictReader(fp))
    s.update(tail_metrics(rows, end_epoch, args.fresh_threshold_ms))
    s.update(finals_due(rows, segments, end_epoch))
    s.update(server=parse_stats(text), config=config, scenario=sc.name, segments=segments,
             server_rss_mb_max=max(rss) if rss else None)
    s["problems"] = problems_of(s)
    json_path.write_text(json.dumps(s, indent=2), encoding="utf-8")
    print(f"  finals {fmt(s.get('final_completeness_pct'), 2)}% (due {fmt(s.get('finals_due_pct'), 2)}% of "
          f"{s.get('finals_due_segments')})  dup {s['duplicates']}  stale {s['stale']}  "
          f"errors {s['connection_errors']}  coverage {fmt(s.get('normal_update_coverage_pct'))}%  "
          f"P50/P95 {fmt(s.get('p50_ms'), 0)}/{fmt(s.get('p95_ms'), 0)} ms  fresh {fmt(s.get('fresh_pct'))}%  "
          f"fresh finals {fmt(s.get('fresh_final_pct'))}%  tail P95 {fmt(s.get('tail_p95_ms'), 0)} ms  "
          f"hopeless {s['server']['hopeless_closes_total']}"
          + (f"  PROBLEMS: {s['problems']}" if s["problems"] else ""), flush=True)
    return s


# ---------------------------------------------------------------------------- report

def fmt(v, digits=1):
    return "n/a" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{v:.{digits}f}"


def med(runs: list[dict], key: str, sub: str | None = None):
    vals = []
    for r in runs:
        v = r.get(key)
        if sub is not None:
            v = v.get(sub) if isinstance(v, dict) else None
        if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
            vals.append(v)
    return statistics.median(vals) if vals else None


COLUMNS = [  # (header, key, sub, digits)
    ("finals %", "final_completeness_pct", None, 2),
    ("due %", "finals_due_pct", None, 2),
    ("cover %", "normal_update_coverage_pct", None, 1),
    ("P95 ms", "p95_ms", None, 0),
    ("normal P95", "normal_clients", "p95_ms", 0),
    ("slow P95", "slow_clients", "p95_ms", 0),
    ("fresh fin %", "fresh_final_pct", None, 1),
    ("tail P95", "tail_p95_ms", None, 0),
    ("recov P95", "recovery_p95_ms", None, 0),
    ("errors", "connection_errors", None, 0),
]


def print_table(results: dict[str, dict[str, list[dict]]]) -> None:
    print("\n| scenario | config | " + " | ".join(h for h, *_ in COLUMNS) + " | dup/stale | verdict |")
    print("|---|---|" + "---:|" * len(COLUMNS) + "---|---|")
    for sc in SCENARIOS:
        if sc.name not in results:
            continue
        for config in CONFIGS:
            runs = results[sc.name].get(config, [])
            if not runs:
                continue
            cells = [fmt(med(runs, k, sub), d) for _, k, sub, d in COLUMNS]
            dup = sum(r.get("duplicates", 0) for r in runs)
            stale = sum(r.get("stale", 0) for r in runs)
            bad = [p for r in runs for p in r["problems"]]
            verdict = ("FAIL: " + "; ".join(bad)) if bad and config == "after" else ("ok" if not bad else "; ".join(bad))
            print(f"| {sc.name} | {config} | " + " | ".join(cells) + f" | {dup}/{stale} | {verdict} |")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--list", action="store_true", help="list the scenarios and exit")
    p.add_argument("--only", help="scenario names and/or components (adv1, adv2, recon), comma-separated")
    p.add_argument("--configs", default="before,after", help="before,after (default) or one of them")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--port", type=int, default=8793)
    p.add_argument("--iface", default="lo")
    p.add_argument("--tag", default="")
    p.add_argument("--fresh-threshold-ms", type=float, default=500.0)
    args = p.parse_args()

    if args.list:
        for sc in SCENARIOS:
            extra = f", shaped{' until ' + format(sc.shape_until, 'g') + ' s' if sc.shape_until else ''}" if sc.shape else ""
            print(f"{sc.name:15s} {sc.component:5s} {sc.clients:4d} clients {sc.duration:4.0f} s{extra}  {sc.desc}")
        return 0

    configs = [c.strip() for c in args.configs.split(",") if c.strip()]
    if not configs or any(c not in CONFIGS for c in configs):
        p.error("--configs takes before, after or both")
    scenarios = select(args.only)
    if any(sc.shape for sc in scenarios) and not Path("/proc/net/dev").exists():
        raise SystemExit("shaped scenarios need Linux tc (Ubuntu VM or WSL2)")

    drift = timer_drift()
    print(f"asyncio timer drift: {drift:+.0%}", flush=True)
    if drift > TIMER_DRIFT_WARN:
        print("WARNING: timers on this machine are unreliable; results will not be meaningful (see HANDOFF.md).")

    RESULTS.mkdir(exist_ok=True)
    results: dict[str, dict[str, list[dict]]] = {}
    failed = False
    for sc in scenarios:
        for run_no in range(1, args.runs + 1):
            for config in configs:  # interleaved: both configs see the same machine conditions
                s = run_once(sc, config, run_no, args)
                results.setdefault(sc.name, {}).setdefault(config, []).append(s)
                failed |= config == "after" and bool(s["problems"])

    print_table(results)
    summary_path = RESULTS / f"hidden{args.tag}_summary.json"
    summary_path.write_text(json.dumps({
        "timer_drift": drift, "args": vars(args),
        "scenarios": {sc.name: asdict(sc) for sc in scenarios},
        "results": results}, indent=2, default=str), encoding="utf-8")
    print(f"\nSummary: {summary_path.relative_to(HERE)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
