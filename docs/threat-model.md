# gitbulk — Threat Model

*Author's lens: pragmatic CISO / security hawk. This is a risk document, not a
theory paper. It ranks what could actually happen against the cost of stopping
it, and it explicitly names the things I decided **not** to worry about so the
triage is auditable.*

Last updated: 2026-06-03 · Scope: gitbulk as built at `8ecc50a` (v0.6.2),
the workstation it runs on, the GitHub fleet it acts against, and the project's
own software supply chain. Cross-references
`../origin-platform/docs/github-supply-chain-security-standards.md` (cited below
as **SC §n**).

---

## Status / remediation log

The body below is the original point-in-time analysis at `8ecc50a`; it is
preserved as the record. Findings addressed since are tracked here rather than
by editing the analysis.

- **2026-06-05 — SEC-F1 (review-panel): the default `claude` dispatch backend
  no longer runs with the operator's full ambient env — DONE.** Previously the
  no-config path was special-cased to a native `ProductionClaudeClient` that
  emitted `env=None`, so `claude --dangerously-skip-permissions` inherited
  `GH_TOKEN` / `SSH_AUTH_SOCK` / `AWS_*` / npm creds — full RCE-with-identity by
  default, including under cron. The `claude` preset now ships an `env`
  allowlist (its own `ANTHROPIC_*` auth/endpoint vars only) and is driven by
  the same `CommandAgentBackend` as every other agent, so it is *secure by
  default* on equal footing with the non-claude presets (SEC-F2). The
  special-cased `ProductionClaudeClient` / `ProductionAgentBackend` were removed
  entirely. OAuth login is unaffected (credentials live in `~/.claude`, reached
  via `HOME` in the minimal base; `sandbox: none` stays the default). **Still
  open:** filesystem isolation of the dispatch agent (reading `~/.ssh` etc.)
  remains opt-in via `sandbox: fs-only` / `fs+no-net` — `fs+no-net` is
  unavailable to direct-API claude (it needs egress to `api.anthropic.com`),
  and `fs-only` requires `ANTHROPIC_API_KEY` (it shadows `~/.claude`).
- **2026-06-03 — quick wins landed on `main`:**
  - **T2** (no invisible-Unicode / Trojan-Source gate) — **DONE**:
    `scripts/check_unicode.py` + a `unicode-guard` CI job now reject
    zero-width / bidi-control / PUA / variation-selector / tag code points in
    `src/`, `prompts/`, `bin/`, `scripts/`, `.github/` (commit `82f7d9c`).
  - **T4** (CI/release actions on mutable tags; unchecksummed actionlint) —
    **DONE**: all actions SHA-pinned across `ci.yml` / `release.yml`, actionlint
    download checksum-verified, and the github-actions Dependabot updates grouped
    (commits `82f7d9c`, `5581564`).
  - **T6** (the `claude` PATH-hijack asymmetry vs. the `gh` F2 fix) — **DONE**:
    the claude binary resolves via `shutil.which` at construction (commit
    `565372d`; the logic now lives in `gitbulk.agent._pin_binary` after the
    SEC-F1 unification removed `ProductionClaudeClient`). The broader T6
    review-policy / CODEOWNERS items remain open.
