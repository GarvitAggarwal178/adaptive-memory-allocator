"""Checks of the trace generator and the simulator's event rules (python3 tests/test_pipeline.py)."""
from pathlib import Path
import csv
import json
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import workload  # noqa: E402

SIMULATOR = ROOT / "build" / "simulator"
HEADER = ["pid", "arrival", "size", "lifetime", "size_class", "phase"]


def simulate(rows, policy="FF", memory=10, warmup=1, window=50, p_bf=0.0):
    """Run the simulator in check mode on a hand-written trace; return (summary, raw rows)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        trace, uniforms, raw = tmp / "trace.csv", tmp / "u.csv", tmp / "raw.csv"
        with trace.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            w.writerows(rows)
        np.savetxt(uniforms, np.random.default_rng(100).random(len(rows)), fmt="%.17g")
        out = subprocess.run(
            [str(SIMULATOR), "--trace", str(trace), "--uniforms", str(uniforms),
             "--policy", policy, "--memory", str(memory), "--requests", str(len(rows)),
             "--warmup", str(warmup), "--window", str(window), "--p-bf", str(p_bf),
             "--raw", str(raw), "--check"],
            capture_output=True, text=True, check=True)
        with raw.open() as f:
            return json.loads(out.stdout), list(csv.DictReader(f))


class GeneratorTests(unittest.TestCase):
    def test_same_seed_same_bytes(self):
        meta = workload.generate(4096, 0.8, 999)
        path = workload.WORKLOADS / f"{meta['trace_id']}.csv"
        first = path.read_bytes()
        workload.generate(4096, 0.8, 999)
        self.assertEqual(first, path.read_bytes())

        with path.open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), workload.N_REQUESTS)
        self.assertTrue(all(16 <= int(r["size"]) <= 320 and int(r["lifetime"]) >= 1 for r in rows))
        self.assertAlmostEqual(workload.MEAN_SIZE, 95.45)
        for suffix in (".csv", ".json"):
            path.with_suffix(suffix).unlink()
        (workload.WORKLOADS / "random_draws_seed999.csv").unlink()


class EventRuleTests(unittest.TestCase):
    def test_release_happens_before_arrival_and_failures_are_dropped(self):
        # M = 10. t0: 10 units for 1 tick. t1: released first, so 10 more fit.
        # t2: 11 > M is a capacity failure. t3: memory still full -> capacity failure.
        _, raw = simulate([[0, 0, 10, 1, "s", 0], [1, 1, 10, 10, "s", 0],
                           [2, 2, 11, 1, "s", 0], [3, 3, 1, 2, "s", 0]])
        self.assertEqual([r["success"] for r in raw], ["1", "1", "0", "0"])
        self.assertEqual([r["warmup"] for r in raw], ["1", "0", "0", "0"])
        self.assertEqual(raw[2]["reason"], "capacity")
        self.assertTrue(all(r["elapsed_ns"] == "" for r in raw))   # no timing in check mode

    def test_tied_releases_free_both_blocks_before_the_arrival(self):
        # Both blocks are due at t2, so the request for all 10 units succeeds at address 0.
        _, raw = simulate([[0, 0, 4, 2, "s", 0], [1, 1, 6, 1, "s", 0], [2, 2, 10, 1, "s", 0]])
        self.assertEqual((raw[2]["success"], raw[2]["start"]), ("1", "0"))

    def test_random_control_uses_requested_share(self):
        rows = [[k, k, 1, 1, "s", 0] for k in range(10_000)]
        summary, _ = simulate(rows, policy="RandomSwitch", memory=1, warmup=0, window=1, p_bf=0.37)
        self.assertLess(abs(summary["bf_share"] - 0.37), 0.02)


if __name__ == "__main__":
    unittest.main(verbosity=2)
