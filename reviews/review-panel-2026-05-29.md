# Review-panel synthesis — gitbulk @ main

**Date:** 2026-05-29
**Target:** `/home/daniel/code/gitbulk` branch `main`
**Run:** review-panel workflow (8 adversarial personas, read-only), concurrency 3
**Raw → adjudicated:** 29 raw findings from 8 personas → **28 after dedupe**
**Severity:** 7 HIGH · 11 MEDIUM · 10 LOW
**Disposition:** 15 recommend-fix · 9 recommend-defer · 4 recommend-accept-risk

Per-persona narrative reviews live alongside this file:
`reviews/security-review.md`, `reviews/devops-review.md`,
`reviews/compliance-review.md` (the other five personas — architect,
performance, testability, maintainability, ux — did not write standalone
files; their findings are captured in the table below). The ux-guru
persona returned **zero** findings (a CLI/cron tool with no interactive
surface). dedupe merged SEC-F4 (reported by both security-hawk and
devops-engineer).

---

## Recommended action order

1. **SEC-F1** — git argument-injection → RCE under cron. Security blocker; small fix.
2. **Docs-drift cluster** (MNT-F2, ARC-F2, CMP-F3, MNT-F3, MNT-F5) — one sweep; removes a dangerous lie (docs claim the mutating subcommands are inert exit-99 scaffolds; they are live).
3. **ARC-F1 / ARC-F4** — fork-PR dimension on `PRInfo` + GraphQL; fix the data model once, gate cross-repo PRs.
4. **PERF-F1** — single terminal `state.yaml` write (drop O(n²) per-repo rewrite).
5. **MNT-F1 + CMP-F1** — `this.i`/manifest bookkeeping (repo-lock divergence; audit actor).

Intent-level items (MNT-F1, ARC-F1/F4 fork gap, MNT-F5 unimplemented
safety invariants) are also recorded in `this.i` — see tension nodes
`rlkrcn3p` (repo-lock reconciliation), `frkpr5kq` (fork-PR handling),
and the augmented `ivb5kq3n` (remaining-invariant backlog).

---

## HIGH (7)

### SEC-F1 — PR ref/SHA values reach positional git args without `--` terminator (arg-injection / RCE) · CONFIRMED · recommend-fix · small
`src/gitbulk/rebase.py:100` (also `:107,145-151`; `worktree.py:143-150`)
`dedupe_key: git-ref-args-unsafe`
`pr.base_ref/head_ref/head_sha` (plain str from gh GraphQL `baseRefName/headRefName/headRefOid`, `pr_info.py:131-133`, no validation) flow into `git fetch origin <base_ref>`, `git rebase origin/<base_ref>`, `git worktree add --detach <target> <sha>`, and `--force-with-lease=<head_ref>:<sha>` with no `--` terminator. A `-`-leading ref becomes a git option like `--upload-pack=<cmd>`; empirically `git fetch origin '--upload-pack=touch /tmp/PWNED'` creates the file → RCE during unattended cron. Reachable via threat-model scenario C (swapped/malicious gh) or D (crafted PR data), both in-scope. Fix: insert `--` and/or validate ref segments (reject leading `-`) and SHA (`^[0-9a-f]{7,40}$`) in `_pr_info_from_graphql_node`.

### DEV-F1 — Orphaned-worktree sweep (Track-A jw3kpn4q item 3) never implemented · CONFIRMED · recommend-fix · small
`src/gitbulk/gc.py:13`; `src/gitbulk/commands/dispatch.py:561-648`
`dedupe_key: worktree-orphan-unbounded`
dispatch/rebase-pr create worktrees under `<worktree_root>/<runid>/` but teardown lives in the step-11 post-pass, not a `finally`; the run-start sweep node `jw3kpn4q` scoped to Phase 4 was never built (`sweep_orphan_worktrees` is only a `gc.py` docstring). A SIGKILL/OOM/crash during the long-running Claude pool strands a full checkout tree with no recovery, contradicting the README "cleaned up automatically" claim.

