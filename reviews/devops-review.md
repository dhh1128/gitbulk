# DevOps / CI/CD Review: gitbulk

**Date:** 2026-05-29
**Effort level:** medium
**Mode:** unattended
**Reviewer role:** DevOps Engineer (adversarial)

---

## Calibration note

`gitbulk` is a single-user personal CLI that runs from cron on a Linux dev box.
No HTTP surface, no container, no Helm chart, no Kubernetes, no Flyway, no
health probe, no Prometheus, no on-call rota. The container/k8s/Flyway/probe
families of the role prompt do not apply and are not turned into findings (the
2026-05-27 review already enumerated them). This review focuses on: the CI
workflow, supply-chain hardening of the two workflows, the cron wrapper, the
runtime cache lifecycle (runs/, worktrees/, locks), dependency reproducibility,
and local-dev ergonomics.

## Prior dispositions honored

The 2026-05-27 devops review's findings have largely been resolved, and I do
not re-litigate them:

- **F1 (no GC / unbounded runs/):** RESOLVED for Track A — `gc.prune_runs()`
  exists and is wired into `RunState.complete(retain_runs=...)` for every
  subcommand; `policy.defaults.retain_runs` defaults to 30. (this.i `jw3kpn4q`.)
- **F2 (stale lock metadata):** RESOLVED — `locks.py` now checks pid liveness
  (`_is_pid_alive`) and annotates the error with "(no longer running — stale
  lock metadata)".
- **F3 (no lockfile):** RESOLVED — `uv.lock` committed; CI runs
  `uv sync --frozen`; `.github/dependabot.yml` covers `pip` + `github-actions`.
- **F4 (3-version matrix):** RESOLVED by conscious decision (this.i node at
  ~line 1380, revised 2026-05-28 to single-version 3.12 + `.python-version`).
- **F5 (cron wrapper gaps):** RESOLVED — `bin/gitbulk-cron` now has
  `set -eo pipefail`, an outer `flock -n`, PATH/GITBULK_BIN resolution, and
  cron-log retention.
- **Logging into the void:** RESOLVED — `cli._configure_logging()` wires a
  stderr handler with `GITBULK_LOG_LEVEL`.

---

## Evidence Inventory

Read: `AGENTS.md`, `README.md`, `pyproject.toml`, `uv.lock` (presence),
`.python-version`, `.gitignore`, `.githooks/pre-commit`,
`.github/workflows/ci.yml`, `.github/workflows/copilot-review-gate.yml`,
`.github/dependabot.yml`, `.agent-bin/{git,config.json}`, `bin/gitbulk-cron`,
`config/gitbulk.yaml.example`, `src/gitbulk/{cli,gc,subcommands,worktree}.py`,
`src/gitbulk/config/policy.py`, `src/gitbulk/commands/{dispatch,rebase_pr}.py`,
`this.i` node `jw3kpn4q` and the python-version node, plus the prior review
`reviews/devops-engineer-2026-05-27.md`.

Verified externally: `actions/checkout@v6`, `actions/setup-python@v6`, and
`astral-sh/setup-uv@v7` all resolve to `using: node24` on their pinned tags —
no Node-20 deprecation warning.

Not run: pytest / coverage gate (not independently executed).

---

## Executive Summary

Operational hygiene is strong and most prior findings are closed. The one
material gap is a **forgotten Track-A commitment**: this.i node `jw3kpn4q`
scoped a run-start orphaned-worktree sweep to land "in Phase 4 when dispatch
actually creates worktrees," but Phase 4+ has shipped (`dispatch` and
`rebase-pr` both create worktrees) and no sweep function was ever implemented —
`sweep_orphan_worktrees` exists only as a docstring mention in `gc.py`.
Worktree teardown lives in a post-pass, not a `finally`, so a SIGKILL/OOM/crash
during the long-running Claude pool strands a full `<runid>/` checkout tree on
disk with no recovery path, contradicting the README's "cleaned up
automatically" promise. Everything else is minor (config-discoverability,
SHA-pinning, CI concurrency, license TODO).

---

## Top Findings

### F1: Orphaned-worktree sweep (this.i Track-A item 3) never shipped, though Phase-4 worktree creation has — crashed runs strand full checkouts

