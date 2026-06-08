# cron wrapper sets last-failure.log on any rc!=0, conflating needs-attention with failure exit codes
kind: debt
tags: cron
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity MINOR. Source: platform-architect-2026-05-27 F4. Verified outstanding 2026-06-08: bin/gitbulk-cron (~line 106-112) symlinks last-failure.log on any rc!=0 without branching on the distinct exit codes (1/2/3/4) the tool returns; the ATTENTION sentinel is the real source of truth, so 'needs attention' and 'failed' get conflated in the log name. Fix: branch on exit code (or rename to last-nonzero.log) so failure vs attention is distinguishable.
