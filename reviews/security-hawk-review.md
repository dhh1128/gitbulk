# Security Review: gitbulk

**Date:** 2026-06-05
**Effort level:** deep
**Mode:** unattended (orchestrated)
**Reviewed commit:** e03c42a (v0.7.1), main; clean working tree
**Context sources used:** AGENTS.md, docs/threat-model.md, SECURITY.md, README,
prior reviews/security-hawk-2026-05-28.md + reviews/security-review.md (read
AFTER forming an independent model), and direct reading of `agent.py`,
`sandbox.py`, `isolated_clone.py`, `worktree.py`, `rebase.py`, `claude.py`,
`paths.py`, `update.py`, `gh.py`, `commands/dispatch.py`, `commands/merge.py`,
`scripts/check_unicode.py`, and all four `.github/workflows/*.yml`.

---

## Evidence Inventory

Read in full: the security-critical core (agent backend, sandbox, isolated
clone, worktree, rebase/push, claude/gh clients, dispatch handler, merge
approval gates, paths/slug guard, self-update, unicode guard, CI/release
workflows). Grepped the whole `src/` tree for `shell=True`/`os.system`/`eval`/
`exec` (none), invisible-Unicode code points (none in src/prompts/bin/scripts),
PEM/token secret patterns (none committed), and action pins (all SHA-pinned).
Did NOT run the test suite (read-only review) and did NOT run an external CVE
scanner — the only runtime third-party dependency is PyYAML, vendored from the
hash-pinned `uv.lock`; the lockfile is committed. CVE scan therefore noted as
not-performed but blast radius is a single, well-known pure-Python dep.

This codebase has an unusually mature, accurate threat model
(`docs/threat-model.md`, T1–T13) and two prior adversarial security reviews. I
built my own model first, then verified the threat model's "remediation log"
claims against HEAD. Most are genuinely landed: SHA-pinned actions, checksummed
actionlint download, the Trojan-Source Unicode CI gate, `gh`/`claude` binary
pinning via `shutil.which`, the foreign-author dispatch gate, secure-by-default
env allowlists for non-claude agents, the bwrap sandbox running on a
self-contained clone (not a linked worktree), and gitbulk-owns-the-push with
independent verify-before-push. Confidence on the auth/authz model is HIGH; this
is a personal automation tool with no server-side authz surface of its own.

---

## Executive Summary

gitbulk is well-defended and the existing threat model is honest about its one
structural hazard: `dispatch` runs an auto-approving coding agent with the
operator's authority against PR content. The pluggable-agent hardening (env
allowlist, bwrap sandbox, foreign-author gate, verify-before-push) is real and
tested. The most actionable residual is that **all of those new controls apply
only to non-`claude` backends** — the default, no-config, cron path runs
`claude --dangerously-skip-permissions` with the *full ambient environment*
(GH_TOKEN, SSH, AWS) and *no filesystem sandbox*, so on your own PRs the agent
still has full RCE-with-your-identity. The cheapest unaddressed gap is the
absence of any automated secret-scanning gate. Nothing here is a fresh CRITICAL;
the items below refine residuals the threat model already names.

---

## Top Findings

Ordered by bang-for-buck.

