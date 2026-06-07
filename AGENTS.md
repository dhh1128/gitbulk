# AGENTS.md — Behavioral rules for AI agents (and humans) working on gitbulk

This file is the non-negotiable contract for anyone — human or AI — making
changes to this repository.

---

## What gitbulk is for

`gitbulk` runs unattended (often from cron) against ~150 git repositories
the user contributes to. It performs sensitive operations: merging PRs,
force-pushing rebases, closing stale PRs, dispatching headless AI agents
into worktrees. **A bug in gitbulk can damage real work in real repos.**
TDD is not aspirational here; it is the only acceptable workflow.

---

## TDD discipline (mandatory)

For any change to `src/gitbulk/`:

1. **Read** the existing test file(s) covering the code you will change.
2. **Run** `pytest -q` to establish a green baseline.
3. **Write or modify** the test first if you are changing behavior.
4. **Write or modify** the implementation.
5. **Run** `pytest -q` again; iterate until green.
6. **Commit only when tests pass.** Sign off with `git commit -s`.

Summary: `read-run-change-run-commit`. Never commit code that fails tests.

---

## Hard rules

### Local-git safety contract (the most important rule)

`gitbulk` must never:

- Modify the working tree of any clone under `~/code/<repo>` (no `git checkout`,
  no `git pull`, no `git reset`, no `git stash`, no file writes).
- Modify the index or `HEAD` of any clone.
- Change the user's current branch in any clone.

Operations that require a checkout MUST use `git worktree add` into a
disposable path under `/tmp/gitbulk/` (or a configured worktree root) and
MUST clean up the worktree in a `finally` block.

Any code path that calls `git -C ~/code/<repo>` with a mutating subcommand
is a defect. Read-only subcommands (`rev-parse`, `status --porcelain`,
`config --get`, `remote get-url`, `branch --show-current`, `log`,
`worktree list`) are fine.

**One blessed local mutation (node `wtrm6kpq`):** `gitbulk prune-worktrees`
may run `git -C <clone> worktree remove <linked-worktree>` (and, for a
fully-merged branch, `git -C <clone> branch -d <branch>`). This is permitted
*only* because it never touches the working tree, index, `HEAD`, or current
branch of the **primary** clone the user edits — it removes a separate
*linked* worktree and prunes admin metadata. It is gated on the same
path verification `create_worktree` uses: the target MUST resolve to a real
linked worktree of the clone and MUST NOT be the main worktree path. Removal
uses `git worktree remove` **without** `--force` (so git's own dirty/lock
refusals stand), never `rm -rf`.

### Worktree path verification

Before any operation that writes inside a worktree, the code MUST verify
that the worktree path resolves under the configured worktree root and is
not the same as the main clone. This prevents bugs where a worktree-creation
failure causes subsequent operations to fall back to the main clone.

### Concurrency

Two `gitbulk` processes must be safe to run at the same time. Locking is
**resource-scoped**: a lock protects one shared resource and its scope is
exactly that resource, never wider (this.i node `rsclk7nq`,
`docs/design/resource-scoped-locking.md`). Each shared resource has its own
keyed `fcntl.flock`: per-subcommand run-state (`run_state_lock`), per-repo
clone+remote mutation (`repo_lock`), the org-members and default-branch
caches, the ATTENTION sentinel, the dashboard, and the watchdog-ack cache.
The single global `run.lock` of the original two-lock model (`lj5pqn4kr`) has
been retired in favour of these. A mutating run on repo A and any operation
on repo B never contend; `show <sub>` never contends with a run of a
*different* subcommand. New subcommands MUST declare which resource locks
they take (and in which mode) explicitly, and acquire them in the documented
order (`org -> default_branches -> repo(slug) -> run_state(sub) -> sentinel
-> dashboard`) if more than one is ever held at once. Every atomic file
write uses `gitbulk.util.atomicio` (unique temp names) so concurrent writers
of the same file never collide.

### Default branch detection

Every operation that touches a PR must verify that the PR's `baseRefName`
equals the repo's current default branch on GitHub. If not, the PR is
skipped with a prominent reason in the report. Override via
`--allow-non-default-base` only.

### Mutating subcommands default to dry-run

Every mutating subcommand (`merge`, `close-stale`, `rebase-onto-default`,
`dispatch`) defaults to `--dry-run` and requires `--apply` to actually act.
A misconfigured cron entry must not silently merge things.

### The dispatched agent never touches a remote (cross-backend invariant)

