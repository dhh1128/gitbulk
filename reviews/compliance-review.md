# Compliance Review: gitbulk

**Date:** 2026-05-29
**Effort level:** medium (breadth-first)
**Frameworks in scope:** SOC-2 Type II concepts applied by analogy; GDPR N/A (no personal/customer data). Calibrated to a single-user personal CLI.
**Context sources used:** AGENTS.md, README.md, docs/architecture.md, this.i (grep + targeted reads), src/gitbulk/runstate.py, gc.py, invariants/runner.py, invariants/catalog.py, gh.py (mutating methods + gh.authenticated), commands/{merge,rebase_pr,close_stale,dispatch}.py, exec.py, bin/gitbulk-cron, config/policy.py, reviews/security-hawk-2026-05-28.md.

---

## Evidence Inventory

- **No regulated/personal data.** gitbulk is single-user, local, no network-exposed surface, no DB, no PII/customer data. Auth delegated entirely to `gh` (GitHub token) and `ssh-agent`. Most SOC-2 control families (access reviews, off-boarding, breach notification, vendor DPAs, data-subject requests, BCP/DR) are out of scope and produced **no findings** — manufacturing them would be dishonest.
- **The relevant compliance lens is the audit trail for high-stakes operations.** gitbulk now performs *merging PRs*, *closing PRs*, and *force-pushing rebases* against ~150 real repos. The run-artifact tree under `~/.cache/gitbulk/runs/<runid>-<sub>/` (manifest.yaml, state.yaml, invariants.log, errors.log) IS the audit trail.
- **Audit-trail mechanics inspected and sound in structure:** schema-versioned artifacts (`schv4nrm`), atomic writes, JSONL append for invariants/errors with UTC ISO timestamps, per-PR outcome records including `merge_commit_sha`, `--skip-check` overrides logged as WARNING + exit 4, dry-run-by-default gates (`2vqp4nk6`).
- **Mutating subcommands are LIVE** (merge, rebase-pr, close-stale wired as `mutating=True` handlers in subcommands.py), contradicting README/architecture "Phase 5 still ahead."
- **Tamper-evidence gap already adjudicated** by security-hawk (2026-05-28, Scenario A): plain files at dev-box uid, no hash chain / signing; accepted as fine for a personal tool. Not re-litigated here.

---

## Executive Summary

gitbulk's audit trail is structurally sound for a personal tool: schema-versioned, atomically written, timestamped, with override and dry-run signals. The one genuine completeness gap is **actor attribution** — the run artifacts record *what* was merged/closed/force-pushed and *when*, but never *which authenticated GitHub identity* did it, even though `gh.authenticated` already fetches that login during preflight. The most urgent action is to stamp the authenticated login into `manifest.yaml` at run begin (a few lines). Secondary: the run-artifact retention default (30) has no documented basis and effectively caps the audit history at ~30 nightly runs.

---

## Compliance Documentation Status

| Artifact | Status |
|---|---|
| Audit-trail design | Present (runstate.py + `kp7nw4mq`/`schv4nrm`/`tp4kq2nr`) |
| Data-retention policy / basis | **Absent** — `retain_runs=30` and `retain_cron_log_days=30` are magic defaults with no stated rationale or regulatory basis |
| Data classification | **Absent** — no statement of what dispatch agent-output logs may contain |
| Incident response / breach notification | N/A (no data subjects, single-user) |
| Access control / access review | N/A (single user; auth delegated to gh/ssh-agent) |
| Tamper-evidence on audit log | Absent by design; accepted (security-hawk Scenario A) |
| Change-management evidence (own docs) | Present but **drifted** — docs understate shipped destructive capability |

---

## Top Findings

