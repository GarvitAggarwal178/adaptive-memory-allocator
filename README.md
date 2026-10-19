# Adaptive memory allocation: CS4 case study

Operating Systems case study CS4, VIT Vellore.

**Question.** Can an allocator that switches between First Fit and Best Fit,
based on the current state of the free list, reduce fragmentation and
allocation failures compared with the four standard fit strategies?

**Short answer.** It cuts failures roughly in half compared with First Fit at
offered loads of 0.80 and 0.95, but it is no better than plain Best Fit, and no
better than switching between FF and BF at random with the same proportions.
At high load it uses Best Fit almost all the time.

## What is simulated

- One contiguous region of M = 16,384 units (1 unit = 1 KiB).
- Free holes are kept in an address-ordered array. A request takes exactly its
  size from the low end of the chosen hole; freed blocks merge with free
  neighbours immediately. No compaction, no alignment, no headers, so internal
  fragmentation is zero and the study measures external fragmentation only.
- Policies: First Fit, Best Fit, Worst Fit, Next Fit, Adaptive, and a random
  FF/BF control.
- **Adaptive rule.** Every 50 requests, scan the free list once to find the
  largest hole L. With T = total free memory, compute F = 1 - L/T. Use Best Fit
  if F >= 0.70 or T/M < 0.20, otherwise First Fit. The scan is charged to the
  allocator as search cost.
- **Random control.** Switches between FF and BF at random every 50 requests,
  using the same BF share that Adaptive used on the same trace. If Adaptive is
  only good because it uses BF often, the control will do just as well.
- A request fails as *capacity* if T is smaller than the request, and as
  *fragmentation* if T is enough but no single hole is.

## Experiment

- 10,000 requests per trace, one per tick. Sizes: 16-64 KiB (50%), 65-160 KiB
  (35%), 161-320 KiB (15%). Lifetimes are exponential, scaled so the expected
  live demand is 0.60, 0.80 or 0.95 of memory (the *offered load*).
- The first 1,000 requests are warm-up and are not measured.
- Thresholds (0.70, 0.20) were picked on pilot seeds 1-5 and then frozen.
  Final results use seeds 101-120: 3 loads x 20 seeds x 6 policies = 360 runs.
  Every policy replays exactly the same trace files.
- Search cost is counted in probes (holes examined). Wall-clock time per
  allocation call is also recorded but is noisy at this scale (~25 ns of the
  measured time is the clock itself).

## Layout

```
src/            allocator.c/.h (all policies), simulator.c (trace replay), check.h
tests/          test_allocator.c (unit tests), test_pipeline.py (generator and event rules),
                reference_model.py (cross-check against a per-unit memory map)
examples/       demo.c: the worked three-hole example from the report
tools/          clock_overhead.c: cost of an empty timed interval
scripts/        workload.py (trace generator), experiment.py (pilot / final runs),
                analyse.py (tables, intervals, figures), audit_raw.py (recompute from raw logs)
configs/        experiment.json: frozen settings written by the pilot
workloads/      input traces (.csv), their metadata (.json), random-control draws
results/raw/    one per-request log per run: load<L>_16mb_seed<S>_<policy>.csv
results/summary all_runs.csv (one row per run) and derived summaries
results/tables  the tables used in the report
results/figures the figures used in the report
```

## Reproducing

Needs gcc, make and Python 3.10+ with the packages in `requirements.txt`.
Linux or WSL2.

```
make test                                  # C unit tests
python3 tests/test_pipeline.py             # generator and event-order tests
python3 scripts/experiment.py pilot        # ~20 s: chooses M and thresholds, writes configs/experiment.json
python3 scripts/experiment.py final        # 360 runs, writes results/raw and results/summary/all_runs.csv
python3 scripts/experiment.py environment  # records CPU, compiler and clock overhead
python3 scripts/analyse.py                 # tables and figures
python3 scripts/audit_raw.py               # recomputes every summary number from the raw logs
python3 tests/reference_model.py           # cross-checks 18 final runs + 30 random cases
make demo                                  # prints the worked example
```

Traces are generated from fixed seeds, so the pilot and final stages
reproduce the same traces and the same allocation decisions on any machine.
Only the timing columns change.

## AI assistance

The code and scripts in this repository were written with help from AI coding
assistants. All results come from running this code; the team reviewed it and
takes responsibility for it.