- **2026-06-04 — pluggable coding agents + dispatch hardening (branch
  `feat/pluggable-agents`; see docs/pluggable-agents.md, this.i `agbknd7q`…
  `agatk5n`):** this is the substantive remediation of **T1** and adds a new,
  deliberately-accepted surface (the T6/§3.4-4 "config chooses the binary"
  class). Both are reconciled in **§3.5** below. In brief:
  - **T1 — substantially addressed.** The dispatch agent no longer performs any
    networked/credentialed/irreversible git op: gitbulk pre-fetches the base and
    performs the `force-push-with-lease` itself, after independently verifying
    the worktree (the agent's verdict is advisory). The agent's task is now
    local-only, so it can be run env-scoped and inside an unprivileged
    bubblewrap sandbox (`fs+no-net`) that hides `~/.ssh`/`~/.aws`/`~/.config/gh`
    and the other clones and cuts network egress. Foreign-author dispatch
    gating (§3.3-fix item 1) and the `.agent-bin`-on-dispatch idea (item 4)
    remain open. *(this.i `agpriv8n`, `agenv6q`, `agsbx3k`)*
  - **New surface accepted:** agents are now selectable/configurable
    (`agents:` / `--agent`), which is exactly the config-driven-binary pattern
    §3.4-4 flags. Accepted on purpose with compensating controls (no-shell
    argv-lists, `shutil.which` pinning, env allowlist, sandbox); see **§3.5**.
- **Still open / highest-leverage:** T3 (server-side branch protection + token
  scoping — operational), T5 (sign releases), the remaining T1 items
  (foreign-author gating, runtime shim), and the open-source-readiness items in
  §3.4. See §6.
- **Line-number caveat:** the 2026-06-04 work refactored `dispatch.py`,
  `exec.py`, `claude.py`, and `prompts/resolve-conflicts.md`; line references
  below that point into those files predate it and should be re-verified against
  HEAD. The agent invocation now lives behind `gitbulk.agent` /
  `gitbulk.claude.AgentBackend.plan`, not the old `exec._claude_argv`.

---

## 0. TL;DR for the impatient

gitbulk does not — and structurally **cannot** — give your `gh` more GitHub
privilege than your token already has. That framing is a non-threat; I debunk it
in §3.1. The real risks are about **amplification and de-supervision**:

1. **`dispatch` is the crown jewel.** It runs `claude … --dangerously-skip-permissions`
   (exec.py:182) unattended, inside a worktree checked out at a pull request's
   **untrusted head content**, with your *entire* ambient authority — not just
   `gh`'s scope, but your SSH keys, cloud creds, `~/.aws`, `~/.npmrc`, shell
   env, and every clone under `~/code`. The agent is told to read repo files and
   run the repo's own tests/build (prompts/resolve-conflicts.md). That is
   *arbitrary code execution on your workstation, by design*, gated only by the
   honesty of attacker-controlled files and a prompt. **This is the one to fix
   first.** (§3.1, §3.2, §3.3)

2. **gitbulk is a pre-built fleet-spread engine.** If your token or workstation
   is ever popped, the attacker doesn't need to write Megalodon — gitbulk
   already enumerates ~150 repos and ships turnkey unattended merge / push /
   dispatch. It collapses the "spread across the fleet" step of a supply-chain
   attack to near zero. (§3.2)

3. **If open-sourced, the highest-probability attack is a "helpful" PR that
   quietly weakens a guardrail** — and you currently have **no invisible-Unicode
   / Trojan-Source CI gate** (SC §3.4), so the GlassWorm class of change is
   invisible to human review. The guardrails *are* the product. (§3.4)

4. **gitbulk's own release supply chain has standard, fixable gaps**: actions
   pinned to mutable tags (`@v6`/`@v7`, incl. third-party `astral-sh/setup-uv`),
   no SHA pinning (SC §4.2), and a **sha256-without-signature** self-update whose
   integrity hash is published from the *same* compromised-able release as the
   binary it "verifies" (node `shano4kp`). (§3.2.4)

Everything else — shell injection, YAML deserialization, literal privilege
escalation, routine local-data loss — is either well-defended already or low
enough probability that I would not spend the next sprint on it. See §5.

---

## 1. What gitbulk is, in security terms

`gitbulk` is a single-file Python zipapp that runs **unattended from cron** on a
developer's workstation and performs **privileged, bulk, irreversible-ish**
operations across ~150 GitHub repositories: merging PRs, approving PRs,
force-pushing rebases, closing PRs, deleting branches, pruning worktrees, and
**dispatching headless AI agents** into disposable worktrees.

It is, deliberately, an **automation force-multiplier for a privileged human**.
Every security property follows from that sentence.

### The authority it runs with (the blast radius)

gitbulk holds **no credentials of its own** and grants **no privilege of its
own**. It borrows the operator's *ambient authority* in three layers, each wider
than the last:

| Layer | Mechanism | What it can touch |
|---|---|---|
| **GitHub API** | shells out to authenticated `gh` (gh.py) | anything the operator's `gh` token scopes allow, across all fleet repos: merge, approve, comment, close, delete branch refs, push |
| **Local git** | shells out to `git` (worktree.py) | read all clones; create/remove *disposable* worktrees; delete *merged* local branches (the one blessed local mutation) |
| **Full workstation** | spawns `claude --dangerously-skip-permissions` (exec.py, claude.py) | **everything the operator's UID can do** — every file, key, token, and network egress on the box |

Layer 3 is the important one. The first two are bounded by GitHub's server-side
authZ and by gitbulk's local-git safety contract. The third is bounded by
**nothing technical** — only by a prompt and by the agent's compliance.

### The existing guardrails (genuinely good, and worth protecting)

This codebase is unusually disciplined, and credit is due. The defenses I
verified:

- **Local-git safety contract** (AGENTS.md): never mutate working tree / index /
  HEAD / current branch of any clone; checkouts go to disposable worktrees under
  `~/.cache/gitbulk/worktrees/`; verified by `create_worktree`'s `is_relative_to`
  path check and `--detach`-by-SHA (worktree.py:137-170).
- **Dry-run by default**: every mutating subcommand requires `--apply`
  (README status table).
- **Default-branch verification** and a named **invariant chain** gate every PR
  action; skips are logged.
- **Concurrency locks** (global advisory + per-repo) so two runs can't race.
- **Slug hardening** against path traversal: `_SLUG_PATTERN` + forbidden `.`/`..`
  segments (paths.py:22-25, the "security-hawk F1" fix).
- **`gh` PATH-hijack closed**: `ProductionGHClient` resolves `gh` via
  `shutil.which` at construction and uses the absolute path thereafter
  (gh.py:1026-1036, the "security-hawk F2" fix).
- **All subprocess calls are list-form**; no `shell=True`, `os.system`, `eval`,
  or `exec` anywhere in `src/`; **`yaml.safe_load` everywhere**.
- **AI-agent dev shims** (`.agent-bin/{git,gh}`) block `gh pr merge`,
  `git push` to protected branches, and `gh repo delete`.
- **100% branch coverage** enforced in CI, precisely because "an untested
  fallback could be the branch that writes to the main clone."

Hold onto that list — §3.4 is largely about an attacker trying to erode it one
plausible PR at a time.

---

## 2. Threat actors (ranked by realism)

| # | Actor | Realism | Why they care about gitbulk |
|---|---|---|---|
| **A1** | **Malicious / compromised PR author** in a repo the operator dispatches against | **High** if `dispatch` is used with a widened author filter; Medium otherwise | Their PR's *files* become input to a skip-permissions agent on the operator's box |
| **A2** | **Attacker who already has the operator's `gh` token or a foothold on the workstation** (phished, stealer-malware, poisoned IDE/npm/pip dep per SC §1) | **Medium** and rising — this is the 2026 norm | gitbulk is a ready-made, pre-authorized fleet-spread tool |
| **A3** | **Malicious open-source contributor** (only if/when gitbulk is public) | **High, conditional on open-sourcing** | Weaken a guardrail so future runs misbehave; the guardrails are the asset |
| **A4** | **Attacker who compromised gitbulk's GitHub repo / release pipeline** (stolen maintainer token, tag tampering per SC §1) | **Low–Medium** | Poison the official binary → every gitbulk user runs their code unattended |
| **A5** | **The operator themselves, via a fat-fingered config or cron line** | **Medium** (accidents are common) | A wrong `--filter`, `--approve-author`, or `--include-untracked` widens blast radius |
| **A6** | **Network MITM** between gitbulk and GitHub | **Very low** (TLS + `gh`) | Tamper with API responses or update payload |

The two I'd actually plan around are **A1/A2 (dispatch + amplification)** and
**A3 (guardrail erosion)**. A4 is the classic "low probability, catastrophic
impact" tail that deserves cheap structural mitigation, not a fire drill.

---

## 3. The threats

Organized around the four questions in the brief.

### 3.1 "What if gitbulk escalated the operator's privileges — could `gh` suddenly do things it couldn't before?"

**Short answer: no, not in the literal sense — and yes, in the sense that
matters.**

**The literal framing is a non-threat.** `gh` acts with the operator's token;
GitHub enforces authorization **server-side**. Nothing in gitbulk mints scopes,
swaps identities, or impersonates another user. `merge_pr`, `approve_pr`,
`delete_branch_ref`, push — each only succeeds if the operator's token already
permits it (gh.py). There is no code path that grants new capability to the
token. So "gitbulk escalates your GitHub privilege" is **architecturally false**,
and I would not spend a minute defending against it directly.

**The framing that *is* real is "authority amplification / confused deputy."**
gitbulk exercises your *existing* privileges with the friction removed. Three
concrete forms, in descending severity:

**(a) The dispatched agent runs with your FULL ambient authority, not `gh`'s
scope.** This is the actual escalation. A human reviewing a PR is sandboxed by
their own caution; `claude --dangerously-skip-permissions` (exec.py:182,
claude.py:205) is sandboxed by nothing. It can read `~/.aws/credentials`,
`~/.ssh/id_*`, `~/.npmrc`, Vault tokens, and exfiltrate or reuse them — exactly
the secret set the 2026 stealers enumerate (SC §6). Whatever your token *can't*
do, the agent can often do another way (it has your shell, not just your gh).
So the honest statement is not "gh gains privilege" but **"an AI process acting
as you gains your whole identity, unattended, steered by untrusted input."**

**(b) `merge --approve` exercises a *human-reserved* judgment — approval — on
your behalf.** This is the one place gitbulk deliberately spends *maintainer*
authority: it posts an APPROVING review to satisfy branch protection, then
merges (merge.py `approve_pr`, gh.py:1431). The gates are real and layered
(merge.py:322-351): the only skips allowed are `approved_per_policy` /
`age_threshold`; `merge_policy` must be `strict`; author ∈ (`policy.bots` ∪
`--approve-author`); **not** self-approval; and the viewer must hold
`admin`/`maintain`/`write` (`_APPROVE_PERMISSIONS`). That's a well-built gate.
The residual risk is **who controls the allowlist**: `policy.bots` comes from the
config file and `--approve-author` from the invocation/cron line. Widen either
(maliciously per A5, or via a poisoned config) and "auto-approve dependabot" can
become "auto-approve-and-merge an arbitrary author's code you never read." The
gate is sound; its *inputs* are the soft spot.

**(c) Scale removes the human rate-limit.** A person merges a handful of PRs a
day, each glanced at. gitbulk merges/pushes across 150 repos in one unattended
run. The *per-action* privilege is unchanged, but the **effective blast radius
of a single bad decision or poisoned input is escalated from one repo to the
whole fleet** — which is precisely the enabling condition behind the 2026
mass-compromise campaigns (Megalodon: 5,561 repos in 6 hours, SC §1).

> **Likelihood / impact:** (a) Medium-likelihood, **critical** impact.
> (b) Low-likelihood (gates are good), high impact. (c) is a multiplier on
> everything else.
>
> **Recommendations:**
> - **Scope the token to the job.** gitbulk needs `repo`-level merge/push and
>   nothing else; it does **not** need `delete_repo`, org admin, workflow, or
>   packages scopes. A fine-grained PAT restricted to the fleet repos, with a
>   short max lifetime (SC §2.1, §5), caps layers 1–2 even if the agent in
>   layer 3 tries to abuse them. Track time-to-revoke.
> - **Treat `policy.bots` and `--approve-author` as security-critical config.**
>   Keep `~/.config/gitbulk/` mode `0700`, owned by the operator, and consider
>   logging the effective allowlist into the run manifest on every `--approve`
>   run so widening is auditable after the fact.
> - **The dispatch fixes in §3.3 are the real mitigation for (a).**

---

### 3.2 "How could gitbulk be leveraged for supply-chain attacks via GitHub?"

Map gitbulk onto SC §1's unifying chain — *steal a credential → push malicious
code / publish a malicious package → that code steals more credentials →
repeat*. gitbulk touches **three** links, and is itself a fourth target.

**3.2.1 gitbulk as the "spread across the fleet" payload (the big one).**
This is what makes gitbulk attractive to A2. Post-compromise, the attacker
inherits a tool that already:
- knows every repo (`repos.txt`),
- runs unattended on a schedule (cron),
- has `merge --apply`, `rebase-pr --apply` (force-push-with-lease), and
  `dispatch --apply` wired and authorized.

They don't author Megalodon; they **edit one of three files and wait for cron**:
`~/.config/gitbulk/gitbulk.yaml` (policy/allowlist), the dispatch prompt
(`~/.config/gitbulk/prompts/*.md`), or the crontab line. A malicious dispatch
prompt = AI-driven arbitrary changes committed across the fleet; the bundled
`resolve-conflicts.md` even ends in `git push --force-with-lease`
(prompts/resolve-conflicts.md:53). gitbulk has effectively **pre-built the
worm's logistics layer**. The mitigation is not in gitbulk's code — it is
**server-side branch protection on the 150 repos** (SC §2.2, §3.1: PR required,
no force-push, required review, signed commits). That is the control that makes a
stolen-credential fleet push *fail* regardless of what drives it, and it is
SC's own #1 priority. gitbulk's local guardrails do not, and cannot, substitute
for it.

**3.2.2 `dispatch` executing untrusted PR code = the workstation vector (SC §6).**
SC is explicit that malware "runs on package import / on install," and that you
must "run untrusted code … in a sandbox/VM, not on the machine that holds your
credentials." gitbulk's `dispatch` does the opposite on purpose: it checks out a
PR head and runs an agent that the prompt directs to execute the repo's own
`pytest -q` / `npm test` / `make check` (prompts/resolve-conflicts.md:43-49) and
to read its files — **on the credential-bearing workstation, unattended.** A
malicious `Makefile`, `conftest.py`, `package.json` `postinstall`, or test
fixture in the PR is arbitrary code execution. (Full treatment in §3.3.)

*One incidental, load-bearing-but-accidental mitigation:* `create_worktree` does
`git worktree add --detach <target> <pr_head_sha>` against the **local** clone
(worktree.py:151-158), so it can only check out a SHA already fetched locally.
A pure fork-PR head that was never fetched will fail worktree creation and be
skipped. This narrows A1 to PRs whose head SHA is in the operator's clone
(branches pushed to the upstream, or fetched fork refs) — but it is a **side
effect of the SHA-checkout design, not a deliberate trust boundary**, and it
would silently evaporate if anyone added a `git fetch <pr-ref>` step. Don't rely
on it; make it explicit (§3.3).

**3.2.3 gitbulk as a writer of GitHub Actions / CODEOWNERS.** The `codeowners`
dispatch prompt writes `.github/CODEOWNERS` and lets gitbulk push/PR it
(prompts/codeowners.md). Two angles: (i) a subverted dispatch agent could just
as easily write `.github/workflows/*.yml` — the exact Megalodon payload (SC §1)
— and gitbulk would carry it to the remote; (ii) even un-subverted, the
codeowners logic = "push-access ∩ committers in last 60 days," so a compromised
contributor who lands **one** commit and holds push can get themselves added as
a code owner, then approve future PRs. Subtle, but it's a privilege-laundering
path.

**3.2.4 gitbulk's OWN supply chain (target A4).** This maps one-to-one onto SC
§4–§5, and it has real, fixable gaps:

- **Self-update is sha256-only, no signature (node `shano4kp`).** `gitbulk
  update` fetches `update.json` from `releases/latest`, downloads the binary,
  and checks its sha256 against the manifest with a constant-time compare
  (update.py:230 — good hygiene). But **the hash and the binary come from the
  same release**: an attacker who can publish a release (stolen maintainer token
  / tag tampering, SC §1) controls both, and the sha256 "verification" passes.
  There is no offline signing key, so the check defends against transit
  corruption, not against a compromised publisher — the precise gap SC §5's
  "trusted publishing / artifact attestation" closes. **Mitigating factors that
  keep this Medium not Critical:** update never auto-applies, is TTY-gated, is
  suppressed from cron (`GITBULK_NO_UPDATE_CHECK=1`, bin/gitbulk-cron:45), and
  requires a human to run it. But the binary is the **root of trust for all the
  unattended automation**, so a single post-compromise `gitbulk update` is
  durable, total, and runs forever with your creds.
- **CI/release actions are pinned to mutable major tags, not SHAs**
  (ci.yml / release.yml: `actions/checkout@v6`, `actions/setup-python@v6`, and
  the **third-party** `astral-sh/setup-uv@v7`). SC §4.2 is categorical: pin to a
  full-length commit SHA, never `@vN`, because tags can be silently retargeted
  (tj-actions, Laravel-Lang). `setup-uv@v7` runs in the **release** job, which
  has `contents: write` and publishes the binary every user downloads — a tag
  retarget there poisons the official artifact. The `actionlint` job also does
  `curl … | tar` of an actionlint release with **no checksum** (ci.yml:92-95):
  another fetch-and-execute on a mutable upstream.
- **PyYAML is vendored from the build machine** at bundle time (bundle.py
  `_vendor_yaml`). If the builder's PyYAML is ever a trojanized version (the
  durabletask/Shai-Hulud PyPI vector, SC §1/§7), the malicious `.py` is baked
  into every gitbulk binary. The `.so` is stripped, but pure-Python payloads
  survive. Builds should install from the committed `uv.lock` with hashes
  (which CI does — `uv sync --frozen`) and the bundle should vendor *that*
  resolved, hash-checked PyYAML, never an ambient one.

