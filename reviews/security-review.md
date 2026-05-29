# Security Review: gitbulk (Phase 5 mutating subcommands)

**Date:** 2026-05-29
**Effort level:** medium (breadth-first)
**Mode:** unattended (orchestrated)
**Reviewer role:** security-hawk

**Calibration:** single-user personal CLI; no HTTP surface, no DB, no
multi-tenancy. Service-style criteria (RFC 9421 sig validation, nonce
replay, IDOR, JWT/OAuth, CORS, cell-boundary sealed-box, DB schema
grants) do not apply and are out of scope. The relevant threat is the
one the prior review fixed: **untrusted external data (PR/branch
metadata from `gh`, repos.txt config) coercing gitbulk into damaging
real GitHub repos or the local box.** Threat-model scenarios C
(swapped/malicious `gh`) and D (crafted PR data) are explicitly
in-scope per the prior review's calibration.

**Context sources read:** AGENTS.md (skimmed), README.md (skimmed),
prior `reviews/security-hawk-2026-05-28.md`, role prompt + orchestrating
doc. Code: `exec.py`, `gh.py`, `claude.py`, `config/repos.py`,
`rebase.py`, `worktree.py`, `commands/dispatch.py`, `commands/rebase_pr.py`,
`commands/merge.py` (partial), `util/github_url.py`, `pr_info.py`,
`cli.py` (umask), `.github/workflows/*.yml`, `.github/dependabot.yml`,
`bin/gitbulk-cron`, `pyproject.toml`.

---

## Evidence Inventory

- Prior review's F1 (slug `..` traversal) is **fixed**: `config/repos.py:41-44`
  now uses a tight slug regex plus a `_FORBIDDEN_SEGMENTS` `.`/`..` check.
- Prior F2 (gh PATH hijack) is **fixed**: `gh.py:737-756` resolves
  `gh` via `shutil.which` once at construction to an absolute path.
- Prior F3 (umask) is **fixed**: `cli.py:606-621` calls `os.umask(0o077)`
  at the top of `main()`. `locks.py:143` still passes `0o644` to
  `os.open`, but the process umask now masks it down to `0o600`.
- All subprocess invocations are list-argv; **no `shell=True`, no
  `os.system`, no `eval`/`exec`** in `src/`. YAML is `safe_load`/`safe_dump`.
- New since prior review: the Phase 4/5 mutating code (`rebase.py`,
  `worktree.py`, `commands/dispatch.py`, `commands/rebase_pr.py`) issues
  local `git` against worktrees, taking ref/SHA values from `gh`.
- Empirically verified: `git fetch origin '--upload-pack=touch /tmp/PWNED'`
  creates the file (arg-injection → arbitrary command exec); a `--`
  terminator blocks it. `git worktree add --detach <path> '--foo'` is
  likewise parsed as an option.

---

## Executive Summary

The two structural exposures the prior review flagged as Phase-5
liabilities (slug traversal, gh PATH) are now closed, and umask
discipline is in place. The one **new** material risk is a confirmed
git **argument-injection** primitive: PR branch/SHA strings flow from
`gh` straight into positional `git fetch` / `git rebase` / `git worktree
add` arguments with no `--` option terminator and no ref validation. A
ref-shaped value beginning with `-` (reachable via a malicious/swapped
`gh` or crafted PR metadata — both in-scope) becomes a git option such
as `--upload-pack=<cmd>`, which git executes. Fix is a few lines (insert
`--`, or validate refs). Everything else is defense-in-depth.

---

## Top Findings

### F1: PR ref/SHA values reach positional `git` args without a `--` terminator → argument-injection / command-exec

- **Severity:** HIGH
- **Confidence:** CONFIRMED (git behavior + dataflow confirmed; trigger requires hostile/crafted `gh` PR data, which is in-scope)
- **Location:** `src/gitbulk/rebase.py:100` (`_git(worktree_path, "fetch", "origin", base_ref)`), `:107` (`git rebase origin/<base_ref>`), `:145-151` (`force_push_with_lease`: `--force-with-lease={head_ref}:{expected_sha}` + `HEAD:{head_ref}`), `src/gitbulk/worktree.py:143-150` (`git worktree add --detach <target> pr_head_sha`)
- **Finding:** `pr.base_ref`, `pr.head_ref`, `pr.head_sha` are plain `str`
  fields in `pr_info.py:131-133`, populated directly from GraphQL
  `baseRefName`/`headRefName`/`headRefOid` (`gh.py:1392-1394`) with no
  validation. They are passed as trailing positional args to `git fetch`,
  `git rebase`, and `git worktree add --detach`, none of which use a `--`
  terminator. A value like `--upload-pack=touch /tmp/PWNED` (verified to
  create the file via `git fetch origin <that>`) is interpreted as a git
  option, and `--upload-pack`/`--exec`/`-c core.fsmonitor=<cmd>` give
  arbitrary command execution on the user's box during an unattended cron
  run.
