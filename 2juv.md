# No automatic orphan-worktree sweep at run-start / finally teardown (manual prune-worktrees only)
kind: todo
tags: worktree
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity HIGH originally, now MITIGATED by the manual 'gitbulk prune-worktrees' subcommand. Source: review-panel-2026-05-29 DEV-F1. Verified partial 2026-06-08: gc.py still documents sweep_orphan_worktrees as 'deferred to Phase 4+' (gc.py:12-13); dispatch teardown lives in the result loop, not a finally. A SIGKILL/OOM/crash during the Claude pool strands a checkout tree until the next manual prune-worktrees run. Fix: implement run-start sweep_orphan_worktrees and/or move teardown into a finally.