> **Likelihood / impact:** 3.2.1 Medium/critical (conditional on a prior
> compromise, but turns a foothold into a fleet breach). 3.2.2 High/critical if
> dispatch is used broadly. 3.2.4 Low–Medium/critical-to-all-users.
>
> **Recommendations (cheap → structural):**
> - **Branch protection + org rulesets on the fleet** (SC §2.2, §3.1). This is
>   the highest-leverage control and it lives outside gitbulk. Do this first.
> - **Pin every action to a SHA** in `ci.yml`/`release.yml`; let Dependabot bump
>   them; checksum the actionlint download. (Also satisfies the user's standing
>   "GitHub Actions versions" rule.)
> - **Move release to OIDC / artifact attestation** (SC §5, §2.3) and/or add a
>   detached signature (minisign/cosign) to the release, verified by `gitbulk
>   update` against a key **baked into the binary** — so a release compromise
>   can't also forge the signature. This converts §3.2.4 from "sha256 theater"
>   into real publisher authentication.
> - **Vendor PyYAML from the locked, hash-pinned resolution only.**
> - Add **secret scanning + push protection** and **Dependabot malware alerts**
>   on the gitbulk repo itself (SC §2.3).

---

### 3.3 "How could gitbulk destroy local, unpushed data that isn't on any remote?"

