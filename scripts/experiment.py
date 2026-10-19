#!/usr/bin/env python3
"""Run the study.

  python3 scripts/experiment.py pilot        # choose M and the adaptive thresholds on seeds 1-5
  python3 scripts/experiment.py final        # 3 loads x 20 seeds x 6 policies on seeds 101-120
  python3 scripts/experiment.py environment  # record machine, compiler and clock overhead

The pilot writes configs/experiment.json. `final` refuses to run if the C
sources changed after that file was written, so the thresholds cannot be
re-tuned after seeing final results.
"""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import csv
import json
import os
import platform
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workload import N_REQUESTS, WARMUP, WINDOW, generate, sha256  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = ROOT / "build" / "simulator"
CONFIG = ROOT / "configs" / "experiment.json"
SUMMARY = ROOT / "results" / "summary"
RAW = ROOT / "results" / "raw"

POLICIES = ["FF", "BF", "WF", "NF", "Adaptive", "RandomSwitch"]
FIXED = POLICIES[:4]
LOADS = [0.60, 0.80, 0.95]
PILOT_SEEDS = list(range(1, 6))
FINAL_SEEDS = list(range(101, 121))
THETA_GRID = [0.3, 0.4, 0.5, 0.6, 0.7]
PHI_GRID = [0.05, 0.10, 0.20]
FROZEN_FILES = ["src/allocator.c", "src/allocator.h", "src/check.h", "src/simulator.c",
                "scripts/workload.py"]


def build():
    subprocess.run(["make", "build/simulator"], cwd=ROOT, check=True, capture_output=True)


def simulate(meta, policy, theta=0.5, phi=0.1, hysteresis=False, p_bf=0.0,
             raw=False, check=False):
    """Run one trace under one policy and return the simulator's summary as a dict."""
    tid = meta["trace_id"]
    cmd = [str(SIMULATOR),
           "--trace", str(ROOT / "workloads" / f"{tid}.csv"),
           "--uniforms", str(ROOT / "workloads" / f"random_draws_seed{meta['seed']}.csv"),
           "--policy", policy,
           "--memory", str(meta["M"]),
           "--requests", str(meta["arrivals"]),
           "--warmup", str(WARMUP),
           "--window", str(WINDOW),
           "--theta", str(theta), "--phi", str(phi),
           "--hysteresis", str(int(hysteresis)),
           "--p-bf", str(p_bf)]
    if raw:
        RAW.mkdir(parents=True, exist_ok=True)
        cmd += ["--raw", str(RAW / f"{tid}_{policy}.csv")]
    if check:
        cmd.append("--check")

    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    result = json.loads(out.stdout)
    result.update(trace_id=tid, M=meta["M"], rho_target=meta["rho_target"],
                  seed=meta["seed"], algorithm=policy, run_id=f"{tid}_{policy}")
    return result


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, restval="", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def mean_of(rows, key, rho=None):
    return float(np.mean([r[key] for r in rows if rho is None or r["rho_target"] == rho]))


# ---------------------------------------------------------------- pilot