- **Exploit path:** Threat-model scenario C — a swapped/malicious `gh`
  on `$PATH` (or an `gh` extension/plugin compromise) returns a PR node
  whose `baseRefName` is `--upload-pack=<payload>`; `rebase-pr --apply`
  then runs `git fetch origin --upload-pack=<payload>` inside the
  worktree. Scenario D — crafted PR metadata. The local-git safety
  contract ("never touch the main clone") does not help: the injected
  option runs arbitrary code regardless of cwd.
- **Recommendation:** Terminate options before every user-controlled
  positional: `git fetch origin -- <base_ref>`, `git worktree add
  --detach <target> -- <sha>`. For `rebase origin/<base_ref>` and the
  `--force-with-lease=<head_ref>:<sha>` forms (where `--` can't precede
  an `=`-embedded value), validate the ref/SHA shape at the boundary —
  reject any ref segment beginning with `-` and any SHA that isn't
  `[0-9a-f]{7,40}` — ideally in `_pr_info_from_graphql_node` so every
  consumer is covered.

---

### F2: `merge_pr` / `close_pr` / `post_comment` pass slug + number to `gh` cleanly, but `fetch_check_runs` interpolates `sha` into a REST path

- **Severity:** LOW
- **Confidence:** LIKELY
- **Location:** `src/gitbulk/gh.py:1258-1266` (`api repos/{slug}/commits/{sha}/check-runs`)
- **Finding:** `sha` originates from `fetch_merge_commit_sha` (gh's own
  `mergeCommit.oid`) so it is gh-trusted, and it is passed as a single
  argv element (not shell-interpolated), so there is no shell injection.
  But there is no `[0-9a-f]{40}` validation; a hostile `gh` could return
  a `sha` containing `../` to pivot the REST path to another endpoint
  (`repos/<slug>/commits/../../<other>/check-runs`). Impact is bounded
  (read-only check-runs call, gh re-encodes most of it), hence LOW.
- **Recommendation:** Validate `sha` against `^[0-9a-f]{7,40}$` before
  building the path. Same hardening family as F1; cheap to do alongside.

---

### F3: No automated secret-scanning gate (pre-commit or CI)

- **Severity:** LOW
- **Confidence:** CONFIRMED
- **Location:** repo-global (`.github/workflows/`, `.githooks/pre-commit`)
- **Finding:** The role prompt calls out secret-scanning as a standard
  control. No `gitleaks`/`trufflehog`/`git-secrets`/`detect-secrets`
  step exists in CI or the pre-commit hook. The repo currently carries
  no secrets (verified: no `.env`, no hardcoded tokens; workflows use
  `${{ secrets.GITHUB_TOKEN }}` correctly), so this is prevention, not
  remediation. Carried forward from the prior review (still open).
- **Recommendation:** Add a `gitleaks` CI step (composite/docker action,
  no node20 deprecation concern) or a pre-commit hook. One-time setup.

---

### F4: GitHub Actions pinned by mutable major tag, not commit SHA

- **Severity:** LOW
- **Confidence:** CONFIRMED
- **Location:** `.github/workflows/ci.yml:15,18,24` (`actions/checkout@v6`,
  `astral-sh/setup-uv@v7`, `actions/setup-python@v6`)
- **Finding:** Third-party actions are referenced by mutable tag, so a
  compromised maintainer or tag-retarget (the tj-actions class of attack)
  could swap the code each pulls. The CI job has `contents: read` only
  and handles no publish/deploy secret, so blast radius is small. Tags
  are already node24-runtime (no deprecation warning). Acceptable
  trade-off for a single-author repo with Dependabot watching actions,
  but the deviation should be recorded. Carried forward from prior review.
