# RunState rewrites all of state.yaml on every record_repo_state/record_extra (O(n^2) write amplification)
kind: debt
tags: perf
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity HIGH (perf). Source: review-panel-2026-05-29 PERF-F1. Verified outstanding 2026-06-08. runstate.py:171/183/197 each call _rewrite_state() (line 199) which yaml.safe_dump's the whole {repos:...} dict + atomic tmp+os.replace on EVERY per-repo update. For 150-205 repos total work is proportional to N^2 plus N fsync-class writes. Fix: accumulate in self._per_repo (already present) and write state.yaml ONCE in complete() (or once per phase); crash-resilience preserved by the begin() empty write + final write.
