#!/usr/bin/env python3
"""Turn results/summary/all_runs.csv into report tables, paired comparisons and figures.

Every number is a mean over the 20 final seeds. Intervals are 95% percentile
bootstrap intervals over seeds (10,000 resamples). For paired comparisons the
per-seed difference A - B is resampled, so both policies always see the same trace.
A seed is the unit of replication; individual attempts within one trace are
correlated and are not treated as independent samples.
"""
from pathlib import Path
import csv
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "results" / "summary"
TABLES = ROOT / "results" / "tables"
FIGURES = ROOT / "results" / "figures"

POLICIES = ["FF", "BF", "WF", "NF", "Adaptive", "RandomSwitch"]
LABELS = {"FF": "First Fit", "BF": "Best Fit", "WF": "Worst Fit", "NF": "Next Fit",
          "Adaptive": "Adaptive", "RandomSwitch": "Random switch"}
COLORS = {"FF": "#2a78d6", "BF": "#eb6834", "WF": "#1baf7a", "NF": "#eda100",
          "Adaptive": "#6250d6", "RandomSwitch": "#7a7a74"}
MARKERS = {"FF": "o", "BF": "s", "WF": "^", "NF": "D", "Adaptive": "P", "RandomSwitch": "X"}
INK, MUTED, GRID = "#1f1f1e", "#5c5c57", "#e4e3de"

METRICS = ["frag_fail_rate", "cap_fail_rate", "mean_F", "mean_util", "mean_holes",
           "mean_search_probes", "mean_selector_probes", "mean_total_probes",
           "median_ns", "p99_ns", "bf_share", "switches", "mean_release_ns"]
COMPARISONS = [("Adaptive", "FF"), ("Adaptive", "BF"), ("Adaptive", "RandomSwitch"),
               ("FF", "BF"), ("BF", "WF"), ("BF", "NF")]


def bootstrap_mean(values, resamples=10_000):
    values = np.asarray(values, dtype=float)
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(values), (resamples, len(values)))
    means = values[idx].mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return float(values.mean()), float(low), float(high)


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load():
    config = json.loads((ROOT / "configs" / "experiment.json").read_text())
    with (SUMMARY / "all_runs.csv").open() as f:
        rows = list(csv.DictReader(f))
    runs = {}
    for r in rows:
        key = (float(r["rho_target"]), int(r["seed"]), r["algorithm"])
        runs[key] = {m: float(r[m]) for m in METRICS}
    expected = len(config["loads"]) * len(config["final_seeds"]) * len(POLICIES)
    assert len(runs) == expected, f"expected {expected} runs, found {len(runs)}"
    return config, runs


