#!/usr/bin/env python3
"""Recompute every summary metric from the per-attempt raw logs.

For each of the 360 final runs this re-reads results/raw/<run>.csv, checks the
bookkeeping on every row (free + live = M, failure classes, probe rules) and
confirms that the means in all_runs.csv follow from the rows.
"""
from pathlib import Path
import csv
import json
import math

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "results" / "summary"
WARMUP, WINDOW, N = 1000, 50, 10_000


def check_row(k, row, M, policy):
    size, T, L = int(row["size"]), int(row["free_total"]), int(row["largest_free"])
    ok = row["success"] == "1"
    assert int(row["attempt"]) == int(row["pid"]) == k
    assert 0 <= L <= T <= M and int(row["live_units"]) + T == M
    assert (row["reason"] == "ok") == ok
    assert row["warmup"] == str(int(k < WARMUP))
    if ok:
        assert 0 <= int(row["start"]) <= M - size
    elif row["reason"] == "capacity":
        assert T < size and row["search_probes"] == "0"
    else:
        # fragmentation: enough memory in total, no hole large enough, every hole probed
        assert T >= size > L and row["search_probes"] == row["n_holes"]
    if policy != "Adaptive" or k % WINDOW:
        assert row["selector_probes"] == "0"


def audit_run(summary_row):
    M, policy = int(summary_row["M"]), summary_row["algorithm"]
    totals = dict.fromkeys(["frag_fail_rate", "cap_fail_rate", "mean_util", "mean_holes",
                            "mean_search_probes", "mean_selector_probes", "mean_total_probes",
                            "bf_share"], 0.0)
    dispersion, undefined, switches, last_mode, times, measured = 0.0, 0, 0, None, [], 0

    with (ROOT / "results" / "raw" / f"{summary_row['run_id']}.csv").open() as f:
        for k, row in enumerate(csv.DictReader(f)):
            check_row(k, row, M, policy)
            if k < WARMUP:
                continue
            measured += 1
            T, L = int(row["free_total"]), int(row["largest_free"])
            search, selector = int(row["search_probes"]), int(row["selector_probes"])
            totals["frag_fail_rate"] += row["reason"] == "fragmentation"
            totals["cap_fail_rate"] += row["reason"] == "capacity"
            totals["mean_util"] += (M - T) / M
            totals["mean_holes"] += int(row["n_holes"])
            totals["mean_search_probes"] += search
            totals["mean_selector_probes"] += selector
            totals["mean_total_probes"] += search + selector
            totals["bf_share"] += row["mode"] == "BF"
            if T:
                dispersion += 1 - L / T
            else:
                undefined += 1
            if last_mode is not None and row["mode"] != last_mode:
                switches += 1
            last_mode = row["mode"]
            times.append(int(row["elapsed_ns"]))

    assert k == N - 1 and measured == N - WARMUP == int(summary_row["attempts"])
    expected = {key: value / measured for key, value in totals.items()}
    expected["mean_F"] = dispersion / (measured - undefined)
    for key, value in expected.items():
        assert math.isclose(value, float(summary_row[key]), rel_tol=1e-9, abs_tol=1e-10), \
            (summary_row["run_id"], key, value, summary_row[key])
    assert switches == int(summary_row["switches"])
    assert float(np.median(times)) == float(summary_row["median_ns"])
    assert int(np.quantile(times, 0.99, method="inverted_cdf")) == int(summary_row["p99_ns"])
    return measured


def main():
    with (SUMMARY / "all_runs.csv").open() as f:
        runs = list(csv.DictReader(f))
    rows = sum(audit_run(r) for r in runs)
    result = {"status": "PASS", "runs": len(runs), "measured_rows": rows}
    (SUMMARY / "raw_audit.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
