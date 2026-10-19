#!/usr/bin/env python3
"""Cross-check the C simulator against a simple per-unit model.

The reference keeps an array `owner[M]` (which pid owns each unit, -1 if free)
and derives holes from it on every attempt, so it shares no split or merge
code with the C free list. It replays a trace with the same policy rules and
compares placement, failure class, probe counts, mode and memory state with
the C raw log, row by row.

Checks seed 101 at every load for all six policies, plus five small random
traces with short windows and hysteresis switched on and off.
"""
from pathlib import Path
import csv
import heapq
import json
import random
import subprocess
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
POLICIES = ["FF", "BF", "WF", "NF", "Adaptive", "RandomSwitch"]
FIELDS = ["success", "reason", "start", "search_probes", "selector_probes", "mode",
          "free_total", "largest_free", "n_holes", "live_units", "warmup"]


def holes_of(owner):
    """Maximal runs of free units as (start, size), in address order."""
    free = owner == -1
    edges = np.flatnonzero(np.diff(np.r_[False, free, False]))
    return [(int(a), int(b - a)) for a, b in zip(edges[::2], edges[1::2])]


def choose(holes, size, mode, rover):
    """Return (hole index or None, probes) using the textbook definition of each rule."""
    n = len(holes)
    fits = [i for i, (_, s) in enumerate(holes) if s >= size]
    if mode == "FF":
        return (fits[0], fits[0] + 1) if fits else (None, n)
    if mode == "BF":
        exact = [i for i in fits if holes[i][1] == size]
        best = min(fits, key=lambda i: (holes[i][1], i)) if fits else None
        return best, (exact[0] + 1 if exact else n)
    if mode == "WF":
        return (min(fits, key=lambda i: (-holes[i][1], i)) if fits else None), n
    # NF: start at the first hole ending after the rover, wrap once.
    first = next((i for i, (a, s) in enumerate(holes) if a + s > rover), 0)
    if not fits:
        return None, n
    chosen = min(fits, key=lambda i: (i - first) % n)
    return chosen, (chosen - first) % n + 1


def replay(trace, raw, M, policy, uniforms, p_bf, theta=0.7, phi=0.2, window=50,
           warmup=1000, hysteresis=False):
    owner = np.full(M, -1, dtype=np.int32)
    pending, live, rover = [], {}, 0
    mode = policy if policy in POLICIES[:4] else "FF"
    attempts = 0

    with trace.open() as tf, raw.open() as rf:
        log = csv.DictReader(rf)
        for k, req in enumerate(csv.DictReader(tf)):
            row = next(log)
            size, life = int(req["size"]), int(req["lifetime"])

            while pending and pending[0][0] <= k:
                _, pid = heapq.heappop(pending)
                start, length = live.pop(pid)
                assert (owner[start:start + length] == pid).all()
                owner[start:start + length] = -1

            holes = holes_of(owner)
            T = sum(s for _, s in holes)
            L = max((s for _, s in holes), default=0)

            selector = 0
            if k % window == 0:
                if policy == "Adaptive":
                    selector = len(holes)
                    threshold = theta - 0.1 if hysteresis and mode == "BF" else theta
                    mode = "BF" if T == 0 or 1 - L / T >= threshold or T / M < phi else "FF"
                elif policy == "RandomSwitch":
                    mode = "BF" if uniforms[k // window] < p_bf else "FF"

            start, probes, reason = -1, 0, "capacity"
            if T >= size:
                index, probes = choose(holes, size, mode, rover)
                reason = "fragmentation"
                if index is not None:
                    start, reason = holes[index][0], "ok"
                    owner[start:start + size] = k
                    live[k] = (start, size)
                    heapq.heappush(pending, (k + life, k))
                    if mode == "NF":
                        rover = start + size

            after = holes_of(owner)
            T_after = sum(s for _, s in after)
            expected = {"success": int(reason == "ok"), "reason": reason, "start": start,
                        "search_probes": probes, "selector_probes": selector, "mode": mode,
                        "free_total": T_after, "largest_free": max((s for _, s in after), default=0),
                        "n_holes": len(after), "live_units": M - T_after, "warmup": int(k < warmup)}
            for field in FIELDS:
                if str(expected[field]) != row[field]:
                    sys.exit(f"MISMATCH {trace.name} {policy} attempt {k} {field}: "
                             f"reference {expected[field]}, C {row[field]}")
            attempts += 1
        assert next(log, None) is None
    return attempts


def main():
    config = json.loads((ROOT / "configs" / "experiment.json").read_text())
    with (ROOT / "results" / "summary" / "all_runs.csv").open() as f:
        summary = {(r["trace_id"], r["algorithm"]): r for r in csv.DictReader(f)}

    checked = 0
    for rho in config["loads"]:
        tid = f"load{round(rho * 100)}_{config['M'] // 1024}mb_seed101"
        uniforms = np.loadtxt(ROOT / "workloads" / "random_draws_seed101.csv")
        p_bf = float(summary[tid, "Adaptive"]["bf_share"])
        for policy in POLICIES:
            checked += replay(ROOT / "workloads" / f"{tid}.csv",
                              ROOT / "results" / "raw" / f"{tid}_{policy}.csv",
                              config["M"], policy, uniforms, p_bf,
                              config["theta"], config["phi"])
        print(f"load {rho}: all policies match", flush=True)

    rng = random.Random(873467)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for case, (window, hysteresis) in enumerate([(1, False), (7, True), (50, False), (7, False), (50, True)]):
            n, M = 2000, 256
            trace, ufile, raw = tmp / "t.csv", tmp / "u.csv", tmp / "r.csv"
            with trace.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["pid", "arrival", "size", "lifetime", "size_class", "phase"])
                w.writerows([k, k, rng.randint(1, 96), rng.randint(1, 40), "x", 0] for k in range(n))
            uniforms = np.random.default_rng(8491 + case).random((n + window - 1) // window)
            np.savetxt(ufile, uniforms, fmt="%.17g")
            for policy in POLICIES:
                subprocess.run([str(ROOT / "build" / "simulator"), "--trace", str(trace),
                                "--uniforms", str(ufile), "--policy", policy, "--memory", str(M),
                                "--requests", str(n), "--warmup", "0", "--window", str(window),
                                "--theta", "0.7", "--phi", "0.2", "--hysteresis", str(int(hysteresis)),
                                "--p-bf", "0.37", "--raw", str(raw), "--check"],
                               check=True, capture_output=True)
                checked += replay(trace, raw, M, policy, uniforms, 0.37, window=window,
                                  warmup=0, hysteresis=hysteresis)
            print(f"stress case {case} (W={window}, hysteresis={hysteresis}): all policies match", flush=True)

    print(f"PASS: {checked} attempts identical to the reference model")


if __name__ == "__main__":
    main()
