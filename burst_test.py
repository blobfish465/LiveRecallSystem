"""Faster / bursty caption source and variable revision counts, through the unmodified harness.

The spec's hidden evaluation may vary "caption revision rate and burstiness" (section 10),
but run_demo.py always starts caption_source.py at the public pace (0.20 s between
revisions, 0.35 s gap, ~4 messages/s). This tool repeats run_demo.py's wiring with the
same unmodified programs (solution_server.py, load_clients.py, caption_source.py,
analyse_results.analyse_file) and only passes caption_source.py's own pacing options:

    --revision-delay  seconds between revisions of a segment
    --segment-gap     seconds after a final before the next segment
    --mixed           a transcript of short and long sentences: the public generator
                      emits 1 revision for sentences under 4 words and 3 otherwise, so
                      revision counts vary per segment (expected updates are counted
                      with the generator's own make_revisions())

Scenarios we ran (100 clients, 40 s, 3 runs; HANDOFF.md, Day 5):
    python burst_test.py fast5_mixed 0.05 0.05 --mixed    # ~5x faster, 1 or 3 revisions
    python burst_test.py fast12 0.02 0.02                 # ~12x faster (~50 msg/s)
    python burst_test.py burst 0.0 0.35                   # 3 revisions back to back

Do not combine a fast source with T3's slow readers and expect 100% finals: at 5x a
500 ms reader cannot keep up even with finals alone, which the spec rules out for the
evaluation (requirement 11: "will not intentionally make a client permanently so slow
that it cannot keep up even with the final-caption stream alone").

Results: results/burst/<name>_runNN.{csv,json,log} (gitignored). Exit 1 if a run of our
server (not --no-coalesce) has < 100% finals, duplicates, stale updates or connection errors.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

from analyse_results import analyse_file
from caption_source import DEFAULT_SENTENCES, make_revisions
from run_demo import terminate_process, wait_for_port

MIXED = [
    "Okay.", "Right, so.", "Next slide please.",  # under 4 words: 1 revision (final only)
    "Today we look at how TCP congestion control reacts to loss.",
    "Any questions?",
    "A WebSocket runs over one TCP connection, so loss shows up as delay.",
    "Good.",
    "The server must never let an older revision replace a final one.",
]
SOURCE_START_DELAY = 1.0  # run_demo.py's default


def plan(sentences, revision_delay, segment_gap, duration):
    """Segments that fit run_demo's source budget at this pace, and their total updates."""
    seg_s = [(len(make_revisions(s)) - 1) * revision_delay + segment_gap for s in sentences]
    active = max(3.0, duration - SOURCE_START_DELAY - 2.0)  # as default_source_segments()
    segments, t = 0, 0.0
    while t + seg_s[segments % len(sentences)] <= active:
        t += seg_s[segments % len(sentences)]
        segments += 1
    updates = sum(len(make_revisions(sentences[i % len(sentences)])) for i in range(segments))
    return segments, updates


