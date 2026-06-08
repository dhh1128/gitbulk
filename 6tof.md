# Dispatch persists per-agent stdout/stderr under the run dir with no documented data-classification/lifecycle
kind: todo
tags: compliance
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity LOW (recommend-defer). Source: review-panel-2026-05-29 CMP-F5. Verified outstanding 2026-06-08: dispatch.py + exec.py persist each headless agent's stdout/stderr under the run dir; can mirror diffs/file contents/prompts. Owner-only (umask 0o077) and pruned by retain_runs, but nothing classifies the contents/lifecycle. Fix: a one-line data-classification note in docs.
