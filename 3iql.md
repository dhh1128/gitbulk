# Trojan-Source Unicode gate excludes tests/, but test code runs in CI (deferred)
kind: todo
tags: security
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity LOW (DEFERRED). Source: review-panel-review (2026-06-05) SEC-F4 — the one 06-05 finding not merged. scripts/check_unicode.py:57 DEFAULT_ROOTS omits tests/; CI runs the gate with no args so tests/ is never scanned, yet tests/ is executed via pytest and defines the doubles the 100% coverage gate relies on. Deliberately deferred: enabling now risks flagging intentional non-ASCII test fixtures. REVISIT CONDITION: before open-sourcing / accepting external PRs that touch tests/.