### F1: Audit records omit the acting GitHub identity
- **Severity:** HIGH
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/runstate.py:89-100` (manifest); `src/gitbulk/commands/merge.py:545` / `close_stale.py:548,562` / `rebase_pr.py:451`; `src/gitbulk/invariants/catalog.py:101`
- **Finding:** Every mutating operation (merge, close, force-push) is recorded with slug, PR number, outcome, timestamp, and (for merge) the resulting commit SHA — but never the authenticated GitHub login that performed it. The `gh.authenticated` UNIVERSAL invariant already calls `ctx.gh.authenticated_user()` and reads `user["login"]`, then discards it. `manifest.yaml` captures argv and a config snapshot but no actor.
- **Audit/regulatory consequence:** An auditor (or the user, post-incident) reconstructing "who force-pushed/closed this PR" from gitbulk's own records cannot attribute the action to an identity — only to "whatever token `gh` was configured with on the box at run time." For a tool acting on 150 repos under potentially-rotating credentials, this is the core attributability gap.
- **Recommendation:** At `RunState.begin`, record the authenticated login (and optionally the gh account/host) into `manifest.yaml` (e.g. `actor: {login: ..., host: github.com}`). Reuse the value already fetched by the `gh.authenticated` invariant to avoid a second round-trip. Small change; closes the gap for all mutating subcommands at once.

### F2: No documented basis for audit-artifact retention; default effectively caps history at ~30 runs
- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/config/policy.py:79` (`retain_runs: int = 30`); `src/gitbulk/gc.py:29-89`; this.i `jw3kpn4q` Track A
- **Finding:** `retain_runs` defaults to 30 and `RunState.complete` prunes older same-subcommand runs beyond that count. `this.i` documents *that* the sweep exists (to bound disk growth) but not a *retention* rationale. For a nightly cron, 30 runs ≈ 30 days of merge/close/rebase history; older audit records are deleted. The typical audit-log expectation (12 months, 3 months hot) is undocumented as either met or deliberately waived.
- **Audit/regulatory consequence:** Undocumented retention is indistinguishable from an unknown gap — the record of what gitbulk did to real repos silently ages out with no stated policy. Even for a personal tool, the basis ("30 runs is enough because…") should be written down so the default is a decision, not an accident.
- **Recommendation:** Add a one-paragraph retention rationale to `this.i`/`docs` stating the chosen window and why (e.g. "history beyond N runs is recoverable from GitHub's own PR/commit history, so local retention is for convenience, not the system of record"). If GitHub is the true system of record for these actions, say so — that single sentence reframes F1/F2 substantially.

### F3: Doc/code drift understates shipped destructive capability
- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `README.md:20-35` ("merge, rebase-onto-default, close-stale ... return exit code 99 until Phase 5 lands"); `docs/architecture.md:3-9, 441-456` ("Phase 5 (mutating subcommands) ... still ahead"); contradicted by `src/gitbulk/subcommands.py:145-168` (merge/rebase-pr/close-stale all `mutating=True` with full handlers) and `commands/{merge,rebase_pr,close_stale}.py`.
- **Finding:** The README capability table and architecture status header both state the mutating subcommands are unimplemented/return exit 99, but all three are fully wired live handlers that merge PRs, close PRs, and force-push branches.
- **Audit/regulatory consequence:** The documentation is the change-management/capability-inventory evidence for this tool. A reader or operator trusting the docs would believe gitbulk cannot mutate remote state — the opposite of reality, for the most dangerous operations it performs. An auditor treats "docs claim a control/capability boundary that the code violates" as a management-letter-grade finding regardless of intent.
- **Recommendation:** Update README §Status and `docs/architecture.md` (header + §12 phase plan + §13 known gaps) to reflect that merge/rebase-pr/close-stale have shipped and are mutating; verify the exit-code semantics table no longer implies exit 99 for them.