- **Recommendation:** Pin to full commit SHAs (Dependabot still bumps
  SHA-pinned actions), or explicitly accept the risk in this.i.

---

## Additional Patterns Noted

- `prefetch_default_branches_chunk` (`gh.py:917-928`) interpolates
  `owner`/`name` into a GraphQL query string, but both are constrained
  by the tightened slug regex (no quotes/backslashes), so injection is
  not reachable. Fine.
- `copilot-review-gate.yml` correctly passes untrusted `PR_TITLE`/labels
  via `env:` and references them as shell variables (no `${{ }}` in the
  `run:` body) — the safe pattern. Good.
- `locks.py:143` still hardcodes `0o644`; relies on the process umask
  set in `cli.main`. If any code path creates a lock without going
  through `main` (e.g. a future library import), the file would be
  world-readable. Belt-and-suspenders: change the literal to `0o600`.
- `pr_info.KNOWN_MERGEABLE_STATES` is still not used to validate incoming
  `mergeable_state`; a hostile `gh` could report `CLEAN` for a dirty PR.
  Merge eligibility is gated by invariants, but tightening at the
  boundary is consistent with F1's recommended fix location.

---

## Residual Unknowns

- `commands/merge.py` and `commands/close_stale.py` were only partially
  read; their git/gh dataflow appears to route through `gh.merge_pr` /
  `gh.close_pr` (server-side, clean argv) rather than local git, so the
  F1 injection class likely does not extend to them — but this was not
  exhaustively traced.
- `this.i` was not read (independence). Some recommendations (action
  SHA-pinning trade-off, secret-scan deferral) may already be
  adjudicated there; if so, treat F3/F4 as already-dispositioned.

---

## Findings manifest

```yaml
findings:
  - id: SEC-F1
    persona: security-hawk
    title: PR ref/SHA values reach positional git args without `--` terminator (arg-injection / command-exec)
    severity: HIGH
    confidence: CONFIRMED
    location: src/gitbulk/rebase.py:100
    dedupe_key: git-ref-args-unsafe
    recommended_disposition: recommend-fix
    rationale: Verified git arg-injection; pr.base_ref/head_ref/head_sha from gh flow unvalidated into git fetch/rebase/worktree-add with no `--`; a `-`-leading ref yields --upload-pack=<cmd> RCE under cron. Fix is a few lines (`--` + ref/sha validation).
    revisit_condition: null
    fix_effort: small
  - id: SEC-F2
    persona: security-hawk
    title: fetch_check_runs interpolates unvalidated sha into REST path
    severity: LOW
    confidence: LIKELY
    location: src/gitbulk/gh.py:1258
    dedupe_key: check-runs-sha-unsafe
    recommended_disposition: recommend-fix
    rationale: sha is gh-trusted and argv-passed (no shell), but unvalidated; a hostile gh could embed ../ to pivot the read-only endpoint. Validate `^[0-9a-f]{7,40}$` alongside the F1 boundary hardening.
    revisit_condition: null
    fix_effort: small
  - id: SEC-F3
    persona: security-hawk
    title: No automated secret-scanning gate (pre-commit or CI)
    severity: LOW
    confidence: CONFIRMED
    location: .github/workflows/ci.yml
    dedupe_key: secret-scanning-missing
    recommended_disposition: recommend-defer
    rationale: No gitleaks/trufflehog/git-secrets configured; repo currently carries no secrets, so this is prevention not remediation. Carried forward from 2026-05-28.
    revisit_condition: Before the repo accepts external contributions or stores any credential/fixture, add a gitleaks CI step.
    fix_effort: small
  - id: SEC-F4
    persona: security-hawk
    title: GitHub Actions pinned by mutable major tag, not commit SHA
    severity: LOW
    confidence: CONFIRMED
    location: .github/workflows/ci.yml:15
    dedupe_key: github-actions-unpinned
    recommended_disposition: recommend-accept-risk
    rationale: Mutable-tag pins are a tj-actions-class supply-chain vector, but the CI job is contents:read with no publish/deploy secret, so blast radius is small; Dependabot watches actions. Residual risk signed off for a single-author repo.
    revisit_condition: If the CI gains a job with write permissions or a publish/deploy secret, switch those actions to SHA pins.
    fix_effort: small
```