def main():
    config, runs = load()
    loads, seeds = config["loads"], config["final_seeds"]

    def per_seed(rho, policy, metric):
        return [runs[rho, s, policy][metric] for s in seeds]

    # Means and seed-bootstrap intervals for every policy, load and metric.
    grouped = []
    for rho in loads:
        for p in POLICIES:
            for m in METRICS:
                mean, low, high = bootstrap_mean(per_seed(rho, p, m))
                grouped.append(dict(load=rho, algorithm=p, metric=m, mean=mean, ci_low=low, ci_high=high))
    write_csv(SUMMARY / "means_by_policy.csv", grouped)
    stat = {(g["load"], g["algorithm"], g["metric"]): g for g in grouped}

    def mean(rho, p, m):
        return stat[rho, p, m]["mean"]

    # Paired differences A - B.
    paired = []
    for rho in loads:
        for a, b in COMPARISONS:
            for m in ["frag_fail_rate", "mean_F", "mean_total_probes", "median_ns"]:
                diff = np.subtract(per_seed(rho, a, m), per_seed(rho, b, m))
                d, low, high = bootstrap_mean(diff)
                verdict = "A lower" if high < 0 else "A higher" if low > 0 else "no detectable difference"
                paired.append(dict(load=rho, a=a, b=b, metric=m, difference=d,
                                   ci_low=low, ci_high=high, verdict=verdict))
    write_csv(SUMMARY / "paired_differences.csv", paired)

    # How close each trace came to its target load.
    diagnostics = []
    for rho in loads:
        metas = [json.loads((ROOT / "workloads" / f"load{round(rho * 100)}_{config['M'] // 1024}mb_seed{s}.json").read_text())
                 for s in seeds]
        realized = [m["realized_offered_load"] for m in metas]
        diagnostics.append(dict(target=rho, mean=round(float(np.mean(realized)), 4),
                                min=round(min(realized), 4), max=round(max(realized), 4),
                                outside_2_percent=sum(not m["within_2_percent_of_target"] for m in metas)))
    write_csv(SUMMARY / "realized_load.csv", diagnostics)

    # ---- report tables (already rounded for the document)
    TABLES.mkdir(parents=True, exist_ok=True)
    write_csv(TABLES / "failure_rates.csv", [
        {"policy": LABELS[p], **{f"{rho:.2f}": f"{100 * mean(rho, p, 'frag_fail_rate'):.3f}" for rho in loads}}
        for p in POLICIES])
    write_csv(TABLES / "dispersion_and_utilization.csv", [
        {"policy": LABELS[p],
         **{f"F {rho:.2f}": f"{mean(rho, p, 'mean_F'):.3f}" for rho in loads},
         **{f"util {rho:.2f}": f"{100 * mean(rho, p, 'mean_util'):.1f}" for rho in loads}}
        for p in POLICIES])
    write_csv(TABLES / "search_cost.csv", [
        {"policy": LABELS[p],
         **{f"probes {rho:.2f}": f"{mean(rho, p, 'mean_total_probes'):.2f}" for rho in loads},
         **{f"ns {rho:.2f}": f"{mean(rho, p, 'median_ns'):.0f}" for rho in loads}}
        for p in POLICIES])
    write_csv(TABLES / "adaptive_modes.csv", [
        {"load": f"{rho:.2f}",
         "adaptive_bf_share_pct": f"{100 * mean(rho, 'Adaptive', 'bf_share'):.1f}",
         "random_bf_share_pct": f"{100 * mean(rho, 'RandomSwitch', 'bf_share'):.1f}",
         "switches_per_run": f"{mean(rho, 'Adaptive', 'switches'):.1f}",
         "selector_probes_per_attempt": f"{mean(rho, 'Adaptive', 'mean_selector_probes'):.2f}",
         "ff_mean_F": f"{mean(rho, 'FF', 'mean_F'):.3f}"}
        for rho in loads])
    pair = {(p["load"], p["a"], p["b"], p["metric"]): p for p in paired}
    t5 = []
    for rho in loads:
        for b in ["FF", "BF", "RandomSwitch"]:
            f = pair[rho, "Adaptive", b, "frag_fail_rate"]
            c = pair[rho, "Adaptive", b, "mean_total_probes"]
            t5.append({"load": f"{rho:.2f}", "baseline": LABELS[b],
                       "failure_pp": f"{100 * f['difference']:+.3f} [{100 * f['ci_low']:+.3f}, {100 * f['ci_high']:+.3f}]",
                       "probes": f"{c['difference']:+.2f} [{c['ci_low']:+.2f}, {c['ci_high']:+.2f}]"})
    write_csv(TABLES / "adaptive_vs_baselines.csv", t5)

    with (SUMMARY / "threshold_grid.csv").open() as f:
        grid = list(csv.DictReader(f))
    write_csv(TABLES / "threshold_grid.csv", [
        {"theta": g["theta"], "phi": g["phi"], "qualifies": g["qualifies"],
         "failure_pct": f"{100 * float(g['mean_frag']):.3f}", "probes": f"{float(g['mean_probes']):.2f}"}
        for g in grid])

    # ---- figures
    FIGURES.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": MUTED,
        "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED,
        "axes.spines.top": False, "axes.spines.right": False, "savefig.dpi": 220,
    })

    def style(ax):
        ax.grid(color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)

    # Figure 1: failure rate and dispersion against load.
    fig, axes = plt.subplots(1, 2, figsize=(7.2, 2.7))
    for ax, metric, ylabel, scale in [(axes[0], "frag_fail_rate", "Failed requests (%)", 100),
                                      (axes[1], "mean_F", "Mean F = 1 - L/T", 1)]:
        for p in POLICIES:
            y = np.array([mean(r, p, metric) for r in loads]) * scale
            lo = np.array([stat[r, p, metric]["ci_low"] for r in loads]) * scale
            hi = np.array([stat[r, p, metric]["ci_high"] for r in loads]) * scale
            ax.plot(loads, y, color=COLORS[p], marker=MARKERS[p], markersize=5,
                    linewidth=2 if p == "Adaptive" else 1.5, label=LABELS[p])
            ax.fill_between(loads, lo, hi, color=COLORS[p], alpha=0.12, linewidth=0)
        ax.set_xticks(loads)
        ax.set_xlabel("Offered load")
        ax.set_ylabel(ylabel)
        style(ax)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=6, frameon=False, fontsize=8)
    fig.tight_layout(rect=(0, 0.1, 1, 1))
    fig.savefig(FIGURES / "failures_and_dispersion.png", bbox_inches="tight")
    plt.close(fig)

    # Figure 2: probes per attempt, adaptive scan stacked on top.
    fig, axes = plt.subplots(1, 3, figsize=(7.2, 2.5), sharey=True)
    short = ["FF", "BF", "WF", "NF", "Adapt.", "Rand."]
    for ax, rho in zip(axes, loads):
        search = [mean(rho, p, "mean_search_probes") for p in POLICIES]
        scan = [mean(rho, p, "mean_selector_probes") for p in POLICIES]
        ax.bar(range(6), search, color=[COLORS[p] for p in POLICIES], width=0.7,
               edgecolor="white", linewidth=1)
        ax.bar(range(6), scan, bottom=search, color=INK, width=0.7, label="Selector scan")
        ax.set_xticks(range(6), short, rotation=35)
        ax.set_title(f"Offered load {rho:.2f}", fontsize=9, color=INK)
        style(ax)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("Probes per attempt")
    fig.legend(*axes[2].get_legend_handles_labels(), loc="lower center", frameon=False, fontsize=8)
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    fig.savefig(FIGURES / "search_cost.png", bbox_inches="tight")
    plt.close(fig)

    print("wrote means_by_policy.csv, paired_differences.csv, realized_load.csv, 6 tables and 2 figures")
    for rho in loads:
        print(f"\nload {rho}")
        for p in POLICIES:
            print(f"  {p:13s} fail {100 * mean(rho, p, 'frag_fail_rate'):6.3f}%  "
                  f"F {mean(rho, p, 'mean_F'):.3f}  probes {mean(rho, p, 'mean_total_probes'):6.2f}  "
                  f"median {mean(rho, p, 'median_ns'):6.0f} ns")


if __name__ == "__main__":
    main()