Two very different answers depending on whether you mean **deterministic
gitbulk** or **the dispatched agent**.

**Deterministic gitbulk: low risk, because the local-git safety contract is
real and well-tested.** By construction gitbulk never touches the primary
clone's working tree, index, HEAD, or branch. The only local-mutation surfaces:

- **`prune-worktrees`** removes *linked* worktrees and deletes *merged* local
  branches. The guardrails are genuinely strong: never the main worktree
  (path-verified, worktree.py:401); `git worktree remove` **without** `--force`
  so git itself refuses a dirty/locked tree (worktree.py:405); `git branch -d`
  (lowercase) refuses an unmerged branch (worktree.py:418); a grace period; and
  an unpushed-commit check via `rev-list --count <branch> --not --remotes`
  (worktree.py:371). Fail-safe defaults: unreadable git state ⇒ treat as
  dirty/in-progress and **keep** (worktree.py:310, 354).
  - **The one knob that *can* destroy never-saved data: `--include-untracked`**
    (prune_worktrees.py:166-169). It overrides the untracked-files guard, so a
    worktree full of untracked, never-committed scratch files becomes eligible
    for removal. A malicious or careless cron line carrying that flag (A5) is
    the realistic way deterministic gitbulk eats unpushed work. Keep it
    human-only; never put it in an unattended cron entry.