gitbulk can drive coding agents other than Claude (`agents:` / `--agent`; see
[`docs/pluggable-agents.md`](docs/pluggable-agents.md), this.i `agbknd7q`…).
Regardless of which backend runs, **the agent must never perform a networked,
credentialed, or irreversible git operation** — no `git fetch`, no `git push`,
no `gh pr merge`/close/delete. gitbulk pre-fetches the base before launching the
agent and performs the `force-push-with-lease` itself, only after independently
verifying the worktree (`rebase.verify_resolved_for_push`); the agent's
`RESOLVED:`/`ESCALATED:` verdict is **advisory and never trusted as proof of
work** (this.i `agpriv8n`). Any change that re-introduces a push/fetch into a
dispatch prompt, or makes gitbulk push on the verdict alone without
verification, is a defect.

Related hard rules for the agent layer:

- **Command templates are argv lists, never shell strings**; a scalar
  `command`/`model_args` is a config error. `{prompt}`/`{model}` substitute
  within a single token. No `shell=True`, anywhere (this.i `agtmpl9k`).
- **The agent binary is pinned via `shutil.which`** at load — never let config
  or CLI choose an arbitrary executable path that isn't pinned.
- **Env reaching the agent is an allowlist** (`agenv6q`); don't widen the safe
  base to include credential-bearing vars.
- **A requested sandbox that's unavailable refuses by default** (`agsbx3k`);
  don't change `sandbox_fallback` to silently downgrade.

These are guarded by `tests/test_agent_security.py` — treat any change that
weakens those tests as security-sensitive (mirrors the threat-model §3.4-6
warning about eroding the `Fake*`/test safety net).

### Invariants are first-class

New operations that touch repos or PRs must be expressed as a chain of
named invariants from the registry. Skipping invariants is allowed only
via `--skip-check NAME`, and every such skip MUST be logged into the run
state with a WARNING.

### No network in tests

Tests MUST NOT call `gh`, `git fetch`, or any other network operation.
Subprocess and network dependencies are injected so tests stay offline,
deterministic, and fast.

### Verify gh invocations against GitHub API deprecations

Every `gh` command that lands in `ProductionGHClient` (or any other
production code path that subprocesses to `gh`) MUST be verified to not
emit a GitHub API deprecation warning at the time it is wired in. The
`gh` CLI surfaces deprecation notices on stderr in the form
`Warning: …` or `…is deprecated and will be removed on YYYY-MM-DD`.

Verification procedure:

1. Run the candidate command interactively against a real repo:
   `gh <args> 2>&1 1>/dev/null | grep -iE 'warning|deprecat'`.
2. If the grep matches anything, switch to the recommended alternative.
   Common moves: REST endpoint flagged → use GraphQL or the new REST
   endpoint named in the warning; GraphQL field deprecated (e.g.,
   `mergeable` → `mergeStateStatus`) → use the new field.
3. Record verification at the call site:
   `# verified non-deprecated against gh CLI YYYY-MM-DD`.

If no clean replacement exists yet, record a `tension:` node in
`this.i` with the deprecation deadline, or a `TECH_DEBT` comment with
the deadline. Do not let a known-deprecated command land silently.

The rationale and full procedure are recorded in the global memory
`feedback-gh-cli-deprecation-verification`. Decision node
`ghclmp7n` (gh Client Implementation) makes the rule a hard
requirement for Phase 2 code.

### Coverage standard

100% branch coverage on `src/gitbulk/`, enforced in CI. A gap requires
an approved `deviation:` node in `this.i` (see `docs/methodology.md` §6);
a gap without one is a defect, not a judgment call. The framing — "a bug
in gitbulk can damage real work in real repos" — applies most acutely
to the local-git safety contract above, where an untested fallback could
be the branch that writes to the main clone instead of a worktree. The
decision is recorded in `this.i` as node `cn4pk7zq`.

### Unattended-mode changes get a live cron shakedown

