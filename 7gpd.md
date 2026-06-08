# RunState rewrites all of state.yaml on every record_repo_state/record_extra (O(n^2) write amplification)
kind: debt
tags: perf
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity HIGH (perf). Source: review-panel-2026-05-29 PERF-F1. Verified outstanding 2026-06-08. runstate.py:171/183/197 each call _rewrite_state() (line 199) which yaml.safe_dump's the whole {repos:...} dict + atomic tmp+os.replace on EVERY per-repo update. For 150-205 repos total work is proportional to N^2 plus N fsync-class writes. Fix: accumulate in self._per_repo (already present) and write state.yaml ONCE in complete() (or once per phase); crash-resilience preserved by the begin() empty write + final write.
- 2026-06-08T22:03Z Fixed 2026-06-08 on branch perf/7gpd-37ic-5agg-cluster. record_repo_state/set_repos/record_extra now only accumulate in memory + mark a dirty flag; the single state.yaml write happens in new flush_state(), called from complete() (or explicitly at a phase boundary). O(n^2) full-file rewrites -> O(n) single write at 150-205 repo scale. Crash-resilience preserved: begin() empty write keeps the file parseable, and the mutating-action audit is appended live to errors.log/invariants.log (state.yaml is only the post-action per-repo summary). this.i node kp7nw4mq.c updated with the UPDATE+tension resolution. 1964 tests green, 100% cov.