- **`prune-branches`** deletes *remote* branch refs (not local data) via the
  GitHub ref API, records the deleted SHA for recovery, and has its own
  no-data-loss guard (`branch_ahead_by`). Not a local-destruction vector.

**The dispatched agent: this is where local data actually dies.** The worktree
under `~/.cache/gitbulk/worktrees/` is a **convention, not a sandbox.** Nothing
technically confines `claude --dangerously-skip-permissions` to it — no
container, namespace, seccomp, or filesystem boundary. The agent's *cwd* is the
worktree and the *prompt* tells it to "Operate only inside this worktree … never
touch the user's main clone" (prompts/resolve-conflicts.md:23-26), but that is a
**polite request to a non-deterministic process reading attacker-controlled
files.** A subverted or prompt-injected agent (§3.2.2) can `cd ~/code/anything`
and `rm -rf`, `git reset --hard`, or `git stash drop` uncommitted work in any
clone — destroying exactly the unpushed data the local-git safety contract was
built to protect, by going *around* the contract rather than through it.
Separately, `remove_worktree` uses `git worktree remove --force` (worktree.py:189)
which **discards uncommitted changes in the dispatch worktree itself** — that is
intentional (the worktree is disposable) and *not* a finding, but it means any
work the agent left uncommitted there is gone on cleanup; nothing the agent
produces is safe unless it commits or pushes it.

