# No performance-measurement infrastructure/baseline despite stated 150-205 repo scale
kind: idea
tags: perf
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity MEDIUM (recommend-defer). Source: review-panel-2026-05-29 PERF-F3. Verified outstanding 2026-06-08: pyproject.toml has only pytest + pytest-cov; no pytest-benchmark, benchmarks/, or phase timing. Without a baseline there is no way to confirm the PERF-F1/F2 fixes helped or to catch an O(n^2) regression. Fix: perf_counter timing around the 3 pipeline phases into the manifest + one pytest-benchmark over a synthetic 200-repo RunState loop.