- **Severity:** HIGH
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/gc.py:13` (only a docstring mention of
  `sweep_orphan_worktrees`; no implementation); `src/gitbulk/commands/dispatch.py:561-648`
  (worktree teardown in step-11 post-pass, not a `finally`);
  `src/gitbulk/commands/rebase_pr.py:567`; this.i `jw3kpn4q` Track A item 3.
- **Finding:** Node `jw3kpn4q` Track A item 3 commits to a run-start
  orphaned-worktree sweep "defined as a function but not wired into a CLI
  handler until Phase 4 when dispatch actually creates worktrees." Phase 4+ has
  landed — `dispatch` and `rebase-pr` create worktrees under
  `<worktree_root>/<runid>/<owner>__<repo>__pr<N>` — but the function was never
  implemented (grep finds `sweep_orphan_worktrees` only in a `gc.py` docstring),
  and no command calls a sweep at startup. Cleanup happens only in the dispatch
  step-11 post-pass after `execute_targets()` returns; that pool runs headless
  Claude agents and can run for many minutes. A SIGKILL, OOM-kill, host reboot,
  or unhandled crash between `create_worktree` and the post-pass leaves every
  created worktree — a full repo checkout — on disk forever, with no
  `gitbulk gc` subcommand (Track B, deferred) and no run-start sweep to reclaim
  it.
- **Operational consequence:** The README crontab comment promises worktrees
  "are cleaned up automatically," but a crashed weekly `dispatch --apply`
  silently accumulates tens-to-hundreds of MB per orphan, and the only recovery
  is manual `git worktree remove --force`. This is the operational risk Track A
  was created to prevent, now reintroduced by Phase 4 shipping without item 3.
- **Recommendation:** Implement `gc.sweep_orphan_worktrees(worktree_root,
  current_runid, ...)` that, at the start of each worktree-creating run,
  `git worktree remove --force`'s any `<runid>/` tree whose runid is not the
  current run and whose mtime exceeds a grace window, skipping in-conflict
  worktrees (`is_worktree_in_conflict`, node `vp7n2krq`). Call it from
  `dispatch` and `rebase-pr` startup; log what it reclaimed into
  `invariants.log`/`errors.log`. This is the already-scoped Track A work, not
  new design.

---

### F2: `retain_runs` retention knob is invisible in the example config

- **Severity:** LOW
- **Confidence:** CONFIRMED
- **Location:** `config/gitbulk.yaml.example` (no `retain_runs` key);
  `src/gitbulk/config/policy.py:79` (`retain_runs: int = 30`).
- **Finding:** The runs/ retention sweep (F1 of the prior review) is governed by
  `defaults.retain_runs`, defaulting to 30. The example config documents
  `worktree_root` (commented) but never mentions `retain_runs`, so an operator
  who wants to keep more/less history or who is debugging "why did my old run
  dirs disappear" has no discoverable knob — they would have to read the policy
  source.
- **Operational consequence:** Minor surprise. A user investigating missing run
  history, or one who wants a longer forensic window, cannot find the control
  without reading code.
- **Recommendation:** Add a commented `retain_runs: 30` line (and ideally a note
  on `GITBULK_CRON_RETAIN_DAYS` for cron logs) to the `defaults:` block of
  `config/gitbulk.yaml.example`.

---

### F3: GitHub Actions pinned to mutable major-version tags, not commit SHAs

- **Severity:** LOW
- **Confidence:** CONFIRMED
- **Location:** `.github/workflows/ci.yml:15,18,24` (`@v6`/`@v7`);
  `.github/dependabot.yml` (github-actions ecosystem covered).
- **Finding:** Actions reference mutable major tags (`actions/checkout@v6`,
  `actions/setup-python@v6`, `astral-sh/setup-uv@v7`) rather than full commit
  SHAs. Supply-chain best practice is SHA-pinning with Dependabot bumping the
  pins. Mitigating context: all three are first-party / well-known publishers,
  Dependabot already watches `github-actions`, and the CI workflow's
  `GITHUB_TOKEN` is `contents: read`, so the blast radius of a compromised tag
  is small for this single-user repo.
- **Operational consequence:** A retargeted upstream tag could run arbitrary
  code in CI; bounded here by read-only token scope and a private/personal repo.
- **Recommendation:** Optionally SHA-pin the three actions (`@<sha> #
  vX.Y.Z`) and let Dependabot maintain them. Low priority given the read-only
  token and trusted publishers; reasonable to accept-risk for a personal repo.

---

### F4: CI workflow has no `concurrency` group; license still a TODO before public push

- **Severity:** LOW
- **Confidence:** CONFIRMED
- **Location:** `.github/workflows/ci.yml` (no `concurrency:`); `README.md:152`
  (`License: TODO — to be decided before the first remote push`).
- **Finding:** Two unrelated low-priority items folded together. (a) The CI
  workflow lacks a `concurrency` group, so rapid pushes to a PR run redundant
  full jobs; harmless on a free/personal repo but wasteful. (b) The repo still
  has no LICENSE; the README badge points at `dhh1128/gitbulk`. If the repo goes
  public, the license decision gates outside contribution.
- **Operational consequence:** Wasted CI minutes (a); legal ambiguity for any
  external contributor (b). Neither blocks current single-user operation.
- **Recommendation:** Add `concurrency: { group: ${{ github.workflow }}-${{
  github.ref }}, cancel-in-progress: true }` to `ci.yml`. Decide the license
  before the first public push (already in the author's TODO).

---

## Additional Patterns Noted

- `.coverage` and `.ai-safety-check.dhh1128` are present in the working tree but
  correctly gitignored and untracked. No action.
- `copilot-review-gate.yml`: the `|| echo` fallbacks that mask
  `request_reviewers` API errors are now annotated with an explicit `TECH_DEBT`
  comment (lines 45-50) telling future-you how to restore loud failure. Prior
  concern addressed.
- No branch-protection-as-code (`.github/settings.yml` or equivalent). For a
  personal repo this is acceptable; a one-paragraph runbook entry listing the
  intended required-checks/required-reviewers settings would aid recoverability
  if the repo is recreated.
- `.agent-bin/` shim model is sound but relies on the operator placing it at the
  front of PATH; nothing warns at CLI startup if the shims are inactive. Noted
  in the prior review; still applies. Defense-in-depth gap only.

---

## Residual Unknowns

- Whether the orphan-worktree sweep (F1) was consciously re-deferred to Track B
  after Phase 4 shipped, or simply forgotten. The node text scopes it to Track A
  / Phase 4, which has shipped — so absent a newer node revising that, I read it
  as a forgotten commitment. If a `deviation:`/`tension:` node now re-defers it,
  the right disposition is rebut-with-pointer.
- Coverage gate was not independently executed; CI asserts `--cov-fail-under=100`.

---

## Decisions Needed

1. **F1:** Implement the Track-A orphan-worktree sweep now (Phase 4 has
   shipped), or record an explicit node re-deferring it to Track B with a stated
   compensating control?
2. **F3:** SHA-pin actions, or accept-risk given read-only token + trusted
   publishers + Dependabot?

---

## Findings manifest

```yaml
findings:
  - id: DEV-F1
    persona: devops-engineer
    title: Orphaned-worktree sweep (Track-A jw3kpn4q item 3) never implemented though Phase-4 worktree creation shipped
    severity: HIGH
    confidence: CONFIRMED
    location: src/gitbulk/gc.py:13; src/gitbulk/commands/dispatch.py:561-648
    dedupe_key: worktree-orphan-unbounded
    recommended_disposition: recommend-fix
    rationale: dispatch/rebase-pr create worktrees but cleanup is a post-pass not a finally, and the scoped run-start sweep was never built; a crash strands full checkouts with no recovery, contradicting the README.
    revisit_condition: null
    fix_effort: small
  - id: DEV-F2
    persona: devops-engineer
    title: retain_runs retention knob undocumented in example config
    severity: LOW
    confidence: CONFIRMED
    location: config/gitbulk.yaml.example; src/gitbulk/config/policy.py:79
    dedupe_key: retain-runs-missing
    recommended_disposition: recommend-fix
    rationale: Operator cannot discover the runs/ retention control without reading policy source.
    revisit_condition: null
    fix_effort: small
  - id: DEV-F3
    persona: devops-engineer
    title: GitHub Actions pinned to mutable major tags, not commit SHAs
    severity: LOW
    confidence: CONFIRMED
    location: .github/workflows/ci.yml:15,18,24
    dedupe_key: github-actions-unpinned
    recommended_disposition: recommend-accept-risk
    rationale: Tag retargeting risk, but bounded by read-only GITHUB_TOKEN, trusted publishers, and Dependabot coverage on a personal repo.
    revisit_condition: repo goes public or gains write-scoped CI jobs
    fix_effort: small
  - id: DEV-F4
    persona: devops-engineer
    title: CI lacks concurrency group; license still TODO before public push
    severity: LOW
    confidence: CONFIRMED
    location: .github/workflows/ci.yml; README.md:152
    dedupe_key: ci-divergent
    recommended_disposition: recommend-defer
    rationale: Redundant CI runs and missing LICENSE; neither blocks single-user operation.
    revisit_condition: repo goes public
    fix_effort: small
```