def one_run(a, run, segments, updates, transcript, env):
    out = Path("results/burst")
    stem = out / f"{a.name}_run{run:02d}"
    uri = f"ws://127.0.0.1:{a.port}"
    server = clients = None
    with open(f"{stem}.log", "w") as log:
        try:
            server = subprocess.Popen([sys.executable, "-u", "solution_server.py", "--port", str(a.port)],
                                      stdout=log, stderr=subprocess.STDOUT, env=env)
            wait_for_port(a.port, server)
            clients = subprocess.Popen([sys.executable, "-u", "load_clients.py", "--uri", uri,
                                        "--clients", str(a.clients), "--duration", str(a.duration),
                                        "--output", f"{stem}.csv", "--history-seconds", "30",
                                        "--slow-percent", str(a.slow_percent), "--slow-ms", str(a.slow_ms),
                                        "--seed", "3103"], stdout=log, stderr=subprocess.STDOUT)
            time.sleep(SOURCE_START_DELAY)
            src = [sys.executable, "-u", "caption_source.py", "--uri", uri, "--segments", str(segments),
                   "--revision-delay", str(a.revision_delay), "--segment-gap", str(a.segment_gap), "--quiet"]
            if a.mixed:
                src += ["--transcript", str(transcript)]
            if subprocess.run(src, stdout=log, stderr=subprocess.STDOUT).returncode != 0:
                raise RuntimeError("caption_source.py failed")
            if clients.wait() != 0:
                raise RuntimeError("load_clients.py failed")
        finally:
            terminate_process(clients, "load_clients.py")
            terminate_process(server, "solution_server.py")
            time.sleep(0.25)
    s = analyse_file(Path(f"{stem}.csv"), expected_finals=segments,
                     expected_updates=updates, fresh_threshold_ms=500)
    Path(f"{stem}.json").write_text(json.dumps(s, indent=1))
    return s


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("name", help="result file prefix")
    p.add_argument("revision_delay", type=float, help="caption_source.py --revision-delay (public: 0.20)")
    p.add_argument("segment_gap", type=float, help="caption_source.py --segment-gap (public: 0.35)")
    p.add_argument("--mixed", action="store_true", help="variable revision counts (1 or 3)")
    p.add_argument("--clients", type=int, default=100)
    p.add_argument("--duration", type=float, default=40)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--slow-percent", type=float, default=0)
    p.add_argument("--slow-ms", type=float, default=500)
    p.add_argument("--port", type=int, default=8798)
    p.add_argument("--no-coalesce", action="store_true", help="plain-FIFO baseline (before/after)")
    a = p.parse_args()

    Path("results/burst").mkdir(parents=True, exist_ok=True)
    transcript = Path("results/burst/mixed_transcript.txt")
    transcript.write_text("\n".join(MIXED) + "\n", encoding="utf-8")
    sentences = MIXED if a.mixed else DEFAULT_SENTENCES
    segments, updates = plan(sentences, a.revision_delay, a.segment_gap, a.duration)
    env = dict(os.environ)
    env.pop("LIVERECALL_NO_COALESCE", None)  # an exported baseline switch must not leak in
    if a.no_coalesce:
        env["LIVERECALL_NO_COALESCE"] = "1"
    print(f"{a.name}: {segments} segments, {updates} updates in {a.duration:.0f} s "
          f"(~{updates / (a.duration - 3):.0f} msg/s), {a.clients} clients"
          + (" [plain FIFO]" if a.no_coalesce else ""), flush=True)

    runs, problems = [], []
    for run in range(1, a.runs + 1):
        s = one_run(a, run, segments, updates, transcript, env)
        runs.append(s)
        print(f"  run {run}: finals {s['final_completeness_pct']:.2f}%  coverage "
              f"{s['normal_update_coverage_pct']:.1f}%  dup {s['duplicates']}  stale {s['stale']}  "
              f"errors {s['connection_errors']}  P50/P95/P99 {s['p50_ms']:.1f}/{s['p95_ms']:.1f}/"
              f"{s['p99_ms']:.1f} ms", flush=True)
        if s["final_completeness_pct"] < 100 or s["duplicates"] or s["stale"] or s["connection_errors"]:
            problems.append(run)
    med = lambda k: statistics.median(r[k] for r in runs)  # noqa: E731
    print(f"median of {len(runs)}: finals {med('final_completeness_pct'):.2f}%  coverage "
          f"{med('normal_update_coverage_pct'):.1f}%  P50/P95/P99 {med('p50_ms'):.1f}/"
          f"{med('p95_ms'):.1f}/{med('p99_ms'):.1f} ms  fresh {med('fresh_pct'):.1f}%")
    if problems and not a.no_coalesce:
        print(f"FAIL: runs {problems} lost finals or had duplicates/stale/errors")
        sys.exit(1)


if __name__ == "__main__":
    main()