### F4: Tamper-evidence absent on the audit trail (deduped to security-hawk Scenario A)
- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/runstate.py` (plain-file writes, default perms); `reviews/security-hawk-2026-05-28.md` Scenario A
- **Finding:** Run artifacts are plain files writable by the dev-box uid; no hash chain, signing, or append-only WORM. The audit trail is exactly as trustworthy as the uid that owns it. Security-hawk already surfaced and accepted this for a personal tool.
- **Audit/regulatory consequence:** In a regulated multi-user context this would be HIGH; here the threat model (single user == admin) makes it defensible. Recording it keeps the acceptance visible as compliance evidence rather than an unexamined gap.
- **Recommendation:** Accept risk; reference the security-hawk acceptance. If gitbulk ever runs as a shared service account, revisit (hash-chain `invariants.log`).

### F5: Dispatch agent-output logs may capture arbitrary repo content with no stated lifecycle
- **Severity:** LOW
- **Confidence:** LIKELY
- **Location:** `src/gitbulk/commands/dispatch.py:608` (`log_dir = rs.run_dir / "dispatch-logs"`); `src/gitbulk/exec.py:247-251` (`<key>.stdout.log` / `.stderr.log`)
- **Finding:** `dispatch` persists each headless Claude agent's stdout/stderr under the run dir. These can contain diffs, file contents, and prompt text the agent saw in the worktree. They are protected by `os.umask(0o077)` (security-hawk F3) and pruned by the same `retain_runs` sweep, but no doc classifies what they may contain or states their lifecycle.
- **Audit/regulatory consequence:** Minor for a personal tool with no regulated data, but if a repo in the fleet ever contains sensitive content, these logs become an uninventoried copy of it. A one-line data-classification note closes the gap cheaply.
- **Recommendation:** Add a sentence to `docs/architecture.md` §10 noting dispatch logs may mirror repo content, are owner-only (0o077), and age out with `retain_runs`.

---

## Additional Patterns Noted

- `merge_pr` tolerates empty/non-JSON stdout and returns `{}`; the audit record then relies on a follow-up `fetch_merge_commit_sha` (non-fatal on failure). When that follow-up fails, the merge IS recorded but without the commit SHA — a partial-attribution case worth a WARNING note in summary.md (already logged to errors.log).
- Good practice observed: `--skip-check` overrides are logged as WARNING + exit 4 (asymmetric audit, `r4nzp7kq`); dry-run-by-default on all mutators; UTC timestamps throughout.

---

## Residual Unknowns

- Whether GitHub's own PR/commit history is intended as the authoritative system of record for gitbulk's actions (would substantially soften F1/F2). This is a one-sentence intent question for the user.
- Actual crontab cadence (assumed nightly) — drives whether `retain_runs=30` is ~30 days or longer.

---

## Decisions Needed

- Is local run-artifact retention the system of record, or convenience over GitHub's own history? (Reframes F1/F2.)
- Acceptable audit-history window for merge/close/force-push actions.

```yaml
findings:
  - id: CMP-F1
    persona: compliance-auditor
    title: Audit records omit the acting GitHub identity for merge/close/force-push
    severity: HIGH
    confidence: CONFIRMED
    location: src/gitbulk/runstate.py:89-100 ; src/gitbulk/invariants/catalog.py:101 ; src/gitbulk/commands/merge.py:545
    dedupe_key: audit-trail-missing-actor
    recommended_disposition: recommend-fix
    rationale: gh.authenticated already fetches the login but discards it; mutating run artifacts record what+when but never who. Cheap to stamp into manifest.yaml at run begin.
    revisit_condition: null
    fix_effort: small
  - id: CMP-F2
    persona: compliance-auditor
    title: No documented basis for audit-artifact retention; default caps history at ~30 runs
    severity: MEDIUM
    confidence: CONFIRMED
    location: src/gitbulk/config/policy.py:79 ; src/gitbulk/gc.py:29-89 ; this.i jw3kpn4q
    dedupe_key: retention-policy-missing
    recommended_disposition: recommend-fix
    rationale: retain_runs=30 is a magic default with no stated retention rationale; for a nightly cron this silently ages out the record of destructive remote actions.
    revisit_condition: null
    fix_effort: small
  - id: CMP-F3
    persona: compliance-auditor
    title: README/architecture claim mutating subcommands unimplemented; they have shipped
    severity: MEDIUM
    confidence: CONFIRMED
    location: README.md:20-35 ; docs/architecture.md:3-9,441-456 ; src/gitbulk/subcommands.py:145-168
    dedupe_key: docs-drift-capability
    recommended_disposition: recommend-fix
    rationale: Capability inventory understates the most dangerous shipped operations (merge/close/force-push); docs as change-management evidence contradict the code.
    revisit_condition: null
    fix_effort: small
  - id: CMP-F4
    persona: compliance-auditor
    title: Audit trail has no tamper-evidence (plain files at dev-box uid)
    severity: MEDIUM
    confidence: CONFIRMED
    location: src/gitbulk/runstate.py ; reviews/security-hawk-2026-05-28.md (Scenario A)
    dedupe_key: audit-trail-tamperable
    recommended_disposition: recommend-accept-risk
    rationale: Single-user==admin threat model makes uid-trustworthy audit defensible; already surfaced and accepted by security-hawk. Recording keeps the acceptance visible.
    revisit_condition: Revisit if gitbulk ever runs under a shared/service account; then hash-chain invariants.log.
    fix_effort: medium
  - id: CMP-F5
    persona: compliance-auditor
    title: Dispatch agent-output logs may capture repo content with no stated data lifecycle
    severity: LOW
    confidence: LIKELY
    location: src/gitbulk/commands/dispatch.py:608 ; src/gitbulk/exec.py:247-251
    dedupe_key: dispatch-logs-unclassified
    recommended_disposition: recommend-defer
    rationale: Logs are owner-only (0o077) and pruned with retain_runs, but no doc classifies their contents; cheap one-line data-classification note closes it.
    revisit_condition: Address when a data-classification note is added to docs/architecture.md §10, or sooner if a fleet repo holds sensitive content.
    fix_effort: small
```
