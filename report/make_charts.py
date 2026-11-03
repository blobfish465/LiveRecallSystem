"""Report charts (Figures 4-6) from report/data/. Needs matplotlib (requirements-dev.txt).

    python report/make_charts.py [out_dir]     # default: report/figures
"""
import csv
import json
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

DATA = Path(__file__).parent / "data"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "figures"
ENV = "Ubuntu 26.04 VM (Apple Silicon, 2 vCPU), timer drift +0-1%, "

BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, GRID, GREY = "#0b0b0b", "#52514e", "#e4e3df", "#8a8985"

plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 11,
    "axes.edgecolor": INK2, "axes.labelcolor": INK2, "axes.titlecolor": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.axisbelow": True, "figure.dpi": 100, "savefig.dpi": 200,
})


def footer(fig, runs="median of 3 runs"):
    fig.text(0.01, 0.01, ENV + runs, fontsize=9, color=INK2, ha="left", va="bottom")


def fig4():
    pub = json.loads((DATA / "public_tests.json").read_text())
    t5 = json.loads((DATA / "t5.json").read_text())
    tests = [("T1\n20 clients", pub["t1"]), ("T2\n100 clients", pub["t2"]),
             ("T3\n10% slow", pub["t3"]), ("T4\n20% drop 5 s", pub["t4"]),
             ("Late join\n20%", pub["late"]), ("T5\ndelay_loss", t5["delay_loss_final"])]
    # dx: small horizontal offset so P95 and P99 don't hide each other where they are close.
    pcts = [("P50", "p50_ms", BLUE, "o", -0.1), ("P95", "p95_ms", ORANGE, "s", 0), ("P99", "p99_ms", AQUA, "D", 0.1)]

    fig, ax = plt.subplots(figsize=(9, 4.8))
    xs = range(len(tests))
    for x, (_, runs) in zip(xs, tests):
        vals = [statistics.median(r[k] for r in runs) for _, k, _, _, _ in pcts]
        ax.plot([x - 0.1, x, x + 0.1], vals, color=GRID, lw=2, zorder=1)
    for name, key, colour, marker, dx in pcts:
        ys = [statistics.median(r[key] for r in runs) for _, runs in tests]
        ax.scatter([x + dx for x in xs], ys, s=64, color=colour, marker=marker, label=name,
                   edgecolor="white", linewidth=1.5, zorder=3)
    # One direct label, on the outlier: T3's P99 comes from the slow readers.
    t3_p99 = statistics.median(r["p99_ms"] for r in pub["t3"])
    ax.annotate(f"{t3_p99 / 1000:.1f} s: slow readers'\n16-message read-ahead", (2.1, t3_p99),
                xytext=(12, -4), textcoords="offset points", fontsize=9, color=INK2, va="center")
    ax.set_yscale("log")
    ax.set_ylim(1, 20000)
    ax.set_yticks([1, 10, 100, 1000, 10000], ["1 ms", "10 ms", "100 ms", "1 s", "10 s"])
    ax.set_xticks(list(xs), [t for t, _ in tests])
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Caption age (log scale)")
    ax.set_title("Caption age per public test", loc="left", fontsize=13, pad=12)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1), frameon=False, ncol=3, handletextpad=0.3)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    footer(fig)
    fig.savefig(OUT / "fig4_caption_age.png")


def fig5():
    rows = list(csv.DictReader(open(DATA / "bandwidth_windows_10s.csv")))
    windows = sorted({int(r["window_start_s"]) for r in rows})
    fig, ax = plt.subplots(figsize=(9, 4.8))
    # Ours in the accent colour, the FIFO baseline in grey; solid = P50, dashed = P95.
    for config, name, colour in [("before", "Plain FIFO", GREY), ("after", "Our server", BLUE)]:
        for key, style in [("p50_ms", "-"), ("p95_ms", "--")]:
            ys = [statistics.median(float(r[key]) / 1000 for r in rows
                                    if r["config"] == config and int(r["window_start_s"]) == w)
                  for w in windows]
            xs = [w + 5 for w in windows]  # plot at the window's midpoint
            ax.plot(xs, ys, style, color=colour, lw=2, marker="o" if style == "-" else None,
                    markersize=7, markeredgecolor="white", markeredgewidth=1.5)
            if key == "p50_ms":
                ax.annotate(name, (xs[-1], ys[-1]), xytext=(10, 0), textcoords="offset points",
                            fontsize=10, color=INK, va="center")
    ax.plot([], [], "-", color=INK2, lw=2, label="P50")
    ax.plot([], [], "--", color=INK2, lw=2, label="P95")
    ax.set_xlim(0, 68)
    ax.set_xticks(range(0, 61, 10), [f"{t} s" for t in range(0, 61, 10)])
    ax.set_ylim(0, 2)
    ax.set_yticks([0, 0.5, 1, 1.5, 2], ["0", "0.5 s", "1 s", "1.5 s", "2 s"])
    ax.grid(axis="x", visible=False)
    ax.set_xlabel("Time into the run (10 s windows)")
    ax.set_ylabel("Caption age")
    ax.set_title("Caption age over time: 500 clients on 2 Mbit/s", loc="left", fontsize=13, pad=12)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1), frameon=False, ncol=2, handlelength=2.5)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    footer(fig)
    fig.savefig(OUT / "fig5_bandwidth_over_time.png")


def fig6():
    fig, ax = plt.subplots(figsize=(9, 4.8))
    for fname, name, colour, dy in [("memory_5min_rss_before_fifo.csv", "Plain FIFO", GREY, 8),
                                    ("memory_5min_rss_after.csv", "Our server", BLUE, -8)]:
        rows = list(csv.DictReader(open(DATA / fname)))
        xs = [float(r["elapsed_s"]) for r in rows]
        ys = [float(r["rss_mb"]) for r in rows]
        ax.plot(xs, ys, color=colour, lw=2)
        ax.annotate(f"{name}: {ys[0]:.1f} to {max(ys):.1f} MB", (xs[-1], ys[-1]), xytext=(-4, dy),
                    textcoords="offset points", fontsize=10, color=INK, ha="right",
                    va="bottom" if dy > 0 else "top")
    # 30-40 MB window, not zero-based, so the two lines stay apart; the ticks show the range.
    ax.set_ylim(30, 40)
    ax.set_yticks(range(30, 41, 2), [f"{v} MB" for v in range(30, 41, 2)])
    ax.set_xlim(0, 300)
    ax.set_xticks(range(0, 301, 60), ["0", "1 min", "2 min", "3 min", "4 min", "5 min"])
    ax.grid(axis="x", visible=False)
    ax.set_xlabel("Time into the run")
    ax.set_ylabel("Server RSS")
    ax.set_title("Server memory over 5 minutes: 100 clients, 10% slow readers", loc="left",
                 fontsize=13, pad=12)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    footer(fig, "1 run each")
    fig.savefig(OUT / "fig6_memory_5min.png")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    fig4()
    fig5()
    fig6()
