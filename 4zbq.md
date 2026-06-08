# CI workflow lacks a concurrency group; rapid PR pushes run redundant full-matrix jobs
kind: todo
tags: ci
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity LOW. Source: review-panel-2026-05-29 DEV-F4 (license half already DONE — Apache-2.0 set). Verified outstanding 2026-06-08: .github/workflows/ci.yml has no concurrency: key. Fix: add a concurrency group keyed on workflow+ref with cancel-in-progress.
