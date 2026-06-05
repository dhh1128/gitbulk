# Testability Review: gitbulk

**Date:** 2026-06-05
**Effort level:** medium
**Run label:** review
**Context sources used:** AGENTS.md, this.i, pyproject.toml, .github/workflows/ci.yml, src/gitbulk/ (all production modules), tests/ (all test files), docs/architecture.md (not read — not critical for this review level)

---

## Evidence Inventory

Files read:
- `AGENTS.md` — TDD discipline, 100% branch coverage mandate, no-network rule, CI description
- `this.i` — nodes cn4pk7zq (coverage standard), agtste9k (e2e tier), cidvp4kr (CI matrix)
- `pyproject.toml` — test config, markers, coverage flags
- `.github/workflows/ci.yml` — CI pipeline structure
- `src/gitbulk/commands/merge.py`, `prune_branches.py`, `prune_worktrees.py`, `close_stale.py`, `dispatch.py`
- `src/gitbulk/invariants/catalog.py` — `_utc_now` clock injection point
- `src/gitbulk/util/parallel.py` — thread pool primitive
- `src/gitbulk/gh.py` — `FakeGHClient` (primary test double)
- `src/gitbulk/worktree.py`
- `tests/test_merge.py`, `tests/test_prune_branches.py`, `tests/test_prune_worktrees.py`, `tests/test_parallel.py` (full scan)
- Selected tests: `test_dispatch.py`, `test_close_stale.py`, `test_rebase.py`

What was skipped: tests were not actually executed (no coverage data from this run).
Platform context from `../origin-platform/docs/` was checked for existence — available.
This is a **CLI tool / library**, not a Spring/Python service; the Java-specific layers (Spring MVC slices, JPA) do not apply. Python-equivalent coverage is the metric.

---

## Executive Summary

gitbulk has a disciplined, well-structured test suite with a strong boundary: `FakeGHClient` provides a complete, protocol-faithful double for all GitHub network calls and is the only legitimate test double for that surface. The 100%-branch-coverage CI gate, clock-injection `_utc_now()` helpers, and the `e2e` tier for real-binary sandbox tests show thoughtful testability design. The most significant structural gap is fixture duplication: `isolated_xdg`, `code_root`, `write_config`, and `fresh_org_cache` are copy-pasted verbatim across five test files with subtle per-file variations; future divergence will cause invisible coverage asymmetry and make it easy to miss updating all copies. The most urgent fix is addressing the `test_parallel_scan_surfaces_all_candidates_in_repo_order` flakiness risk: the test implicitly relies on alphabetical slug ordering in repos.txt matching the sorted plan output, creating a hidden ordering assumption that will fail silently if repos.txt ever uses a non-sorted order.

---

## Top Findings

### F1: Parallel-scan order test relies on implicit alphabetical slug coincidence

- **Severity:** HIGH
- **Confidence:** CONFIRMED
- **Location:** `tests/test_prune_branches.py:459-490`
- **Finding:** `test_parallel_scan_surfaces_all_candidates_in_repo_order` creates slugs `dhh1128/r0` through `dhh1128/r3` written to repos.txt in that exact order, then asserts their positions in the summary are ascending. This passes because `_merge_plan` / `_flatten_plan` outputs rows **sorted alphabetically by slug**, and `r0 < r1 < r2 < r3` alphabetically happens to match the repos.txt order the test uses. If someone writes a future parallel-scan test (or extends this one) with repos that are NOT in alphabetical order in repos.txt — e.g. `["dhh1128/r3", "dhh1128/r1", "dhh1128/r0"]` — the test would verify alphabetical order, not repos.txt order. The docstring explicitly claims "repos.txt order" which is not what is actually tested.
- **Consequence:** A regression in `_flatten_plan`'s sorting key could go undetected. More importantly, the test creates false confidence that the parallel scan preserves a specific ordering semantic. Any refactor that changes the plan sort order (e.g., to timestamp-recency order) would still pass this test as long as the slugs remain alphabetically ordered — the test would not catch the regression.
- **Recommendation:** Either (a) use non-alphabetical slug names in the test (e.g., `["dhh1128/repo-b", "dhh1128/repo-a", "dhh1128/repo-c"]`) and assert they appear in repos.txt order in the summary, or (b) explicitly document that the assertion checks alphabetical-by-slug order and rename the test accordingly.

---

### F2: Core handler-level fixtures are copy-pasted five times with invisible drift risk

