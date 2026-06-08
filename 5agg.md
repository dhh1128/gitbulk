# No performance-measurement infrastructure/baseline despite stated 150-205 repo scale
kind: idea
tags: perf
created: 2026-06-08T19:34Z
closed: 2026-06-08T22:03Z

- 2026-06-08T19:34Z Severity MEDIUM (recommend-defer). Source: review-panel-2026-05-29 PERF-F3. Verified outstanding 2026-06-08: pyproject.toml has only pytest + pytest-cov; no pytest-benchmark, benchmarks/, or phase timing. Without a baseline there is no way to confirm the PERF-F1/F2 fixes helped or to catch an O(n^2) regression. Fix: perf_counter timing around the 3 pipeline phases into the manifest + one pytest-benchmark over a synthetic 200-repo RunState loop.
- 2026-06-08T22:03Z Built 2026-06-08 on branch perf/7gpd-37ic-5agg-cluster. (1) src/gitbulk/util/timing.py PhaseTimer (checkpoint mark() API, injectable clock, perf_counter-backed) + RunState.record_timings() stamping a rounded 'timings' block into manifest.yaml; wired into report's 3 phases (preflight/per_repo/per_pr), recorded on the success path. (2) pytest-benchmark added to test extra + 'benchmark' marker; benchmarks/test_runstate_bench.py exercises a synthetic 200-repo RunState record+flush cycle (outside testpaths, run on demand: pytest benchmarks/ --benchmark-only) - the baseline that proves the 7gpd O(n) fix and catches an O(n^2) regression. Docs updated (architecture.md, reference.md). 100% cov.