### ARC-F1 — rebase-pr force-pushes to origin without distinguishing same-repo from fork PRs · CONFIRMED · recommend-fix · medium
`src/gitbulk/rebase.py:127-156` (force_push_with_lease), `commands/rebase_pr.py:451`; `PRInfo` has no head-repo field (`pr_info.py:117-160`); GraphQL omits `isCrossRepository`/`headRepositoryOwner` (`gh.py:648-700`)
`dedupe_key: rebase-pr-fork-unhandled`
`force_push_with_lease` unconditionally pushes `origin HEAD:<head_ref>`. The `--author` veto (node flt7arg2) restricts rebase-pr to the user's OWN PRs, but a user's own PR can still originate from a FORK (common across a 150-repo fleet). For such PRs the head branch lives on the fork remote, not origin (the upstream/base). gitbulk can't tell: `PRInfo` carries no head-repo field and the query never requests `isCrossRepository`. The lease won't match origin's ref state → push fails or force-pushes an unrelated branch on the upstream. Exactly the "single riskiest unattended thing" `dieug50n` flagged, but the fork dimension is unaddressed and untested. Fix: add `isCrossRepository` to the query + a same-repo gate (Skip cross-repo PRs in `pr.needs_rebase` or refuse in `rebase.py`).

### PERF-F1 — RunState rewrites all of state.yaml on every record_repo_state/record_extra (O(n²) write amplification) · CONFIRMED · recommend-fix · small
`src/gitbulk/runstate.py:146-173`
`dedupe_key: runstate-slow-per-repo-write`
`_rewrite_state()` does a full `yaml.safe_dump` of the whole accumulated `{repos:...}` dict + atomic tmp+`os.replace` on EVERY call. `record_repo_state` runs once per repo (report.py:792, merge.py:512/612, close_stale.py:515/580, dispatch.py:667, rebase_pr.py:507). For 150-205 repos the Nth write serializes all N repos → total work ∝ N² plus N fsync-class writes. Fix: accumulate in memory (already in `self._per_repo`) and write `state.yaml` ONCE in `complete()` (or once per phase). Crash-resilience preserved by the `begin()` empty write + single final write.
Measurement: `python -c "import time; from gitbulk.runstate import RunState; rs=RunState.begin('report',[],{}); t=time.perf_counter(); [rs.record_repo_state(f'o/r{i}',{'pr_count':0,'prs':[]}) for i in range(205)]; print(time.perf_counter()-t)"`

### CMP-F1 — Audit records omit the acting GitHub identity for merge/close/force-push · CONFIRMED · recommend-fix · small
`src/gitbulk/runstate.py:89-100`; `invariants/catalog.py:101`; `commands/merge.py:545`
`dedupe_key: audit-trail-missing-actor`
The `gh.authenticated` invariant already fetches the authenticated login but discards it; mutating run artifacts (merge/close/force-push) record what was acted on and when, but never which GitHub identity did it. Stamp `authenticated_user().login` into `manifest.yaml` at `RunState.begin` → closes the gap for all mutating subcommands at once.

### MNT-F1 — Per-repo lock implemented + documented in this.i but never acquired; Phase 5 uses only the global exclusive lock that lj5pqn4kr explicitly rejected · CONFIRMED · recommend-fix · small
`src/gitbulk/locks.py:169` (repo_lock, no caller); `commands/merge.py:274`, `rebase_pr.py:205`, `close_stale.py` (all take only global_lock 'exclusive'); `this.i` node `lj5pqn4kr` (lines 1165-1176)
`dedupe_key: repo-lock-divergent`
`lj5pqn4kr` is a binding decision: per-repo locks held for the duration of any mutating op so "a merge on repo A can run concurrently with a report on repo B"; it explicitly REJECTS a single global exclusive lock. Shipped Phase 5 takes ONLY the global exclusive lock — the rejected design — and `repo_lock()` is dead code. No deviation/tension/comment records this. Per AGENTS.md, unrecorded divergence from a binding resolution is a defect. Fix: a `this.i` decision/deviation node (+ one-line `locks.py` comment) stating Phase 5 deliberately uses global-exclusive-only and `repo_lock` is parked or should be removed. **[recorded as this.i tension `rlkrcn3p`]**

### MNT-F2 — README/architecture claim merge/rebase/close-stale return exit 99 / "Phase 5 still ahead", but all three are implemented and wired · CONFIRMED · recommend-fix · small
`README.md:21-35`; `docs/architecture.md:3-8,441-455`; vs. `commands/{merge,rebase_pr,close_stale}.py` + `cli.py:556-561` wiring + `subcommands.py` chains
`dedupe_key: phase5-status-stale`
README status table + architecture banner assert the mutating subcommands are scaffolds returning 99, but cli.py routes them to real handlers that merge, force-push, and close PRs against the live fleet. `this.i` is current; the human-facing docs lie about implementation status. A maintainer could run `gitbulk merge --apply` from cron believing it's a no-op. Fix: update README status table + architecture status banner/phase plan to mark Phase 5 landed.