The unit suite runs in-process with a rich environment and CANNOT catch the
failure modes that only appear when cron itself invokes the tool: a missing
MTA silently discarding output, `gh` credentials unreachable from cron's
scrubbed environment, a `PATH` lacking `~/.local/bin` or `git`/`gh`,
`--config-root` defaulting to the wrong place, or the cron daemon not running.
Any change touching the cron path — `bin/gitbulk-cron`, the exit-code/symlink
contract, the `ATTENTION` sentinel, config-root resolution, or how a
subcommand behaves run headless — and any move toward real cron deployment
MUST be verified with a **live one-shot cron tick** before it is trusted:
install a crontab line pinned to a specific minute a minute or two out
(minute+hour+day-of-month+month, so it fires once and does not recur), run a
read-only subcommand (`report`) first, watch it fire via the system journal,
then study the produced artifacts (cron log, run dir, exit code, `last-*.log`
symlink, sentinel) and remove the one-shot. Mutating subcommands graduate to
this only after the read-only tick is clean, and dry-run before `--apply`.
Proactively PROPOSE this shakedown whenever a change lands on the cron path —
don't wait to be asked. Rationale and the first run (2026-05-29) are recorded
in `this.i` node `shkd5crn` (and tension `opd3ny5k` #3).

### Sign off every commit

DCO is enforced in repos this tool operates on, and the same discipline
applies here. Use `git commit -s` on every commit, including amends.

---

## Language and runtime

- **Python 3.10 or later.** Enforce with a runtime check in `cli.py`.
- **POSIX-only runtime.** gitbulk uses `fcntl.flock` and POSIX symlink
  semantics; Windows is not supported (see `this.i` node `posqx2nm`).
  macOS works by virtue of being POSIX-compliant; Linux is the
  primary target.
- **All production code in `src/gitbulk/`.** Tests in `tests/`. Shell
  helpers (cron wrapper, etc.) in `bin/`. No mixing.
- **No JavaScript, no Bash logic of consequence.** `bin/gitbulk-cron`
  is the only shell script and stays trivial.

---

## File modification isolation (for parallel AI agents)

If multiple AI agents work on this repo at once (e.g., one on the config
loader and one on the invariant runner), each agent must be given a
disjoint set of files in its prompt. Two agents must never edit the
same file concurrently.

---

## Where things live

| Path | Purpose |
|------|---------|
| `src/gitbulk/` | All Python implementation |
| `tests/` | Pytest test files; mirrors src layout |
| `bin/gitbulk-cron` | Cron wrapper (shell) |
| `config/*.example` | Example user config |
| `prompts/` | Pluggable prompt files for `dispatch` and `summarize` |
| `~/.config/gitbulk/` | User's actual config (not in repo) |
| `~/.cache/gitbulk/` | Run state, dashboard, ATTENTION sentinel, locks |

---

## Summary workflow

1. Read this file and the test for the area you'll touch.
2. `pytest -q` → green baseline.
3. Write tests first if behavior is changing.
4. Implement.
5. `pytest -q` → green.
6. `git add <specific files>` (never `git add -A`).
7. `git commit -s` with a message focused on the why.

---

## Defect & task management (tick)

This repo tracks defects, tech debt, and ideas in a local
[`tick`](https://github.com/dhh1128/tick) ledger (an orphan `tick` branch; the
`tick` CLI is the interface), **not** GitHub Issues. Reads are plain files — do
**not** use GitHub Issues or any external tracker/API for this.

- **A tick mark is `~` + a digit-first 4-char id**, e.g. `~4mz3`. It pins a tick
  to a code location.
- **Before editing a file**, grep it for marks and read what they reference:
  `rg '~[2-7][a-z2-7]{3}' <file>` then `tick show <id>`. A mark means recorded
  context exists for that spot — read it first.
- **Search** existing ticks with `tick grep <text>`; **list** the open backlog
  with `tick ls` (filter with `--tag bug`, `--kind todo|debt|idea`).
- The id is the durable join key (tick files never cite line numbers): find a
  tick's code with `tick refs <id>`, find a tick from a mark with `tick show <id>`.

**Logging a bug.** When a maintainer says "log a bug about X" (or an agent
discovers a defect worth tracking), capture it immediately — do not wait for
further confirmation — and report the printed `~<id>`:

```
tick add "<concise summary of the defect>" --kind todo --tag bug
```

Then fill in the detail. Use `tick edit <id>` to open the tick and write a
structured body (Summary / Steps to reproduce / Expected / Actual / Environment
/ Notes — logs, stack traces, suspected cause), or append dated detail with
`tick note <id> "<text>"`. Omit a section only when it genuinely does not apply.
`bug` is the only triage tag — no severity/priority tags. If the defect has an
obvious code location, drop its mark there: `tick mark <id> <file:line>`.

**Fixing a bug.** When a maintainer says "let's fix bug X":

1. Resolve X to a tick: `tick grep "X"` (or `tick show <id>` if given an id).
   Confirm the match before proceeding.
2. Branch `fix/<id>-<short-slug>` off the default branch — the tick id is the
   durable join key, replacing the old issue number.
3. Fix it TDD-style per the rules above (failing test first).
4. Reference the mark `~<id>` in the commit message and/or PR body.
5. When the change merges, `tick off <id>` and **delete the `~<id>` mark(s)** it
   reports still in the code. A tick that turns out to be a real design decision
   should graduate into `this.i` / the design docs when closed.

**Finding bugs.** `tick ls --tag bug` lists the open defect backlog; `tick show
<id>` shows one; `tick refs <id>` finds every code site that references it.

## Testing Protocol

This repository has an established test suite. Follow strict TDD:
1. Write one or more failing tests that capture each requirement (including
   both happy paths and its edge cases/unhappy paths) before implementing.
2. Implement until all tests pass.
3. Never commit unless all tests pass. Coverage of any code you touch
   must not decrease.

## CI and Documentation

This repo HAS CI workflows in `.github/workflows/`:

- `ci.yml` — runs on every push to `main` and on every pull request. It
  runs the test suite across a Python matrix (3.10 and 3.12) behind a 100%
  branch-coverage gate, a separate real-binary e2e job (bubblewrap sandbox +
  isolated clone), a release-asset build/validation job (exercises the
  release path without publishing), a Trojan-Source/invisible-Unicode guard
  (`scripts/check_unicode.py`), and `actionlint`.
- `release.yml` — tag-triggered (`v*`); verifies the tag matches the package
  version, builds the single-file zipapp binary plus `update.json`, and
  publishes both as GitHub release assets.
- `deploy-docs.yml` — builds the Zensical docs site and deploys it to GitHub
  Pages (runs on `main` when docs change, or via manual dispatch).
- `copilot-review-gate.yml` — requests or removes Copilot as a PR reviewer
  based on the PR title/labels (e.g. the `[no-ccr]` / `no-ccr` convention).

When you change code, MAINTAIN these workflows: keep them green and update
them as needed (e.g. when adding a dependency, a Python version, or a new
build step). Do NOT propose creating new CI workflows that duplicate what
already exists here.

When writing or modifying GitHub Actions workflows, always use the latest
stable release of each action. Avoid versions pinned to Node.js 16 or
Node.js 20 (both deprecated by GitHub). In 2026, this meant to prefer Node.js
24-compatible versions, but the standard may evolve over time. Check the GitHub
Marketplace for each action's current release.

## Methodology

This repo should have a file called `this.i` at the root. It records the *why* behind every design
decision as a tree of `goal:`, `decision:`, `constraint:`, and `tension:` nodes. It is the
most important file in the repo for understanding why things are built the way they are. The
file is YAML and should be parsed as YAML; do not pattern-match indentation.

Adopt this stance toward it:

1. **The intent tree describes a destination, not just current state.** Nodes may describe
   completed stages or planned futures; read stage-status fields to distinguish them.
2. **Tension resolutions are binding.** Implement consistently with recorded resolutions.
   Do not re-open them or silently resolve them differently.
3. **`why` fields are primary evidence.** When making any decision touching a node, the `why`
   is the most important thing to read.
4. **`deviation:` nodes are the complete list of approved gaps.** Discovery is by node type
   (every `deviation:` node in the tree), not by a numbered list; any gap not represented by a
   `deviation:` node is a defect requiring approval before acceptance. Some files still use the
   legacy `cd-N` convention — migrate each such node to a `deviation:` node with a fresh opaque
   base32 id, populate `deviates-from:` / `scope:` / `why:` / `approved-by:`, and leave a YAML
   comment `# was: cd-Nnnn` on the node's name line recording the old id.
5. **Before making any decision that meets the trigger criteria, record it in `this.i` first.**
   The concrete trigger list is in `docs/methodology.md §3`.

For the full context of what `.i` files are, the intellectual lineage of this system, what makes
a `why` field adequate, and what triggers a required `this.i` update, read `docs/methodology.md`
(or, if none is present, copy it into this repo from `../origin-platform/docs/origin-platform` and then read it).
DO NOT modify code here without understanding the methodology. You should have a clear idea of
what a "speculative interview" is, how it's done, and where its output is recorded; what a "tension"
is in intent; how "marks" work; how we use Fowler's _Refactoring_ discipline to continually improve DRY, encapsulation, and names in code.

If you don't see a `this.i` in this repo's root, you must create one. To understand how, study one in
a sibling repo (`../origin-sip-policy-admin` has an excellent example). Notice how it relates to code
but explains things that are often missing from the code. Then use sources like the code, `README.md`,
and `docs/*.md` (possibly creating `docs/architecture.md` using the `generate-arch-doc.md` prompt if
needed) to form theories about design decisions in this codebase. Then interview the user to confirm
or disprove your theories, and write a starter `this.i` when you're done. (If the codebase is empty,
just ask the user about their intentions for it, and begin building from there.)