- **Severity:** HIGH
- **Confidence:** CONFIRMED
- **Location:** `tests/test_merge.py:47-109`, `tests/test_prune_branches.py:36-83`, `tests/test_prune_worktrees.py:38-90`, `tests/test_close_stale.py:40-92`, `tests/test_dispatch.py:54-157`
- **Finding:** The four foundational test fixtures — `isolated_xdg`, `code_root`, `write_config`, and `fresh_org_cache` — are duplicated verbatim (or near-verbatim) across five test files. Each copy already has minor differences (e.g., `write_config` in `test_merge.py` defaults `min_business_days=0` and materializes clone directories; the `test_prune_branches.py` copy uses a different default set in `defaults`). There is no shared `conftest.py`. Per AGENTS.md, the coverage standard is 100% branch coverage; but if one copy of `write_config` omits a config key that is only checked in certain code paths, tests for that module will silently under-exercise branches that other modules' write_config variants happen to cover.
- **Consequence:** A new policy field that requires a default will be added to one copy of `write_config` but forgotten in another, causing spurious test failures or — worse — silent coverage gaps that only appear as real bugs in cron runs. The maintainability burden of keeping five copies in sync is high and grows with each new config key.
- **Recommendation:** Extract the common fixtures into `tests/conftest.py`. Allow per-file overrides for the cases that legitimately differ (e.g., `min_business_days=0` default for merge tests). This is a medium-effort refactor that eliminates the class of "works in merge tests but breaks in dispatch tests" gap permanently.

---

### F3: `_open_pr()` test-data helper uses `datetime.now()` with a hardcoded `NOW` constant creating a time-mixing anti-pattern

- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `tests/test_prune_branches.py:103-113`, `tests/test_prune_worktrees.py:127-135`
- **Finding:** The `_open_pr()` helper in both test files uses `datetime.now(timezone.utc)` for `created_at` and `updated_at`, while the unit-test-level `_classify_*` calls pass a hardcoded `NOW = datetime(2026, 6, 3, tzinfo=timezone.utc)` as the `now` argument. The two time bases are different: `_open_pr` is wall-clock; `NOW` is pinned. In `test_prune_branches.py`, this is harmless today because `_classify_branch` does not use the PR's `created_at`/`updated_at` (it uses `closed_at` from `ClosedPRRef`). But the mixing is a code smell that could silently break future `_classify_*` tests if a new guard compares `now` against `pr.created_at` or `pr.updated_at`. The `_closed()` helper also uses `datetime.now()` with `days_ago` offset, which is then compared against the hardcoded `NOW` constant. Because `closed_at = actual_now - N days` and `age_days = (NOW - closed_at).days`, any run after 2026-06-03 makes `closed_at` MORE recent than `NOW`, producing a negative `age_days` which always passes the `< grace` check — so the "within grace period" tests remain green but for the wrong reason as time advances.
- **Consequence:** Tests claiming to verify "within the grace period" behavior will continue to pass indefinitely not because the grace logic is correct but because `age_days` is negative. A future test that tries to verify "just outside the grace period" with `days_ago=8` against `prune_min_age_days=7` would fail in the same way. The hidden mixing of `datetime.now()` and hardcoded `NOW` constants makes test intent opaque.
- **Recommendation:** Replace `datetime.now(timezone.utc)` in `_open_pr()` and `_closed()` with the `NOW` constant (or a parameterized clock anchor). This makes the time base uniform and the test intent explicit. Tests that specifically need wall-clock behavior should use `monkeypatch.setattr(pw, "_utc_now", lambda: NOW)` consistently.

---

### F4: Handler-level tests for `prune_worktrees` do not inject `_utc_now` — grace period is always wall-clock

- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `tests/test_prune_worktrees.py:270-330`, `src/gitbulk/commands/prune_worktrees.py:334`
- **Finding:** The `prune_worktrees` handler calls `_utc_now()` at line 334 and passes the result into `_classify_worktree`. The unit-level `_classify_worktree` tests correctly use the hardcoded `NOW` constant. However, the handler-level integration tests (e.g., `test_dry_run_lists_candidate`, `test_apply_removes_worktree_and_deletes_branch`) do NOT monkeypatch `pw._utc_now`. Their `_closed()` helper uses `days_ago=30` with real wall-clock, so the computed `age_days` is always 30 regardless of when the test runs — this is robust. But there is no handler-level test that exercises the grace-period boundary at the handler level. The unit tests verify `_classify_worktree` receives the right `now` and makes the right decision; but the handler's wiring of `_utc_now()` into the classify call has no test that would catch a regression where the handler passes a stale timestamp or the wrong module's clock.
- **Consequence:** A regression where the handler passes `now = datetime(1970, 1, 1)` (a bug in clock wiring) would not be caught by any test, because all handler tests use `days_ago=30` which passes regardless of what `now` is. The grace-period boundary behavior is only verified at the unit level, not end-to-end.
- **Recommendation:** Add one handler-level test that monkeypatches `pw._utc_now` to a known value (e.g., `NOW`) and uses `_closed(..., days_ago=2)` built against that same `NOW` to verify the grace-period skip fires at the handler level. This closes the gap between the unit-level classification test and the handler wiring.