---

## MEDIUM (11)

### ARC-F2 — docs/architecture.md materially stale: declares Phase 5 mutators unimplemented · CONFIRMED · recommend-fix · small
`docs/architecture.md:3-8,425-470`; contradicted by `commands/{merge,close_stale,rebase_pr}.py` + `gh.py:149-265` (merge_pr/post_comment/close_pr present)
`dedupe_key: architecture-doc-stale`
The architecture doc is the stated contributor contract and human index into this.i. It asserts Phase 5 is pending, the GHClient Protocol has no mutating methods, and the mutating subcommands return exit 99 — all false. §6/§13 still say mutating methods are "intentionally absent," inviting an agent to re-add guards that shipped or mistrust live code. Refresh the status banner, phase table, and GHClient section.

### ARC-F3 — Per-handler scaffolding copy-pasted across 5+ command handlers · CONFIRMED · recommend-defer · medium
`commands/{report,dispatch,merge,close_stale,rebase_pr}.py` — `_partition_chain`, `_dc_to_dict`, `_config_snapshot`, `_runid_from_run_dir`, `_finish`, EXIT_* blocks
`dedupe_key: command-handlers-duplicated`
Five handlers each redefine identical helpers with "kept local so the handler stays standalone" comments — a deliberate independence choice while the shape settled. Now stable enough that the duplication is drift risk: an exit-code/filter-wiring/lock-budget fix must be applied 3-5× by hand (ARC-F1-style gaps can be fixed in one handler, missed in another). Extract a shared handler skeleton / `HandlerBase`; the EXIT_* constants in particular should live in one module.

### PERF-F2 — PrAuthorKnownInvariant re-reads + re-parses the org-members cache once per PR · CONFIRMED · recommend-fix · small
`src/gitbulk/invariants/catalog.py:530-543`
`dedupe_key: org-members-cache-slow-per-pr`
`PrAuthorKnownInvariant.check` calls `org_members_cache.load_cache(org)` (open + full `yaml.safe_load`) for EVERY PR; the file is invariant for the run and already loaded once in the `org.members.fresh` preflight (catalog.py:146). Hundreds of redundant YAML parses per locked run. No memoization anywhere (`functools.lru_cache` unused). Fix: load the frozenset once (seed onto `InvariantContext` or `lru_cache` by org).

### TST-F1 — No clock in InvariantContext: two separate module-global _utc_now() seams for one logical "now" · CONFIRMED · recommend-defer · medium
`invariants/base.py:55` (InvariantContext lacks `now`); `invariants/catalog.py:511`; `commands/close_stale.py:178`
`dedupe_key: clock-injection-divergent`
Time-eligibility decisions gating merge/close read wall-clock through two independent module-level helpers instead of a `now` on `InvariantContext` (which the handler already threads through `_decide_action`). Tests must monkeypatch both `catalog._utc_now` AND `close_stale._utc_now`; a test can freeze one and miss the other. A clock-skew bug between gating invariant and action handler wouldn't be caught. Fold `now` into `InvariantContext`.

### TST-F2 — No integration test: dispatch→worktree→exec wiring never exercised together · CONFIRMED · recommend-defer · medium
`tests/test_dispatch.py:386-468` (create_worktree/execute_targets/is_worktree_in_conflict/remove_worktree all stubbed); `commands/dispatch.py:572-635`
`dedupe_key: dispatch-untested-integration`
Every module is unit-tested with collaborators stubbed. The most safety-critical interaction per AGENTS.md — the worktree path from (path-verified) `create_worktree` is the exact path placed into `ExecTarget.working_directory`, so claude only runs in the disposable worktree, never the main clone — is split across two test files and validated by neither end-to-end. A wiring regression (handler passes `repo.local_path` instead of the worktree path) would pass both isolated suites. A stub-free seam test (real tmp git worktree + fake `popen_factory`) would close it.