def pilot():
    build()
    started = time.perf_counter()
    log = []

    # 1. Memory size: the smallest M at which First Fit keeps at least 25 holes on average.
    holes_at = {}
    for memory in (4096, 16384):
        runs = [simulate(generate(memory, 0.8, s), "FF", check=True) for s in PILOT_SEEDS]
        log += runs
        holes_at[memory] = mean_of(runs, "mean_holes")
    memory = 4096 if holes_at[4096] >= 25 else 16384

    # 2. Best Fit baselines at the three loads. The top load must show more
    #    fragmentation than the low load and be fragmentation-dominated; if not, drop it to 0.90.
    loads = list(LOADS)
    traces = {(rho, s): generate(memory, rho, s) for rho in loads for s in PILOT_SEEDS}
    baseline = [simulate(m, "BF", check=True) for m in traces.values()]
    log += baseline
    top = loads[-1]
    top_load_adjusted = not (mean_of(baseline, "frag_fail_rate", top) > mean_of(baseline, "frag_fail_rate", 0.6)
                             and mean_of(baseline, "cap_fail_rate", top) < mean_of(baseline, "frag_fail_rate", top))
    if top_load_adjusted:
        loads[-1] = 0.90
        traces = {(rho, s): generate(memory, rho, s) for rho in loads for s in PILOT_SEEDS}
        baseline = [simulate(m, "BF", check=True) for m in traces.values()]
        log += baseline

    # 3. Threshold grid. A setting qualifies if its failure rate is within 10% of
    #    Best Fit at every load; among those, take the one with the fewest probes.
    windows = (N_REQUESTS - WARMUP) // WINDOW - 1
    grid = []
    for theta in THETA_GRID:
        for phi in PHI_GRID:
            runs = [simulate(m, "Adaptive", theta, phi) for m in traces.values()]
            for r in runs:
                r.update(theta=theta, phi=phi)
            log += runs
            qualifies = all(mean_of(runs, "frag_fail_rate", rho)
                            <= 1.1 * mean_of(baseline, "frag_fail_rate", rho) + 1e-15
                            for rho in loads)
            grid.append({"theta": theta, "phi": phi, "qualifies": qualifies,
                         "mean_frag": mean_of(runs, "frag_fail_rate"),
                         "mean_probes": mean_of(runs, "mean_total_probes"),
                         "switches_per_100_windows": mean_of(runs, "switches") / windows * 100})
    qualified = [g for g in grid if g["qualifies"]]
    if qualified:
        chosen = min(qualified, key=lambda g: (g["mean_probes"], g["theta"], g["phi"]))
    else:
        chosen = min(grid, key=lambda g: (g["mean_frag"], g["theta"], g["phi"]))

    # 4. Hysteresis only if the chosen rule flips mode in more than 20% of windows.
    hysteresis = chosen["switches_per_100_windows"] > 20

    # 5. Check every policy with full invariant checking after every event.
    validation = [simulate(traces[(0.8, s)], p, chosen["theta"], chosen["phi"], hysteresis,
                           p_bf=0.5, check=True)
                  for s in PILOT_SEEDS for p in POLICIES]

    write_csv(SUMMARY / "pilot_runs.csv", log)
    write_csv(SUMMARY / "threshold_grid.csv", grid)
    write_csv(SUMMARY / "validation_runs.csv", validation)

    config = {
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "M": memory, "loads": loads, "theta": chosen["theta"], "phi": chosen["phi"],
        "hysteresis": hysteresis, "W": WINDOW, "N": N_REQUESTS, "warmup": WARMUP,
        "pilot_seeds": PILOT_SEEDS, "final_seeds": FINAL_SEEDS,
        "pilot_mean_holes_FF": holes_at, "top_load_adjusted": top_load_adjusted,
        "chosen_setting": chosen,
        "source_sha256": {f: sha256(ROOT / f) for f in FROZEN_FILES},
    }
    CONFIG.parent.mkdir(exist_ok=True)
    CONFIG.write_text(json.dumps(config, indent=2) + "\n")
    print(json.dumps(config, indent=2))
    print(f"pilot finished in {time.perf_counter() - started:.1f} s")


# ---------------------------------------------------------------- final

def run_order(j):
    """Rotate the fixed policies by seed and slot the Adaptive/Random pair in at a rotating
    position, so no policy always runs first. Adaptive must run before RandomSwitch because
    the control copies Adaptive's measured BF share on the same trace."""
    fixed = FIXED[j % 4:] + FIXED[:j % 4]
    pos = j % 5
    return fixed[:pos] + ["Adaptive", "RandomSwitch"] + fixed[pos:]


def final():
    config = json.loads(CONFIG.read_text())
    for f, digest in config["source_sha256"].items():
        if sha256(ROOT / f) != digest:
            sys.exit(f"{f} changed after the pilot froze the configuration; rerun the pilot")
    build()

    started = time.perf_counter()
    rows = []
    for j, seed in enumerate(config["final_seeds"]):
        for rho in config["loads"]:
            meta = generate(config["M"], rho, seed)
            p_bf = 0.0
            for policy in run_order(j):
                r = simulate(meta, policy, config["theta"], config["phi"],
                             config["hysteresis"], p_bf, raw=True)
                if policy == "Adaptive":
                    p_bf = r["bf_share"]
                rows.append(r)
        print(f"seed {seed}: {len(rows)} / {len(config['final_seeds']) * len(config['loads']) * 6} runs",
              flush=True)

    write_csv(SUMMARY / "all_runs.csv", rows)
    (SUMMARY / "execution.json").write_text(json.dumps(
        {"runs": len(rows), "seconds": round(time.perf_counter() - started, 1),
         "config_sha256": sha256(CONFIG)}, indent=2) + "\n")


# ---------------------------------------------------------------- environment

def environment():
    subprocess.run(["make", "build/clock_overhead"], cwd=ROOT, check=True, capture_output=True)
    clock = json.loads(subprocess.check_output([str(ROOT / "build" / "clock_overhead")], text=True))
    cpu = "unknown"
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    env = {
        "platform": platform.platform(),
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "compiler": subprocess.check_output(["gcc", "--version"], text=True).splitlines()[0],
        "cflags": "-std=c11 -O2 -Wall -Wextra -Werror -pedantic",
        "python": platform.python_version(),
        "numpy": np.__version__,
        "timer": "clock_gettime(CLOCK_MONOTONIC), two reads per timed call",
        **clock,
    }
    out = ROOT / "results" / "environment.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(env, indent=2) + "\n")
    print(json.dumps(env, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["pilot", "final", "environment"])
    stage = parser.parse_args().stage
    {"pilot": pilot, "final": final, "environment": environment}[stage]()