### F1: Default `claude` dispatch backend runs unsandboxed with full ambient env, including under cron
- **Severity:** HIGH
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/agent.py:569-576` (the `name == "claude"` branch returns a bare `ProductionClaudeClient`); `src/gitbulk/claude.py:345-358` (`env=None`, no sandbox)
- **Finding:** The hardening shipped in `feat/pluggable-agents` — per-profile `env` allowlist (`_scoped_env`) and the bwrap `sandbox` policy (`wrap_argv`) — is wired only into `CommandAgentBackend`. `backend_for` short-circuits the `claude` agent to the native `ProductionClaudeClient`, whose `plan()` returns `env=None` (the child inherits the operator's *entire* environment: `GH_TOKEN`, `SSH_AUTH_SOCK`, `AWS_*`, `~/.npmrc`, etc.) and is never wrapped in a sandbox. Since `claude` is the default agent and `--dangerously-skip-permissions` is mandatory for unattended runs, the default cron dispatch path is exactly the T1 "RCE with your whole identity" surface — the secure-by-default presets do not cover it.
- **Exploit path:** A prompt-injected or subverted agent (steered by attacker-controlled files in a PR head — even your own PR can contain a malicious `conftest.py`/`Makefile` the agent is told to run) reads `~/.aws/credentials` / `~/.ssh/id_*` from the inherited env and filesystem and exfiltrates them, or pivots into the other ~149 clones. The foreign-author gate (F-good, already present) and verify-before-push cap *who* and *what gets pushed*, but not what the agent can read/exfiltrate locally once running.
- **Recommendation:** Let the `claude` profile carry an `env` allowlist and a `sandbox` policy too, and route the default agent through the same `CommandAgentBackend` scoping (or add env-scoping+sandbox support to `ProductionClaudeClient`). At minimum, default the unattended/cron claude path to `fs+no-net` (viable because gitbulk pre-fetches the base and owns the push, so the agent needs neither network nor creds) and an env allowlist excluding credential-bearing vars. This is the threat model's own §3.3-fix items 2 and 4, applied to the default backend rather than only the optional ones.

### F2: No automated secret-scanning gate (CI or pre-commit)
- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** repo-wide; `.github/workflows/` (no gitleaks/trufflehog/detect-secrets job), `.githooks/pre-commit` (AgentPrep only), no `.pre-commit-config.yaml`
- **Finding:** There is a Trojan-Source Unicode gate and an actionlint gate, but no gate that blocks a committed credential. A secret committed to git is permanently compromised in history even after removal. gitbulk handles credential-adjacent material constantly (tokens reach `gh`, env allowlists name `GH_TOKEN`/`CURSOR_API_KEY`, example configs exist) — exactly the place a fixture or `.env` slips a real value in. The prior security review (F3, 2026-05-28) and the threat model's §3.4 recommendations both flag this; it remains unaddressed.
- **Exploit path:** A contributor (or the operator) commits a real token in a test fixture, `gitbulk.yaml` example, or doc; it is published to a public repo on open-sourcing and harvested by automated scrapers within minutes.
- **Recommendation:** Add a `gitleaks` (or `detect-secrets`) CI job and a pre-commit hook with a pinned action SHA. Cheap, high-value, and a prerequisite the threat model already lists for going public.

### F3: `git` binary is invoked unpinned (bare `"git"`) everywhere, unlike `gh`/`claude`
- **Severity:** LOW
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/worktree.py:66,202,305,349`; `src/gitbulk/rebase.py:86`; `src/gitbulk/isolated_clone.py:45`; `src/gitbulk/invariants/catalog.py:188,210,260`
- **Finding:** The F2-class PATH-hijack fix pinned `gh` and `claude` to absolute paths via `shutil.which` at construction, but every `git` subprocess still uses the bare name `"git"`, resolved against `$PATH` at each call. This is an asymmetry in the same defense: a `PATH` prepend (e.g. by a process sharing the operator's environment, or a dependency that mutates `PATH`) substitutes the `git` gitbulk runs — including the trusted, *outside-the-sandbox* clone/fetch/push operations in `isolated_clone.py` and `rebase.force_push_with_lease`.
- **Exploit path:** Lower than F1's because it requires write control over the operator's own PATH, at which point most things are lost; but `git` is the binary that performs the credentialed push and the clone of operator repos, so pinning it closes the same gap the gh/claude fix valued enough to close.
- **Recommendation:** Pin `git` once via `shutil.which("git")` at a single seam (the `_git_run`/`_git`/`_run` helpers) and reuse the absolute path, mirroring the gh/claude clients. Treat any future "make git path configurable" PR as a red flag (per threat-model §3.4-4).

### F4: Trojan-Source Unicode gate excludes `tests/`, but test code is executed
- **Severity:** LOW
- **Confidence:** CONFIRMED
- **Location:** `scripts/check_unicode.py:57` (`DEFAULT_ROOTS` omits `tests`); comment at lines 52-56 makes the omission deliberate
- **Finding:** The Unicode gate scans `src`, `prompts`, `bin`, `scripts`, `.github` but intentionally not `tests/`. Tests run in CI and define the `FakeGHClient`/`FakeClaudeClient` doubles that the 100%-coverage gate relies on. The threat model's T11 names "weaken a Fake/test to smuggle a behavior change past coverage" as a real open-source attack; an invisible-Unicode payload in a test file is the GlassWorm-flavored version of exactly that, and it is currently ungated. The stated reason (a test may legitimately embed a control character) is reasonable but leaves the gap.
- **Exploit path:** A "helpful" test-refactor PR hides a payload (e.g. a bidi-reordered assertion, or a PUA-encoded decoder) in `tests/`; it renders benign in the GitHub review UI, runs in CI, and could neutralize a Fake's assertion that's standing in for a real guardrail.
- **Recommendation:** Scan `tests/` too but allow a narrowly-scoped, reviewed allowlist (per-file `# noqa: unicode` or an explicit exceptions list) for the rare legitimate control-character test, rather than excluding the whole tree.

### F5: `update.read_payload` accepts `http://` despite the "https only by construction" claim
- **Severity:** LOW
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/update.py:140-142` (`read_payload` routes both `http://` and `https://` to `fetch_bytes`); `update.py:133` (`urlopen(url ...)  # noqa: S310 (https only by construction)`)
- **Finding:** `script_url` comes from the release manifest. For a non-GitHub-release URL, `_gh_fetch` returns `None` and the code falls back to `urlopen(url)`. The S310 suppression asserts "https only by construction," but `read_payload` explicitly accepts an `http://` source and passes it straight through — the construction does not actually guarantee https. The downloaded payload is sha256-checked (`hmac.compare_digest`), so this is integrity-gated, not an RCE; but a plaintext fetch is a needless cleartext/MITM exposure and the noqa comment is misleading for future maintainers.
- **Exploit path:** A manifest whose `script_url` is `http://...` (a misconfiguration, or an attacker who already controls the manifest — the threat model's T5 publisher-compromise case) causes a cleartext download; the sha256 check still defends integrity, so impact is limited to traffic exposure/MITM-tamper-then-fail.
- **Recommendation:** Reject `http://` in `read_payload` (require `https://`/`file://`/GitHub-release), making the S310 claim true; pairs naturally with the threat model's T5 release-signing work.

---

## Additional Patterns Noted

- **T1's documented residual (foreign-author + sandbox) is genuinely the right framing** — verify-before-push (`rebase.verify_resolved_for_push`), gitbulk-owns-push, and the no-TTY refusal of `--allow-foreign-authors` are all present and correct. F1 above is the one piece that didn't reach the *default* backend.
- **`copilot` preset necessarily allowlists `GH_TOKEN`/`GITHUB_TOKEN`** (`agent.py:169`) — unavoidable for that agent's auth, but it means the copilot backend gets a fleet-capable token; the in-code comment already flags "prefer a scoped token." Worth the scoped-token seam (`agtok2n`) being used in practice, not just available.
- **GraphQL alias interpolation is safe** — owner/name are slug-regex-validated before interpolation (`gh.py:1299-1305`), and the regex rejects quotes/backslashes; consistent with threat-model §5.
- **Self-update sha256-without-signature (T5)** and **branch-protection-is-not-gitbulk's-to-enforce (T3)** remain the standing operational items; both already tracked.
- **PyYAML vendored from the build env (T9)** — CI uses `uv sync --frozen`, so the resolved dep is hash-pinned; confirm the bundle vendors *that* resolution, not an ambient one.

---

## Residual Unknowns

- Could not run a live CVE scanner (offline review); PyYAML is the only runtime
  third-party dep and is lockfile-pinned, so residual dependency risk is low but
  not formally scanned.
- Whether the operator's actual `~/.config/gitbulk/` is mode 0700 and whether
  the fleet has server-side branch protection (T3) are deployment facts outside
  the repo; both are the threat model's named highest-leverage controls.

---

## Decisions Needed

- **Is the default-claude unsandboxed/full-env path (F1) an accepted risk or a
  fix?** The threat model accepts T1 as "largely mitigated" but the mitigations
  bypass the default backend. Decide whether to extend env-scoping + sandbox to
  the default `claude` path (recommended) or to formally accept that the default
  unattended dispatch retains full RCE-with-identity and document it loudly at
  the CLI.

---

```yaml
findings:
  - id: SEC-F1
    persona: security-hawk
    title: Default claude dispatch backend runs unsandboxed with full ambient env (incl. under cron)
    severity: HIGH
    confidence: CONFIRMED
    location: src/gitbulk/agent.py:569-576
    dedupe_key: dispatch-agent-unsafe
    recommended_disposition: recommend-fix
    rationale: env allowlist + bwrap sandbox apply only to non-claude backends; the default no-config cron path inherits GH_TOKEN/SSH/AWS and has no fs isolation while running --dangerously-skip-permissions.
    revisit_condition: null
    fix_effort: medium
  - id: SEC-F2
    persona: security-hawk
    title: No automated secret-scanning gate (CI or pre-commit)
    severity: MEDIUM
    confidence: CONFIRMED
    location: .github/workflows/ (absent); .githooks/pre-commit
    dedupe_key: secret-scanning-missing
    recommended_disposition: recommend-fix
    rationale: Unicode + actionlint gates exist but nothing blocks a committed credential; a secret in git history is permanently compromised. Prior review F3 still open.
    revisit_condition: null
    fix_effort: small
  - id: SEC-F3
    persona: security-hawk
    title: git binary invoked unpinned (bare "git") unlike gh/claude which-pinning
    severity: LOW
    confidence: CONFIRMED
    location: src/gitbulk/worktree.py:66; src/gitbulk/rebase.py:86; src/gitbulk/isolated_clone.py:45
    dedupe_key: git-binary-unpinned
    recommended_disposition: recommend-fix
    rationale: The F2-class PATH-hijack fix pinned gh and claude but not git, which performs the credentialed push and operator-clone operations; asymmetry in the same defense.
    revisit_condition: null
    fix_effort: small
  - id: SEC-F4
    persona: security-hawk
    title: Trojan-Source Unicode gate excludes tests/, but test code is executed
    severity: LOW
    confidence: CONFIRMED
    location: scripts/check_unicode.py:57
    dedupe_key: unicode-gate-tests-excluded
    recommended_disposition: recommend-defer
    rationale: An invisible-Unicode payload in a test/Fake could neutralize a guardrail-standing assertion and is currently ungated (threat-model T11 territory).
    revisit_condition: Before open-sourcing / accepting external PRs that touch tests/
    fix_effort: small
  - id: SEC-F5
    persona: security-hawk
    title: update.read_payload accepts http:// despite "https only by construction" noqa
    severity: LOW
    confidence: CONFIRMED
    location: src/gitbulk/update.py:140-142
    dedupe_key: update-payload-cleartext
    recommended_disposition: recommend-fix
    rationale: read_payload passes an http:// script_url straight to urlopen; integrity is sha256-gated but the S310 https-only claim is false and allows a needless cleartext/MITM fetch.
    revisit_condition: null
    fix_effort: small
```
