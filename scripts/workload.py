"""Synthetic allocation traces.

One request arrives per tick. Request size and lifetime are drawn
independently:

  size class   probability   size range (KiB, uniform integers)
  small        0.50          16 - 64
  medium       0.35          65 - 160
  large        0.15          161 - 320

  E[size] = 0.5*40 + 0.35*112.5 + 0.15*240.5 = 95.45 KiB

Lifetime = max(1, ceil(Exponential(scale))). Because ceil adds about 0.5 tick
on average, scale = rho*M/E[size] - 0.5 gives an expected live demand of about
rho*M, i.e. an offered load rho (Little's law with arrival rate 1).
"""
from pathlib import Path
import csv
import hashlib
import json

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ROOT / "workloads"

SIZE_CLASSES = ["small", "medium", "large"]
CLASS_PROB = [0.50, 0.35, 0.15]
CLASS_LOW = np.array([16, 65, 161])
CLASS_HIGH = np.array([65, 161, 321])          # exclusive upper bounds
MEAN_SIZE = 0.50 * 40 + 0.35 * 112.5 + 0.15 * 240.5

N_REQUESTS = 10_000
WARMUP = 1_000
WINDOW = 50


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def trace_id(memory, rho, seed):
    return f"load{round(rho * 100)}_{memory // 1024}mb_seed{seed}"


def generate(memory, rho, seed, n=N_REQUESTS):
    """Write workloads/<id>.csv, its .json metadata and the random-control draws."""
    WORKLOADS.mkdir(exist_ok=True)
    tid = trace_id(memory, rho, seed)
    path = WORKLOADS / f"{tid}.csv"

    rng = np.random.default_rng(seed)
    classes = rng.choice(3, n, p=CLASS_PROB)
    sizes = rng.integers(CLASS_LOW[classes], CLASS_HIGH[classes])
    scale = rho * memory / MEAN_SIZE - 0.5
    lifetimes = np.maximum(1, np.ceil(rng.exponential(scale, n))).astype(int)

    with path.open("w", newline="") as f:
        out = csv.writer(f, lineterminator="\n")
        out.writerow(["pid", "arrival", "size", "lifetime", "size_class", "phase"])
        for k, (size, life, c) in enumerate(zip(sizes, lifetimes, classes)):
            out.writerow([k, k, int(size), int(life), SIZE_CLASSES[c], 0])

    realized = float(np.mean(sizes * lifetimes) / memory)
    meta = {
        "trace_id": tid,
        "M": memory,
        "rho_target": rho,
        "seed": seed,
        "arrivals": n,
        "warmup": WARMUP,
        "expected_size": MEAN_SIZE,
        "exponential_scale": scale,
        "realized_mean_size": float(sizes.mean()),
        "realized_mean_lifetime": float(lifetimes.mean()),
        "realized_offered_load": realized,
        "within_2_percent_of_target": abs(realized / rho - 1) <= 0.02,
        "trace_sha256": sha256(path),
    }
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n")

    # One uniform draw per decision window, used only by the RandomSwitch control.
    draws = np.random.default_rng(seed + 10_000).random((n + WINDOW - 1) // WINDOW)
    np.savetxt(WORKLOADS / f"random_draws_seed{seed}.csv", draws, fmt="%.17g")
    return meta
