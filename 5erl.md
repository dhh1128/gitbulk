# retain_runs retention default (30) is undocumented in gitbulk.yaml.example with no retention rationale
kind: todo
tags: docs
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity LOW/MEDIUM. Source: review-panel-2026-05-29 CMP-F2 + DEV-F2 (same root). Verified outstanding 2026-06-08: config/policy.py:79 sets retain_runs=30 but config/gitbulk.yaml.example does not mention it, and the rationale (disk-bounding vs audit-retention window) is undocumented. For a nightly cron this silently ages out the record of destructive remote actions after ~30 runs. Fix: document the knob + its retention rationale in the example config (and decide/record whether a 12-month audit expectation applies).