### CMP-F2 — No documented basis for audit-artifact retention; default caps history at ~30 runs · CONFIRMED · recommend-fix · small
`config/policy.py:79`; `gc.py:29-89`; `this.i jw3kpn4q`
`dedupe_key: retention-policy-missing`
`retain_runs=30` is a magic default documented only as disk-bounding, not retention rationale. For a nightly cron it silently ages out the record of destructive remote actions after ~30 days; whether a 12-month audit expectation is met or waived is undocumented.

### CMP-F3 — README/architecture claim mutating subcommands unimplemented; they have shipped · CONFIRMED · recommend-fix · small
`README.md:20-35`; `docs/architecture.md:3-9,441-456`; `subcommands.py:145-168`
`dedupe_key: docs-drift-capability`
(Same root as MNT-F2/ARC-F2 from the compliance lens.) The documented capability inventory understates the most dangerous shipped operations; docs-as-change-management-evidence contradict the code.

### CMP-F4 — Audit trail has no tamper-evidence (plain files at dev-box uid) · CONFIRMED · recommend-accept-risk · medium
`src/gitbulk/runstate.py`; `reviews/security-hawk-2026-05-28.md` (Scenario A)
`dedupe_key: audit-trail-tamperable`
Run artifacts are plain files writable by the dev-box uid, no hash chain/signing/WORM. Already surfaced + accepted by security-hawk (Scenario A) as fine for a single-user tool where user==admin. Re-recorded so the acceptance stays visible as compliance evidence.

### MNT-F3 — README/docs still name the subcommand "rebase-onto-default"; renamed to "rebase-pr" · CONFIRMED · recommend-fix · small
`README.md:22,34`; `docs/architecture.md:28,441,452`; `docs/design-notes.md:89,242,274` — all say `rebase-onto-default`; actual command is `rebase-pr` (`subcommands.py:154`, `this.i:1866`)
`dedupe_key: rebase-command-stale`
`this.i:1872` records "rebase-pr, no alias kept." Because no alias exists, every doc reference to `rebase-onto-default` is a command that errors with "invalid choice." Global rename in README, architecture.md, design-notes.md.

### MNT-F4 — Raw TODO comments violate methodology §8; one hides a behavioral inconsistency · CONFIRMED · recommend-fix · small
`commands/dispatch.py:331-334` ("TODO: surface skipped_entries in dispatch summary"); `filters.py:51-55` ("TODO(flt7arg2): v2 dimensions land here")
`dedupe_key: todo-comments-noncompliant`
`docs/methodology.md:284`: "Do not leave raw TODO or FIXME comments in committed code. Convert to TECH_DEBT: or resolve." The dispatch one is more than style — it documents that dispatch silently discards `load_repos` `skipped_entries` (repos.txt parse errors) while report/merge/rebase-pr surface them — a hidden behavioral divergence (a repos.txt typo is invisible in a dispatch run). Convert both to `TECH_DEBT:` markers, or wire dispatch to mirror the other handlers.

### MNT-F5 — design-notes §7 lists ~9 unimplemented invariants (incl. safety-relevant repo.not_in_deny_list, pr.no_blocking_label) with no deferral marker · CONFIRMED · recommend-defer · medium
`docs/design-notes.md:159-188` vs `catalog.py` implemented set + `subcommands.py` chains
`dedupe_key: invariant-catalog-stale`
design-notes enumerates safety gates never built — notably `repo.not_in_deny_list` (kill-switch to exclude repos from mutation) and `pr.no_blocking_label` (honor a do-not-merge label). A maintainer assumes these are active on `--apply` when they aren't. Fix the doc now (annotate as planned-not-landed); decide + record in `this.i` whether the deny-list/blocking-label gates are required before broader cron use of the apply path. **[folded into this.i `ivb5kq3n`]**

---

## LOW (10)

### SEC-F2 — fetch_check_runs interpolates unvalidated sha into REST path · LIKELY · recommend-fix · small
`gh.py:1258` · `dedupe_key: check-runs-sha-unsafe`
sha from gh's own `mergeCommit.oid`, passed as a single argv element (no shell injection), but unvalidated — a hostile gh could embed `../` to pivot the read-only check-runs read. Bounded (read-only). Validate `^[0-9a-f]{7,40}$` alongside the F1 hardening.