---

### F5: `_partition_chain` helper is copy-pasted in four command modules with no shared test

- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/commands/merge.py:86`, `src/gitbulk/commands/dispatch.py:142`, `src/gitbulk/commands/prune_branches.py:79`, `src/gitbulk/commands/rebase_pr.py:97`
- **Finding:** The `_partition_chain` function (splits an invariant name list into UNIVERSAL / PER_REPO / PER_PR buckets) is duplicated four times. Each copy is independently tested only implicitly through the full handler pipeline. There is no shared test for the function itself; more critically, since each copy is private to its module, a mutation bug in one copy (e.g., an off-by-one or a wrong kind comparison) would only be caught by a handler test that happens to exercise the specific kind that is broken. The `merge.py` docstring says "Identical to the helper in dispatch.py / report.py; kept local so the merge handler stays standalone" — the design is intentional, but the testability cost is not fully accounted for.
- **Consequence:** A future invariant kind addition (e.g., `PER_FLEET`) would need to be added to all four copies consistently. A copy-paste error in one module's `_partition_chain` could silently skip a new invariant kind, producing a false Pass in that subcommand's pipeline.
- **Recommendation:** Extract `_partition_chain` to a shared module (e.g., `gitbulk.invariants.base` or a new `gitbulk.commands._shared`) with a single shared test covering all three buckets. The "standalone handler" goal can be preserved by having the module import from the shared location without introducing a circular dependency.

---

## Additional Patterns Noted

- **`test_prune_branches.py:488`: Summary order test conflates alphabetical slug order with repos.txt order** (covered above as F1, detailed here for completeness). The summary's ordering comes from `_flatten_plan` which calls `sorted(merged)` on the plan dict keys, not from repos.txt order. The test works today because the slugs chosen are naturally alphabetical.

- **`test_merge.py:126`: `_make_pr` uses `datetime.now(timezone.utc) - timedelta(days=14)` for `last_pushed_at`**. This is safe because `write_config` defaults `min_business_days=0`, bypassing the age gate entirely. Tests that set `min_business_days > 0` correctly monkeypatch `_catalog._utc_now`. The risk is low but the pattern is easy to misuse in future tests.

- **`tests/conftest.py` is absent**. A single `conftest.py` would eliminate fixture duplication (F2) and provide a natural home for test utilities shared across files (`_make_pr`, `_open_pr`, `_closed`). Currently each test file independently defines its own versions of these helpers, including subtle differences (e.g., `_open_pr` in `test_prune_branches.py` sets `review_decision=None`, while `_open_pr` in `test_prune_worktrees.py` also sets `review_decision=None` but also omits `checks_status`).

- **`FakeGHClient.prefetch_default_branches` no-ops and fires `on_progress` only once**, while `ProductionGHClient.prefetch_default_branches` fires it once per chunk. Tests that assert on progress-callback call counts would pass in the fake but fail against real behavior. Currently no test asserts on the progress callback count, so this is theoretical.

- **`test_prune_branches.py:916`: `test_second_dry_run_reuses_fresh_repo` relies on the implicit fact that no time passes between two `prune_branches_handler` calls** within the same test, so the fresh-plan check sees an `analyzed_at` within the default 12-hour window. This is safe today but would need a clock injection if the default window were ever tightened below the test execution time (~seconds).

- **`dispatch.py:253` has `except Exception: # pragma: no cover - defensive`**. This is a documented coverage deviation without a `deviation:` node in `this.i`. Per AGENTS.md, "a gap without one is a defect." However, the comment explains it is defensive code (a cleanup path that should never fail in practice), which is the right spirit for a deviation. A `deviation:` node would formalize the approved exemption.

- **No BDD-style test names for the `test_parallel_*` tests in `test_prune_branches.py`**. These tests describe implementation mechanics ("parallel scan surfaces candidates") rather than user-visible behavior ("branches that pass all guardrails appear in the dry-run report regardless of scan concurrency"). The existing names are clear enough for a CLI tool, but do not follow the `given/when/then` convention suggested by the persona instructions. Since this is a personal tool, this is LOW severity.

- **`test_parallel_apply_deletes_every_candidate` asserts on a set** (`{c["slug"] for c in fake.delete_branch_calls} == set(slugs)`) which correctly avoids ordering sensitivity. The comment `# delete_branch_calls.append is atomic in CPython` is accurate and appropriate; this is a good pattern.

---

## Residual Unknowns

