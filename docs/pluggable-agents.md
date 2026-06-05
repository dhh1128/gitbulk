# Pluggable coding agents

Status: **implemented 2026-06-04** on `feat/pluggable-agents` (Phases 1–5, full
suite green at 100% branch coverage). This document is the design contract; the
locked decisions also live as `this.i` nodes `agbknd7q`, `agprof4k`, `agtmpl9k`,
`agpriv8n`, `agdang5k`, `agenv6q`, `agsbx3k`, `agtok2n`, `agatk5n`. The
threat-model reconciliation is in `docs/threat-model.md` §3.5.

---

## 1. Goal and shape

Today gitbulk shells out to exactly one coding agent — Claude Code — and the
invocation is hardcoded in two places:

- `gitbulk.claude.ProductionClaudeClient.run_prompt` (used by `summarize`), and
- `gitbulk.exec._claude_argv` (used by `dispatch`'s parallel kernel).

Both build the same argv: `claude -p <prompt> --model <m> --dangerously-skip-permissions`.

The goal is to formalize that seam so gitbulk can drive **any** CLI coding agent
(Claude, Gemini CLI, GitHub Copilot CLI, Cursor agent, or a fully custom tool)
through a small, config-driven interface — *without* weakening, and ideally
strengthening, gitbulk's safety posture.

Two user-chosen forks anchor the design:

1. **Config shape — presets + custom template.** Built-in presets for the
   common agents (one line to pick one) *and* a raw command-template escape
   hatch for anything else.
2. **Scope — full layered security model**, not just the seam: least-privilege
   (gitbulk performs every networked/irreversible git op), independent
   verification before any push, a scoped-token hook, and per-profile OS
   sandboxing (bubblewrap).

The agent contract that is *already* agent-neutral and stays so:

- Prompts are plain Markdown passed to the agent verbatim.
- The outcome protocol is gitbulk's, not Claude's: the agent ends with one
  `RESOLVED: …` / `ESCALATED: …` line (see `dspesc4q`). Every backend gets the
  same prompt and is held to the same protocol.
- gitbulk independently re-checks worktree state; the verdict is advisory.

---

## 2. The seam: `AgentBackend` (this.i `agbknd7q`)

Generalize the existing `ClaudeClient` Protocol (Protocol + Fake + Production)
into `AgentBackend`, keeping `ClaudeClient`/`ProductionClaudeClient`/
`FakeClaudeClient` as deprecated aliases so nothing breaks during migration.

A single argv builder replaces the two hardcoded ones. `exec.py` stops
constructing argv itself and asks the backend, so `dispatch` and `summarize`
share one code path that turns

```
(prompt, input_text, model, cwd, timeout, env) → (argv, stdin?, env, timeout)
```

into an invocation. `exec.py` keeps its own `Popen` (it needs the live handle
for SIGTERM→SIGKILL escalation, per `execk7nm`) but sources the argv/stdin/env
from the backend's `plan(...)` method rather than reading `_claude_path` /
`_default_model` directly.

Backend surface (sketch):

```python
@dataclass(frozen=True)
class AgentInvocation:
    argv: list[str]            # fully resolved, absolute binary at argv[0]
    use_stdin: bool            # True → prompt delivered on stdin, not in argv
    env: dict[str, str]        # the EXACT environment (already scoped)
    timeout: float

class AgentBackend(Protocol):
    def plan(self, prompt, *, input_text, model, working_directory, timeout) -> AgentInvocation: ...
    def run_prompt(self, prompt, *, input_text=None, model=None, timeout=None, working_directory=None) -> str: ...
```

`run_prompt` (used by `summarize`) is implemented in terms of `plan` +
`subprocess.run`. `exec.py` calls `plan` and drives `Popen` itself.

---

## 3. Agent profiles in config (this.i `agprof4k`)

A new optional `agents:` block plus `default_agent:` in `gitbulk.yaml`, and a
per-repo `agent:` override that reuses the existing `repos.<slug>` override
machinery.

```yaml
default_agent: claude            # global default; omitted → claude

agents:
  # Built-in presets may be referenced by name with zero config.
  # Listing one here only to OVERRIDE a field (e.g. model) is allowed.
  claude:
    model: claude-sonnet-4-6
  gemini:
    model: gemini-2.5-pro
  # A fully custom backend:
  myagent:
    command:    [mytool, run, "{prompt}"]   # argv list; never a shell string
    model_args: [--model, "{model}"]        # appended only when a model is set
    model:      my-default-model
    prompt_via: stdin                        # arg | stdin   (default: arg)
    timeout:    1800
    env:        [MYTOOL_API_KEY]             # allowlist (see §6)
    sandbox:    fs+no-net                     # none | fs-only | fs+no-net

repos:
  owner/repo:
    agent: gemini                            # per-repo override
```

### Built-in presets

Code-defined defaults so the common case needs only `default_agent: <name>`.
The auto-approve flags below are the dangerous, mandatory part (see §5) and are
baked into each preset deliberately, where they are visible and auditable. **The
exact flags must be verified non-deprecated at implementation time** (per the
user's standing rule); the table is the intended shape, not a verified spec.

| name      | base command (illustrative)                                  | prompt_via | model flag |
|-----------|--------------------------------------------------------------|------------|------------|
| `claude`  | `claude -p {prompt} --dangerously-skip-permissions`          | arg        | `--model`  |
| `gemini`  | `gemini -p {prompt} --yolo`                                   | arg        | `-m`       |
| `copilot` | `copilot -p {prompt} --allow-all-tools`                       | arg        | `--model`  |
| `cursor`  | `cursor-agent -p {prompt} --force`                            | arg        | `--model`  |

A user `agents.<name>` block deep-merges over the preset of the same name
(override `model`, `timeout`, `env`, `sandbox`; replace `command` only if given).

### Placeholder rules (security-critical, this.i `agtmpl9k`)

- `command` and `model_args` are **lists of argv tokens**. A scalar string is a
  hard config error (no `shell=True`, ever — this is the single most important
  rule, because prompts and worktree contents carry attacker-influenceable text).
- `{prompt}` and `{model}` substitute as a whole token, or as a substring of one
  token (e.g. `-p={prompt}`) — still exactly one argv element either way, so
  there is no argument-splitting foot-gun.
- `command` must contain exactly one `{prompt}` token **unless** `prompt_via:
  stdin`, in which case it must contain none (validated at load).
- `model_args` is appended only when a model is in effect; an agent that takes no
  model just omits the block — no dangling `--model`.
- The binary (`command[0]`) is resolved via `shutil.which` at load and stored as
  an absolute path (mirrors the `gh`/claude F2 fix), so a later `PATH` prepend
  cannot substitute it. A relative path that doesn't resolve is a config error.

### CLI

`--agent NAME` on `dispatch` and `summarize`. `--model` keeps overriding the
profile's model. Resolution order: `--agent` → per-repo `agent:` →
`default_agent` → built-in `claude`.

### Backward compatibility

With no `agents:`/`default_agent:` config, behavior is byte-identical to today
(implicit `claude` preset reproducing the current argv). Existing configs, cron
jobs, and the 1556-test suite are unaffected.

---

## 4. Least privilege: gitbulk owns every networked git op (this.i `agpriv8n`)

**The pivotal security change.** Today (`prompts/resolve-conflicts.md`) the
*agent* runs the only networked, credentialed, irreversible operation in the
flow: `git fetch`, then `git push --force-with-lease`. We move those into
gitbulk:

1. **gitbulk pre-fetches** the base into the worktree before launching the agent
   (gitbulk has creds; this is audited code reusing `rebase.py`'s helpers).
2. **The agent only rebases + edits files + emits a verdict.** `git rebase` is
   purely local once the base is fetched, so this task needs **no network and no
   credentials** — which is exactly what makes the §7 sandbox tight.
3. **gitbulk verifies, then pushes.** After the agent returns, gitbulk
   independently checks: no conflict markers (`is_worktree_in_conflict`), HEAD
   advanced as expected, only the PR's own head ref was touched, optional cheap
   test pass. Only then does gitbulk call `rebase.force_push_with_lease(...)`
   itself.

This makes a single invariant true across **all** backends:

> **The agent never touches a remote. gitbulk performs every networked
> mutation.**

`codeowners.md` and `migrate-*.md` already follow this ("commit locally, gitbulk
pushes/PRs"); pulling the push out of `resolve-conflicts` makes the rule
uniform. A buggy or prompt-injected agent can no longer push arbitrary refs; the
worst case is garbage in a throwaway worktree, which verification catches before
any push. The `dspesc4q` verdict-surfacing and `vp7n2krq` conflict-preservation
behaviors are preserved; the rebase/escalation choreography in the prompt is
rewritten so the agent stops at "resolved locally" or "escalated" and never
pushes.

---

## 5. Auto-approve flags are mandatory and dangerous (this.i `agdang5k`)

`--dangerously-skip-permissions` / `--yolo` / `--allow-all-tools` / `--force`
are what make unattended runs possible *and* what remove every in-agent safety
net. They live explicitly in each profile so the user consciously opts each
agent into full autonomy. gitbulk **persists the exact effective argv** (prompt
elided) plus the sandbox wrapper and the env-var *names* per target in that
run's `dispatch-logs/<key>.meta.yaml` (`agent_argv` / `agent_env_keys`), so the
authority granted to which binary is auditable after the fact (SEC-F5).

The verdict stays advisory: gitbulk never trusts `RESOLVED:` as proof that work
happened — §4's independent verification gates every irreversible op.

---

## 6. Environment scoping (this.i `agenv6q`)

A subprocess inherits the *entire* environment — every agent would otherwise get
your `GH_TOKEN`, SSH agent socket, and all API keys. Each profile gets an
optional `env` **allowlist**: only the named variables (plus a minimal safe base:
`PATH`, `HOME`, `LANG`, `TERM`, …) are passed through, and per-agent extras
(e.g. `GEMINI_API_KEY`) can be injected. **The built-in non-Claude presets are
secure by default (SEC-F2): each ships an `env` allowlist** (gemini → its API
key; cursor → `CURSOR_API_KEY`; copilot → `GH_TOKEN`/`GITHUB_TOKEN`, since it
authenticates via GitHub — prefer a scoped token there), so `default_agent:
gemini` does **not** hand the agent your `GH_TOKEN`/SSH/AWS. Omitting `env` on a
custom profile still inherits the full environment (the backward-compatible
escape hatch), but that is a foot-gun. Note env scoping stops *environment*-borne
leakage only; filesystem isolation (`~/.ssh` etc.) needs the §7 sandbox.

With §4 in force, the `resolve-conflicts` agent needs *no* credentials at all, so
its env can be scrubbed down to the bare toolchain minimum.

Secrets-in-argv note: `-p <prompt>` exposes the prompt via `/proc/<pid>/cmdline`.
Low risk on a single-user box and current prompts hold no secrets, but
`prompt_via: stdin` is preferred where the agent supports it.

---

## 7. OS sandbox via bubblewrap (this.i `agsbx3k`)

Defense-in-depth — **not** the primary control (that is §4 + §6). Per-profile
`sandbox:` policy:

- `none` — no sandbox (today's behavior; default for backward compat).
- `fs-only` — bwrap with `$HOME` shadowed (`--tmpfs`), `~/.ssh`/`~/.aws`/
  `~/.config/gh` and the other ~149 clones unmounted, only the worktree bound
  rw and a read-only toolchain. Network still available.
- `fs+no-net` — `fs-only` plus `--unshare-net`: zero network. **Only viable for
  tasks that need neither network nor creds — which, thanks to §4, includes
  `resolve-conflicts`.** This is the tightest, recommended policy for that class.

**Workspace (SEC-F1, this.i `agecln4k`).** A linked `git worktree` cannot run
inside the sandbox — its `.git` is a pointer into the operator's clone
(objects/refs/config/hooks), which the sandbox does not bind, and binding it
would re-expose the clone's hooks to the auto-approve agent. So a **sandboxed
agent gets a self-contained `git clone --no-hardlinks`** instead: its own `.git`
(no shared objects/hooks/config), `origin` reset to the real remote,
`core.hooksPath` neutralized, the head fetched + checked out by gitbulk
*outside* the sandbox. The agent then runs bound to that directory alone — git
works, and there is no filesystem path from the agent to the operator's clone,
other repos, or credentials. Unsandboxed/claude agents keep the cheaper linked
worktree. This is validated by a **real-`bwrap` e2e test** (`tests/e2e/`,
auto-skips when bwrap/userns are absent), with a regression control proving the
linked-worktree approach fails — the test that the original argv-shape-only
suite lacked (this.i `agtste9k`).

Mechanics:

- A `wrapper:` prefix in the resolved invocation (`[bwrap, <args...>, <agent
  argv...>]`) — so sandboxing composes with the §2 seam without reworking it.
- A capability **probe** at startup (is `bwrap` installed? are unprivileged user
  namespaces enabled? — WSL2 usually yes, some hardened distros no).
- **Refuse-if-unavailable** by default: if a profile requests a sandbox and the
  host can't provide it, gitbulk refuses to run that target rather than silently
  downgrading to unsandboxed (a silent downgrade defeats the purpose). A config
  knob (`sandbox_fallback: refuse | warn-run`) can relax this.
- The bind set is part of the agent contract; kept minimal precisely because the
  §4-shrunk task needs almost nothing mounted.

Cost/benefit summary: high benefit, low cost for the network-less/cred-less
`resolve-conflicts` class; degrades to "hide unrelated creds + other repos"
(still worthwhile) for tasks that genuinely need network, where it should be
opt-in. Linux-only; adds a dependency + probe. Containers/firejail were
considered and rejected (heavier / setuid attack surface) for a single-box cron
tool; bwrap reuses the host toolchain unprivileged.

---

## 8. Scoped-token hook (this.i `agtok2n`)

Even with §4, some tasks (e.g. a future `codeowners`-style agent that must read
remote state) need a token. The design leaves a seam to mint a **short-lived,
single-repo** credential (fine-grained PAT or GitHub App installation token,
`contents`/`pull_requests` scoped to the one repo) and inject only that via the
§6 allowlist, instead of the full ambient `gh` auth. Blast radius on leak = one
repo, expires fast. Phase 4 lands the seam (an injectable provider returning
per-target env); the actual minting integration is follow-on.

---

## 9. Layering and ordering of controls

Highest leverage per cost first:

1. **Least privilege (§4)** — agent never performs networked/irreversible ops.
   No new dependency; works everywhere; enables everything below. *Do regardless.*
2. **Independent verification before any irreversible op (§5)** — already
   gitbulk's design; made a hard cross-backend rule.
3. **Scoped credentials (§8)** — when a token is unavoidable.
4. **Sandbox (§7)** — defense-in-depth, per-profile, capability-probed.

---

## 11. Threat model + adversarial TDD (this.i `agatk5n`)

This feature is **security-sensitive by definition** and must be reconciled with
`docs/threat-model.md`, not just bolted on:

- It is the substantive fix for **T1 (P0)** — "the dispatch agent runs with full
  ambient authority, confined only by a prompt." §4 (agent never performs
  networked/irreversible ops), §6 (env scoping), and §7 (bwrap) implement
  exactly the §3.3-fix / action-plan-item-2 controls. The threat model's
  remediation log must record T1 as substantially addressed.
- It **deliberately introduces** the surface the threat model flags as a red
  flag in **§3.4(4) / T6**: *config/CLI choosing which binary runs.* We accept
  this on purpose and must document the compensating controls inline in the
  threat model: (a) `command[0]` pinned via `shutil.which` at load; (b)
  argv-lists only, no `shell=True` ever (a scalar `command` is a hard error);
  (c) the config is operator-owned `0700` trusted state, and anyone who can
  write it already has A2-level workstation access — so this adds *no* privilege
  an attacker at that level lacks, while §4/§6/§7 *reduce* what a compromised or
  less-trusted **backend** can do. The net change is a security *improvement*.
- Line-number references in `threat-model.md` (and its "re-verify after refactor"
  footnote) covering `dispatch.py`/`exec.py`/`claude.py` must be refreshed, since
  this branch refactors all three.

### Adversarial TDD — each threat gets a failing test first

The "malicious agent" is a controlled fixture; containment lives in deterministic
gitbulk code, so we assert the code cages it. Red → control → green. Hermetic:
no network, no real `bwrap` required for the core matrix (real-`bwrap` tests
auto-skip when the capability probe fails). Behavioral tests use a real *local*
git repo with a local bare "origin" so pushes are real-but-offline.

| Threat (TM ref) | Adversarial test (written first) | Control |
|---|---|---|
| Command injection via prompt/worktree content (§5 list-form; new template) | prompt = `"; rm -rf ~ $(curl x) \n --inject"` → lands as exactly one argv token; scalar `command` rejected | argv-lists only, whole-token sub, no shell (§3) |
| Binary PATH-hijack (T6/§3.4-4) | `shutil.which` stub → pinned abs path; unresolved relative → config error | `shutil.which` at load (§3) |
| Cred exfil via inherited env (T1/§3.1a) | env seeded with `GH_TOKEN`/`AWS_*`/`SSH_AUTH_SOCK` → planned env excludes them | env allowlist (§6) |
| Agent reads `~/.ssh`/other repos (T1/§3.3) | `fs+no-net` → composed bwrap argv binds only worktree+toolchain; `~/.ssh` in no bind | bwrap fs scoping (§7) |
| Agent network exfil (T1/§3.2.2) | `fs+no-net` → `--unshare-net` present | bwrap `--unshare-net` (§7) |
| Silent sandbox downgrade | probe "unavailable" + sandbox requested → target refused; `warn-run` → warns+runs | refuse-if-unavailable (§7) |
| Agent pushes arbitrary refs (T1/§3.2.1) | the closure handed to the agent has no push capability; push only in gitbulk's verified path | least privilege (§4) |
| Verdict spoofing (`RESOLVED:` w/o resolving) (§5) | agent leaves conflict markers/HEAD unmoved but prints RESOLVED → `force_push_with_lease` NOT called; treated as attention | verify-before-push (§4/§5) |
| Worktree-escape / main-clone clobber (§3.3) | (real-bwrap integration, auto-skip) write outside worktree fails | bwrap fs scoping (§7) |

Tests land in `tests/test_agent_security.py` (+ per-phase additions), each tagged
to its TM finding so the threat→control→test matrix is auditable, mirroring the
project's existing adversarial-review discipline (methodology §10).

---

## 12. Documentation deliverables

Treated as first-class work, not a finalize-step afterthought:

- **Threat model** (`docs/threat-model.md`): remediation-log update for T1; a new
  subsection reconciling the deliberate T6/§3.4-4 surface with its compensating
  controls; refreshed line refs; the threat→control→test matrix above.
- **User docs**: `docs/configuration.md` (the `agents:`/`default_agent:`/per-repo
  `agent:` schema, presets, custom template, `env`, `sandbox`), `docs/commands.md`
  (`--agent` on dispatch/summarize), `docs/running-unattended.md` (sandbox
  prerequisites, refuse-if-unavailable behavior in cron, recommended profiles),
  and `config/gitbulk.yaml.example`.
- **Dev docs**: `docs/architecture.md` (the `AgentBackend` seam + `plan()`,
  gitbulk-owns-push flow), `AGENTS.md` (the new cross-backend invariant "the
  agent never touches a remote"; sandbox/env rules for contributors), and the
  `this.i` nodes (`agbknd7q`, `agprof4k`, `agtmpl9k`, `agpriv8n`, `agdang5k`,
  `agenv6q`, `agsbx3k`, `agtok2n`, `agatk5n`).
- **README** status/feature note.

---

## 10. Implementation phases (TDD throughout; baseline 1556 tests green)

Each phase is **adversarial-test-first**: write the failing security test(s) from
the §11 matrix that the phase is responsible for, then implement until green.

1. **Seam** (§2): generalize Protocol; unify argv via `plan()`; aliases; no
   behavior change. *(security tests: none new; refactor stays green.)*
2. **Profiles** (§3, §6): config schema + presets + custom template + env
   allowlist + `--agent`; binary pinning; no-shell validation. *(tests:
   command-injection, PATH-hijack, env-exfil, scalar-command-rejected.)*
3. **Least privilege** (§4): gitbulk pre-fetch + verify + push; rewrite
   `resolve-conflicts.md`; cross-backend "agent never pushes" invariant.
   *(tests: verdict-spoofing, agent-has-no-push, verify-before-push.)*
4. **Sandbox + token hook** (§7, §8): bwrap wrapper, probe, refuse-if-unavailable;
   scoped-token provider seam. *(tests: fs-scoping argv, `--unshare-net`,
   refuse-if-unavailable, warn-run, real-bwrap integration auto-skip.)*
5. **Threat model + docs + finalize** (§11, §12): update `threat-model.md`
   (T1/T6 reconciliation, line refs, matrix); user + dev docs;
   `gitbulk.yaml.example`; verify each agent CLI's flags non-deprecated with
   dated comments; full suite green at the coverage bar; incremental signed-off
   commits.