> **Likelihood / impact:** deterministic — Low/medium (bounded, recoverable-ish,
> guarded). Agent-driven — Medium/**high** and it shares a root cause with
> §3.2.2.
>
> **Recommendations:** see §3.3-fix below, shared with the dispatch hardening.

#### §3.3-fix / §3.2.2-fix — Contain the dispatch agent (the single most valuable change)

The whole "agent" cluster (§3.1a, §3.2.2, §3.3) collapses to one root cause:
**a non-deterministic process with full ambient authority reads untrusted input
and is confined only by a prompt.** Fixes, in priority order:

1. **Make `dispatch` opt-in to untrusted authorship, loudly.** Today the author
   filter "may widen … default @me" (dispatch.py:570-575) with no special
   ceremony. Require an explicit, scary flag (e.g. `--allow-foreign-authors`)
   before dispatch will operate on any PR not authored by the operator, and
   refuse it outright in unattended/cron mode. Default-@me dispatch on your own
   PRs is far lower risk than agent-on-a-stranger's-code.
2. **Run the agent in a real sandbox**, not just a worktree cwd: a container or
   VM with (a) **no access to `~/.aws`, `~/.ssh`, `~/.npmrc`, gh token, env
   secrets**, (b) a **read-only bind of everything except the worktree**, and
   (c) **egress allow-listed** to GitHub only (SC §2.4 runner-egress idea,
   applied to the workstation). This converts "by-design RCE with your identity"
   into "RCE in a box with a scoped, short-lived push credential."
3. **Give dispatch its own least-privilege push credential**, distinct from the
   operator's interactive gh token, scoped to push feature branches and nothing
   else — so a subverted agent can't merge, can't delete, can't reach other
   orgs.
4. **Make the worktree confinement enforced, not advisory** even short of full
   sandboxing: drop the agent's environment of secret-bearing vars, and consider
   the `.agent-bin` shim approach (which already blocks `gh pr merge` / protected
   pushes for agent shells) on the dispatch PATH so the *runtime* agent — not
   just dev-time agents — is shimmed. Right now the shims are a **dev-time**
   guardrail; the production dispatch agent is unshimmed.
5. **Keep `--include-untracked` and any author-widening flag out of cron.**

---

### 3.4 "If I open-sourced this, how could a malicious contributor undermine the security guarantees?"

This is, pragmatically, the **most likely** of the four to actually happen the
day gitbulk goes public — and the most interesting, because **the guardrails are
the asset and they're all in readable Python.** The attacker is a "helpful"
contributor whose PR looks like a bugfix or a feature and quietly erodes a
defense. Ranked by leverage:

**(1) Invisible-Unicode / Trojan-Source — and you have no gate for it (SC §3.4).**
This is the top finding for an open-source gitbulk. SC is blunt: *you cannot
catch this by eye.* GlassWorm hid a decoder + `eval` feed in PUA/zero-width
characters; the Trojan-Source class uses bidi controls to make a check *read*
one way and *execute* another. gitbulk has **no CI check rejecting non-printable
/ PUA / bidi characters** (verified: nothing in `.github/` or `scripts/` scans
for it). A contributor could hide a payload in any `.py` file and it would sail
through human review. **Cheap, high-value fix: add a pre-commit + CI gate that
rejects U+FE00–FE0F, U+E0100–E01EF, zero-width, and bidi-control characters in
`src/`** (SC §3.4). This is the single cheapest item in this document relative to
the risk it closes.

**(2) Loosen a merge/eligibility gate.** The gates in merge.py:322-351 are
exactly where a one-line "fix a false-negative" PR is most dangerous: treat
`reviewDecision == null` as approved; drop the not-self check; add `triage` to
`_APPROVE_PERMISSIONS`; or make the default `bots` tuple non-empty. Each looks
like a usability tweak and each turns auto-merge into auto-merge-anything.

**(3) Open a prompt-injection channel that doesn't exist today.** Currently no
PR text is interpolated into the dispatch prompt (verified: dispatch.py:700-705
passes the static `prompt_text` and `input_text=None`). A plausible-sounding PR —
"give the agent the PR title/body/diff for context" — would create a **direct**
indirect-prompt-injection vector into a skip-permissions agent. Treat any change
that flows PR/issue/branch text into a prompt or `run:` block as security-
critical (mirrors SC §4.4 for Actions).

**(4) Make config control which binary runs.** The agent binary is pinned via
`shutil.which` at construction (`gitbulk.agent._pin_binary`, parity with the
`gh` client's F2 fix), so PATH cannot substitute it. A contributor could
"improve testability/config"
by adding a config-driven `claude_path` / `gh_path` — instantly turning a config
file (or a poisoned one) into **arbitrary binary execution**, re-opening the very
PATH-hijack the F2 fix closed. Any PR that lets config or CLI choose an
executable path is a red flag.

**(5) Weaken the local-git safety contract.** The highest-impact target: change
`git worktree remove` to add `--force` in the prune path; relax `create_worktree`'s
`is_relative_to` check; add a `git fetch <pr-ref>` that defeats the incidental
fork-PR mitigation (§3.2.2); or change `--detach` to a branch checkout (which
would move a branch in the main clone). AGENTS.md already names this as the most
acute risk class — make sure review enforces it.

**(6) Erode the test/coverage safety net.** Because correctness here is *proven
by tests at 100% branch coverage*, the subtle move is to weaken a **test** or the
**FakeGHClient/FakeClaudeClient** so a dangerous real-client behavior change
still shows "covered." A PR that loosens a `Fake`'s assertions, or that adds a
`# pragma: no cover` over a new branch, can smuggle a behavior change past the
gate that's supposed to stop it. (The project's own `resolve-conflicts.md`
forbids agents from weakening tests — apply the same rule to human PRs.)

**(7) Weaken the self-update / supply-chain code.** "Simplify" the
`hmac.compare_digest` to `==` (timing oracle); change the update manifest URL;
add a second update channel; unpin a CI action; or **add a new third-party
runtime dependency** (today the only one is PyYAML — every addition widens the
attack surface and the vendor-from-builder risk in §3.2.4).

**(8) Disable a shim.** A PR removing or weakening `.agent-bin/{git,gh}` or the
AgentPrep block (AGENTS.md) re-opens agent-driven dangerous ops in dev. Lower
impact than 1–5 (shims are dev-time), but it's a quiet erosion.

> **Likelihood / impact:** (1) High/high — and currently **ungated**. (2),(4),(5)
> Medium/critical. (3),(6) Medium/high. (7),(8) Low–Medium.
>
> **Recommendations for going open-source:**
> - **Add the invisible-Unicode/Trojan-Source CI + pre-commit gate** (item 1).
>   Do this *before* the first external PR.
> - **CODEOWNERS the guardrail files** so a human maintainer must review any
>   change to: `worktree.py`, `paths.py`, `exec.py`, `claude.py`,
>   `commands/merge.py`, `commands/dispatch.py`, `commands/prune_worktrees.py`,
>   `update.py`, `bundle.py`, the `invariants/` chain, `.agent-bin/`, and
>   `.github/workflows/`. (gitbulk literally has a tool to maintain CODEOWNERS —
>   use it on itself.)
> - **Branch protection on `main`**: PR required, ≥1 review, dismiss stale
>   approvals, **block force-push**, **require signed commits**, required status
>   checks incl. the Unicode gate and the 100% coverage gate (SC §3.1).
> - **Dependency review + "no new runtime deps without maintainer sign-off"** as
>   a written policy (SC §2.3).
> - **A SECURITY.md** that names the trust boundaries above so reviewers know
>   which PRs are security-sensitive, and a private disclosure channel.
> - **Require the `merge`/`dispatch`/contract changes to cite a `this.i` node** —
>   the project already enforces intent-first design; lean on it as a review
>   tripwire for guardrail changes.

---

### 3.5 Pluggable coding agents — a deliberately-accepted surface (2026-06-04)

The `feat/pluggable-agents` work lets gitbulk drive coding agents other than
Claude (Gemini, Copilot, Cursor, or a custom CLI), selected by config
(`default_agent:` / per-repo `agent:`) or `--agent`, and configured by an
`agents:` block (built-in presets + a custom `command` template). **This is
exactly the "config/CLI chooses which binary runs" pattern that §3.4(4) / T6
names a red flag.** It is accepted on purpose, because the *capability* it adds
to an attacker who can already write `~/.config/gitbulk/` is **nil** — that
attacker is A2, who already has the workstation and can edit the crontab or the
dispatch prompt (§3.2.1) — while the controls it ships **reduce** what a
less-trusted *backend* can do. The compensating controls (this.i `agtmpl9k`):

- **No shell, ever.** `command`/`model_args` are argv **lists**; a scalar string
  is a hard config error. `{prompt}`/`{model}` substitute *within a single
  token*, so attacker-influenceable prompt/worktree text can never split into
  extra arguments. There is no shell to inject into. (Closes the obvious
  regression of §5's "shell/argv injection is well-defended".)
- **Binary pinned.** `command[0]` is resolved via `shutil.which` at load and
  used as an absolute path thereafter — the same F2 fix, now generalized to
  every backend, so a later `PATH` prepend cannot substitute it. A relative path
  that doesn't resolve is rejected.
- **Least privilege + verify (T1 fix).** Whatever backend runs, it never pushes;
  gitbulk verifies and pushes. A malicious/confused backend's blast radius is
  "garbage in a throwaway worktree," caught before any remote mutation.
- **Env allowlist + sandbox.** A backend gets only the env vars its profile
  names (no ambient `GH_TOKEN`/SSH/cloud creds by default) and can be confined
  to an unprivileged bwrap namespace; an unavailable sandbox **refuses** by
  default rather than silently running unconfined.

**Residual risks (named, not eliminated):**

- **Config is now security-critical in one more way.** Whoever can write
  `gitbulk.yaml` can point `command` at any binary and choose `env`/`sandbox`.
  This is the same trust level as `policy.bots`/`--approve-author` (§3.1b) and
  the dispatch prompt (§3.2.1): keep `~/.config/gitbulk/` mode `0700`,
  operator-owned. The effective agent argv (prompt elided) should be logged per
  run so the granted authority is auditable.
- **The auto-approve flag is still the enabler.** Each preset bakes in the
  backend's `--dangerously-skip-permissions` / `--yolo` / `--allow-all-tools`
  equivalent — mandatory for unattended runs, and the thing that makes the
  sandbox (not the prompt) the real boundary.
- **Less-trusted backends honor prose constraints less reliably than Claude.**
  This is precisely why the sandbox/least-privilege controls, not the prompt,
  are load-bearing for non-Claude agents — and why `claude` remains the trusted
  default served by the native client.

**Post-review correction (2026-06-04, this.i `agsecr5n`).** An adversarial
security review of this very feature found the sandbox was initially
**non-functional** — it bound a linked worktree whose `.git` points into the
operator clone, so git couldn't run, and it was tested only by argv-shape
assertions (never e2e). Fixed: sandboxed agents now run in a self-contained
clone (this.i `agecln4k`), validated by a real-`bwrap` e2e test with a
regression control (`agtste9k`). The review also found least privilege was
opt-in (now secure-by-default presets) and there was no foreign-author gate (now
added). See `agsecr5n` for the full disposition of all five findings.

**Threat → control → test matrix** (each row has an adversarial test in
`tests/test_agent_security.py` / `tests/test_rebase.py` / `tests/test_dispatch.py`,
this.i `agatk5n`):

| Threat | Control | Test |
|---|---|---|
| Prompt-content command injection | argv-lists, one-token sub, no shell | `test_prompt_metacharacters_stay_one_argv_token`, `test_scalar_command_string_is_rejected` |
| Binary PATH-hijack (T6) | `shutil.which` pin; relative-path reject | `test_binary_pinned_via_which`, `test_relative_command_path_that_is_missing_is_rejected` |
| Credential exfil via inherited env (T1) | per-profile env allowlist | `test_scoped_env_excludes_ambient_secrets` |
| Read `~/.ssh` / other clones (T1) | bwrap fs scoping | `test_wrap_does_not_bind_credentials_or_home`, `test_fs_no_net_sandbox_cuts_network_and_hides_creds` |
| Network exfil (T1/§3.2.2) | `--unshare-net` | `test_wrap_fs_no_net_unshares_network` |
| Silent sandbox downgrade | refuse-if-unavailable | `test_sandbox_refuses_when_host_cannot_provide_it`, `test_dispatch_default_agent_sandbox_unavailable_refuses` |
| Agent pushes arbitrary refs (T1/§3.2.1) | agent never pushes; gitbulk owns push | `test_dispatch_resolved_ready_gitbulk_pushes` |
| Verdict spoofing (`RESOLVED` w/o work) | independent verify-before-push | `test_dispatch_resolved_blocked_pushes_nothing_and_flags_attention`, `test_verify_*` |

## 4. Prioritized findings

Ranked by my pragmatic read of *likelihood × impact × cheapness-to-fix*. "Fix
cost" is rough engineering effort.

| ID | Finding | Likelihood | Impact | Fix cost | Priority |
|---|---|---|---|---|---|
| **T1** | `dispatch` agent runs `--dangerously-skip-permissions` with full ambient authority on untrusted PR content, confined only by a prompt (§3.1a/§3.2.2/§3.3) | Med–High | **Critical** | Med–High | **P0 — largely mitigated 2026-06-04 (§3.5): agent never pushes, env-scoped, bwrap-sandboxable; foreign-author gating still open** |
| **T2** | No invisible-Unicode / Trojan-Source CI gate; guardrails are reviewable Python (§3.4-1) | High *(if OSS)* | High | **Low** | **P0** |
| **T3** | gitbulk is a turnkey fleet-spread engine post-compromise; mitigated only by server-side branch protection that isn't gitbulk's to enforce (§3.2.1) | Med | **Critical** | Low *(config the fleet)* | **P0** |
| **T4** | CI/release actions pinned to mutable tags (`@v6/@v7`, third-party `setup-uv`), unchecksummed actionlint download; poisons the binary all users run (§3.2.4) | Low–Med | Critical (all users) | **Low** | **P1** |
| **T5** | Self-update is sha256-without-signature; hash shares the compromised-able release (§3.2.4) | Low–Med | Critical (all users) | Med | **P1** |
| **T6** | Open-source contributor loosens a merge/eligibility gate or adds config-controlled `claude_path`/`gh_path` (§3.4-2,4) | Med | Critical | Low *(review policy + CODEOWNERS)* | **P1** |
| **T7** | `merge --approve` allowlist (`policy.bots`, `--approve-author`) widenable by config/cron (§3.1b) | Low | High | Low | **P1** |
| **T8** | `prune-worktrees --include-untracked` can delete never-saved local files (§3.3) | Med *(A5)* | Med | Low *(doc/policy)* | **P2** |
| **T9** | PyYAML vendored from ambient build env, not the locked resolution (§3.2.4) | Low | High | Low | **P2** |
| **T10** | Dispatch's fork-PR safety is incidental (SHA-must-be-local), not a deliberate boundary (§3.2.2) | Low | High | Low *(make explicit + test)* | **P2** |
| **T11** | Contributor weakens a test / `Fake*` client to smuggle a behavior change past coverage (§3.4-6) | Med *(if OSS)* | High | Med | **P2** |
| **T12** | `.agent-bin` shims are dev-time only; the production dispatch agent is unshimmed (§3.3-fix-4) | Med | Med | Med | **P2** |
| **T13** | codeowners/Actions-writing dispatch could carry a `.github/workflows` payload to the fleet (§3.2.3) | Low | High | Med | **P3** |

---

## 5. What I deliberately did NOT prioritize (auditable triage)

A threat model earns trust by saying what it *isn't* worried about and why.

- **Literal `gh` privilege escalation** — architecturally impossible (§3.1).
  No mitigation needed beyond not re-introducing it.
- **Shell / argv injection** — all subprocess calls are list-form; no
  `shell=True`/`os.system`/`eval`/`exec` in `src/`; slugs pass `_SLUG_PATTERN`
  before any interpolation, including the GraphQL alias path (gh.py:1224-1234).
  Well-defended; low residual.
- **YAML deserialization RCE** — `yaml.safe_load` everywhere; the vendored
  zipapp ships only the pure-Python `SafeLoader`. Non-issue.
- **Path traversal via config slugs** — closed by the F1 fix (paths.py:22-25),
  with defense-in-depth `.`/`..` rejection.
- **`gh` PATH-hijack** — closed by the F2 fix (gh.py:1026-1036). (Note the
  `claude` asymmetry is captured as T6, not here.)
- **Network MITM of GitHub traffic** — TLS via `gh`; updates ride authenticated
  `gh release download`. Very low; not worth effort over the signature work in
  T5.
- **Local cache poisoning** (org-members / default-branch caches under
  `~/.cache/gitbulk/` to widen the `author_known` gate) — requires local write
  access, at which point the attacker already has the workstation and bigger
  options (T1). Marginal; noted, not prioritized.
- **DoS / resource exhaustion** — gitbulk self-throttles (bounded pool,
  per-target timeouts, page caps). Not a meaningful adversarial target for a
  personal automation tool.
- **`remove_worktree --force` discarding worktree-local changes** — intentional
  (the worktree is disposable); a property, not a finding.

---

## 6. The one-page action plan

If you do only a handful of things, in order:

1. **Lock down the fleet server-side** (branch protection / org rulesets, SC
   §2.2/§3.1) and **scope the gh token** (SC §2.1/§5). Biggest blast-radius
   reduction, lives outside gitbulk. *(T3, T1, T7)*
2. **Sandbox the dispatch agent** and gate foreign-author dispatch behind an
   explicit, non-cron flag; give it a least-privilege push credential. *(T1,
   §3.3-fix)*
3. **Add the invisible-Unicode/Trojan-Source CI + pre-commit gate.** Cheapest
   high-value control; do it before open-sourcing. *(T2)*
4. **Pin CI/release actions to SHAs; checksum the actionlint fetch; vendor
   PyYAML from the lockfile.** *(T4, T9)*
5. **Sign releases with a key baked into the binary; verify in `gitbulk
   update`.** *(T5)*
6. **CODEOWNERS the guardrail files + branch-protect `main` with signed commits
   + a documented "security-sensitive PR" review policy.** *(T6, T11, and the
   §3.4 list)*
7. **Keep `--include-untracked` and author-widening out of cron; document it.**
   *(T8)*

---

*Code references are to commit `8ecc50a` (v0.6.2). Re-verify line numbers after
any refactor of `dispatch.py`, `merge.py`, `worktree.py`, or `exec.py`.*