### SEC-F3 — No automated secret-scanning gate (pre-commit or CI) · CONFIRMED · recommend-defer · small
`.github/workflows/ci.yml`, `.githooks/pre-commit` · `dedupe_key: secret-scanning-missing`
No gitleaks/trufflehog/git-secrets/detect-secrets step. Repo carries no secrets (workflows use `${{ secrets.GITHUB_TOKEN }}` correctly) — prevention not remediation. Carried from 2026-05-28 review.

### SEC-F4 — GitHub Actions pinned by mutable major tag, not commit SHA · CONFIRMED · recommend-accept-risk · small
`.github/workflows/ci.yml:15,18,24` · `dedupe_key: github-actions-unpinned` · reported by security-hawk + devops-engineer
`actions/checkout@v6`, `astral-sh/setup-uv@v7`, `actions/setup-python@v6` are mutable-tag pins (tj-actions-class retarget vector). CI job is `contents:read` only, no publish/deploy secret → small blast radius; Dependabot watches actions; tags are node24-runtime. Acceptable for a single-author repo; record the deviation.

### DEV-F2 — retain_runs retention knob undocumented in example config · CONFIRMED · recommend-fix · small
`config/gitbulk.yaml.example`; `config/policy.py:79` · `dedupe_key: retain-runs-missing`
The runs/ retention sweep is governed by `defaults.retain_runs` (default 30) but the example config only documents `worktree_root` — an operator can't discover/tune it without reading source.

### DEV-F4 — CI lacks concurrency group; license still TODO · CONFIRMED · recommend-defer · small
`.github/workflows/ci.yml`; `README.md:152` · `dedupe_key: ci-divergent`
No concurrency group → rapid PR pushes run redundant full-matrix jobs; README license still a TODO. Neither blocks single-user cron operation.

### ARC-F4 — merge_pr defaults delete_branch=True with no cross-repo/fork awareness · LIKELY · recommend-defer · small
`gh.py:149-184` (delete_branch default True); `commands/merge.py:545-550` · `dedupe_key: merge-pr-fork-unhandled`
merge passes `delete_branch=True` for every eligible PR. For a fork PR, `--delete-branch` operates on the fork's head branch — may fail/no-op depending on permissions. Same missing abstraction as ARC-F1 (no fork dimension on PRInfo). Lower severity (server-side, GitHub refuses unsafe deletes) but argues for fixing the data model once.

### PERF-F3 — No performance-measurement infrastructure despite stated 150-205 repo scale · CONFIRMED · recommend-defer · medium
`pyproject.toml:16-17` · `dedupe_key: gitbulk-missing-perf-baseline`
Only pytest + pytest-cov as test deps; no pytest-benchmark, no benchmarks/ or tests/performance/, no cProfile/timeit. Without a baseline there's no way to confirm F1/F2 fixes helped or catch a regression reintroducing O(n²). Recommend: perf_counter timing around the 3 pipeline phases into the manifest, + one pytest-benchmark over a synthetic 200-repo RunState loop.

### TST-F3 — config/repos.py git-remote resolution tested against real `git init`, not an injected seam · CONFIRMED · recommend-accept-risk · small
`tests/test_config_repos.py:250-327` · `dedupe_key: repos-config-untested-seam`
Unlike every other subprocess module (gh, worktree, exec), `config/repos.py`'s `git remote get-url` path spawns real `git` against throwaway repos rather than an injectable seam. Doesn't violate "no network in tests"; read-only setup. Acceptable as-is; worth a seam if repos.py grows more git calls.

### TST-F4 — Assertion-free lock tests document no-block intent only in docstrings · CONFIRMED · recommend-accept-risk · small
`tests/test_locks.py:419,431` · `dedupe_key: lock-tests-untested-intent`
Two lock tests have no `assert`/`pytest.raises` — they prove "the with-block was entered" by completing. If a refactor made `global_lock` silently no-op, they'd still pass. A one-line explicit assertion would harden intent. Low stakes (`timeout=0.5` already fails on contention).

### CMP-F5 — Dispatch agent-output logs may capture repo content with no stated data lifecycle · LIKELY · recommend-defer · small
`commands/dispatch.py:608`; `exec.py:247-251` · `dedupe_key: dispatch-logs-unclassified`
dispatch persists each headless Claude agent's stdout/stderr under the run dir; can mirror diffs/file contents/prompts. Owner-only (umask 0o077, security-hawk F3), pruned by retain_runs, but no doc classifies contents/lifecycle. A one-line data-classification note closes it.