- **Coverage report not run**: The actual branch coverage report was not generated during this review. The CI gate claims 100%, but without running `pytest --cov-branch`, there may be branches excluded by `# pragma: no cover` comments beyond those listed in `exec.py`. A full `pytest --cov-report=term-missing` run would confirm the gate is tight.
- **`test_dispatch.py`** (2382 lines) was not read in full; only the fixture definitions and structure were sampled. Dispatch is the most complex command; potential test gaps there (particularly around the sandboxed-clone path) were not fully audited.
- **E2E tests** (`tests/e2e/test_sandbox_e2e.py`) were not read. The `agtste9k` decision in `this.i` records that these exist to cover the bubblewrap sandbox; their adequacy was not verified.
- **`test_style.py`, `test_subcommands.py`**: Not examined; assumed to be focused unit tests for their respective modules.

---

## Decisions Needed

1. **Conftest extraction (F2)**: Should `isolated_xdg`, `code_root`, `write_config`, and `fresh_org_cache` be moved to `tests/conftest.py`? The per-file variants differ slightly (e.g., `min_business_days=0` default in merge tests). A decision is needed on whether the conftest version uses a single opinionated default or accepts a `defaults_extra` parameter like the current per-file versions.

2. **`_partition_chain` deduplication (F5)**: The current "standalone handler" design is intentional (noted in comments). If extracting to a shared location is rejected, a `deviation:` node in `this.i` should record the deliberate choice to accept this duplication and its testability tradeoff.

3. **`dispatch.py:253` pragma: no cover (additional patterns)**: Requires a `deviation:` node in `this.i` per the coverage standard (node `cn4pk7zq`). Whether this is an explicit design choice or an oversight should be clarified.

---

## Findings Manifest

```yaml
findings:
  - id: TST-F1
    persona: testability-hawk
    title: Parallel-scan order test relies on implicit alphabetical slug coincidence
    severity: HIGH
    confidence: CONFIRMED
    location: tests/test_prune_branches.py:459-490
    dedupe_key: prune-branches-scan-flaky
    recommended_disposition: recommend-fix
    rationale: Test asserts repos.txt order but actually tests alphabetical-slug order; a non-alphabetical repos.txt would reveal the mismatch silently.
    revisit_condition: null
    fix_effort: small

  - id: TST-F2
    persona: testability-hawk
    title: Core handler fixtures duplicated across five test files with no conftest.py
    severity: HIGH
    confidence: CONFIRMED
    location: tests/test_merge.py:47, tests/test_prune_branches.py:36, tests/test_prune_worktrees.py:38, tests/test_close_stale.py:40, tests/test_dispatch.py:54
    dedupe_key: tests-fixtures-duplicated
    recommended_disposition: recommend-fix
    rationale: Five copies of isolated_xdg/code_root/write_config/fresh_org_cache drift independently; new config keys added to one copy silently miss others, producing invisible coverage asymmetry.
    revisit_condition: null
    fix_effort: medium

  - id: TST-F3
    persona: testability-hawk
    title: _open_pr/_closed helpers mix datetime.now() with hardcoded NOW constant
    severity: MEDIUM
    confidence: CONFIRMED
    location: tests/test_prune_branches.py:103-121, tests/test_prune_worktrees.py:119-135
    dedupe_key: tests-time-untested
    recommended_disposition: recommend-fix
    rationale: Time-mixing makes grace-period tests pass for the wrong reason (negative age_days) after 2026-06-03; intent is opaque and future time-sensitive tests will silently misfire.
    revisit_condition: null
    fix_effort: small

  - id: TST-F4
    persona: testability-hawk
    title: prune_worktrees handler-level tests do not inject clock — grace period boundary untested end-to-end
    severity: MEDIUM
    confidence: CONFIRMED
    location: tests/test_prune_worktrees.py:270-330, src/gitbulk/commands/prune_worktrees.py:334
    dedupe_key: prune-worktrees-untested
    recommended_disposition: recommend-fix
    rationale: Unit tests verify _classify_worktree grace logic correctly but the handler's _utc_now() wiring has no integration test; a clock-wiring regression would go undetected.
    revisit_condition: null
    fix_effort: small

  - id: TST-F5
    persona: testability-hawk
    title: _partition_chain helper copy-pasted in four command modules with no shared test
    severity: MEDIUM
    confidence: CONFIRMED
    location: src/gitbulk/commands/merge.py:86, src/gitbulk/commands/dispatch.py:142, src/gitbulk/commands/prune_branches.py:79, src/gitbulk/commands/rebase_pr.py:97
    dedupe_key: commands-partition-chain-duplicated
    recommended_disposition: recommend-defer
    rationale: Four independent copies of the same function create a future-invariant-kind regression risk; however the existing copies are simple and the isolation rationale is documented; defer until a new invariant kind is added.
    revisit_condition: When a new InvariantKind is added, or when a bug is found in one copy but not others.
    fix_effort: medium
```
