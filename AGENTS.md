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

Two `gitbulk` processes must be safe to run at the same time. Read-only
subcommands take a shared global advisory lock; mutating subcommands take
an exclusive global lock plus a per-repo lock. Any new subcommand must
declare its lock requirements explicitly.

### Default branch detection

Every operation that touches a PR must verify that the PR's `baseRefName`
equals the repo's current default branch on GitHub. If not, the PR is
skipped with a prominent reason in the report. Override via
`--allow-non-default-base` only.

### Mutating subcommands default to dry-run

Every mutating subcommand (`merge`, `close-stale`, `rebase-onto-default`,
`dispatch`) defaults to `--dry-run` and requires `--apply` to actually act.
A misconfigured cron entry must not silently merge things.

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

## Defect management (GitHub Issues)

This repo tracks defects as **GitHub Issues** on `dhh1128/gitbulk`, managed
with the `gh` CLI (no issue tracker MCP server is used). Issues are enabled
and the standard `bug` label exists.

**Logging a bug.** When a maintainer says "log a bug about X" (or an agent
discovers a defect worth tracking), create the issue immediately — do not
wait for further confirmation — and report the issue number and URL:

```
gh issue create --repo dhh1128/gitbulk --label bug \
  --title "<concise summary of the defect>" \
  --body "$(cat <<'EOF'
## Summary
<one-paragraph description>

## Steps to reproduce
1. ...

## Expected
<what should happen>

## Actual
<what happens instead>

## Environment
<version / OS / config relevant to the bug, if any>

## Notes
<logs, stack traces, suspected cause, related issues>
EOF
)"
```

Fill in every section you can; omit a section's body only when it genuinely
does not apply. The only triage label is `bug` — no severity/priority labels.
Use milestones or comments if prioritization is needed later.

**Fixing a bug.** When a maintainer says "let's fix bug X":

1. Resolve X to an issue: `gh issue list --repo dhh1128/gitbulk --label bug
   --state open --search "X"`, or `gh issue view <n>` if given a number.
   Confirm the match before proceeding.
2. Branch `fix/<issue#>-<short-slug>` off the default branch.
3. Fix it TDD-style per the rules above (failing test first).
4. Reference `Fixes #<n>` in the commit message and/or PR body so the issue
   auto-closes when the change merges to the default branch.

**Finding bugs.** `gh issue list --repo dhh1128/gitbulk --label bug
--state open` lists the open defect backlog; `gh issue view <n>` shows one.

<!-- BEGIN AGENTPREP MANAGED BLOCK -->
## AgentPrep AI Operating Rules

Use of AI in conjunction with this repository is governed by
[AgentPrep](https://github.com/provenant-dev/agentprep).

As an AI, you will need to know how to identify yourself to the
agentprep tool. Canonical ai_agent_name values are:

    `claude`, `codex`, `copilot`, `cursor`, `gemini`, or `openclaw`

If you are one of these tools, great; use the appropriate ai_agent_name
with agentprep. If you are an AI but your name is not in this
list, stop immediately and tell the user to manually edit this AGENTS.md
file so you recognize your name in the list above.

Once you can name yourself canonically, **before starting any task that
will involve a git commit**, run:

```bash
agentprep verify --agent <ai_agent_name>   # or: export AGENTPREP_AGENT=<ai_agent_name>
```

If verification fails with `verify failed: missing .ai-safety-check.*`, **stop
and tell the user before writing any files**. A git pre-commit hook (committed to
`.githooks/pre-commit` and installed automatically to `.git/hooks/pre-commit` by
`agentprep init` and `agentprep certify`) will block every commit you attempt until
the user runs `agentprep certify --agent <name>` to attest a correct config. Do not
attempt the task until the user confirms they've certified. Then cache the lease timestamp produced by `agentprep verify` once
verification succeeds; you do not need to re-verify within the same session.

The following operations are reserved for humans. The `.agent-bin` shims
installed in this repository will block them if an agent attempts them:

- `git push` to protected branches (defaults: `dev`, `main`, `master` — `dev` is included because it is a shared integration branch, not a personal feature branch) and destructive push modes (`--delete`, `--all`, `--mirror`)
- `gh pr merge` — merging a pull request
- `gh repo delete` — deleting the repository

Creating, viewing, and updating pull requests is permitted (`gh pr create`,
`gh pr edit`), as is pushing feature branches for PR workflows.

Place `.agent-bin` at the front of PATH in agent shells so the shims are active:

```bash
export PATH="$PWD/.agent-bin:$PATH"
```
## Testing Protocol

This repository has an established test suite. Follow strict TDD:
1. Write one or more failing tests that capture each requirement (including
   both happy paths and its edge cases/unhappy paths) before implementing.
2. Implement until all tests pass.
3. Never commit unless all tests pass. Coverage of any code you touch
   must not decrease.

## CI and Documentation

This repo has no CI workflows. Until it does, any time you make code
changes to the user, propose an appropriate set of GitHub actions (e.g.,
`.github/workflows/ci.yml`) that builds and runs tests on every push and
pull request. Propose to remove this instruction from AGENTS.md on the
same commit.

When writing or modifying GitHub Actions workflows, always use the latest
stable release of each action. Avoid versions pinned to Node.js 16 or
Node.js 20 (both deprecated by GitHub). In 2026, this meant to prefer Node.js
24-compatible versions, but the standard may evolve over time. Check the GitHub
Marketplace for each action's current release.

## Origin Platform Context

This codebase is part of the Origin platform ecosystem. Other origin-related
repositories likely exist as siblings in the parent directory (e.g.,
`../origin-auth-lib`, `../origin-common-lib`, `../origin-deployment`); they
definitely exist as sibling repos under https://github.com/provenant-dev/.
Each sibling repo typically has `README.md` and a `docs/` folder containing
design docs with important metadata. This knowledge is available to you and
you should consult it (making sure the local code is current and on its
default branch) if you need broader context than the current repo.

The `../origin-platform/` sibling is the platform-wide knowledge base. Its
`docs/origin-platform/` directory contains platform-wide architecture guides, API
conventions, security requirements, deployment specifications, and cross-cutting
platform decisions. Before making changes that could touch platform conventions —
authentication, URL design, error formats, data models, Kafka topics, deployment
patterns, or testing strategy — consult the relevant doc in
`../origin-platform/docs/origin-platform/`. Start with
`general-origin-characteristics.md`; follow links there to more specific guides.

When a proposed change has platform-wide implications, check whether relevant
sibling repos' `docs/` folders offer useful constraints before proceeding.

The `../origin-platform/prompts/` directory contains reusable AI reviewer prompts
designed for Origin platform codebases. They create named and dated reports in a
/reviews folder in this repo and constitute prioritized next steps or action items
for improving the code. Consider recommending one or more of these to the user after
significant changes, before a release, or during onboarding:

- **`platform-architect.md`** — Reviews alignment with platform-wide API, auth,
  data, and communication conventions; flags drift that creates integration problems.
- **`security-hawk.md`** — Adversarial security review: auth bypasses, authorization
  holes, injection paths, replay attacks, and secret exposure.
- **`compliance-auditor.md`** — SOC-2 / regulatory readiness: audit trails, access
  controls, evidence of consistently operating controls.
- **`maintainability-expert.md`** — Identifies intent boundaries, missing rationale,
  and patterns likely to be incorrectly "fixed" by future developers or AI agents.
- **`testability-hawk.md`** — Finds structural testability gaps across all test layers;
  surfaces classes of missing tests rather than individual cases.
- **`devops-engineer.md`** — Deployment, CI/CD, Docker/Helm correctness, Kubernetes
  health probes, and local development ergonomics.
- **`ux-guru.md`** — UX and frontend architecture review (skip for pure backend repos).
- **`generate-arch-doc.md`** — Generates or refreshes `docs/architecture.md`.

To use a prompt, open the file in your AI tool's context and run it against the
current workspace. Periodic runs of `platform-architect.md` and `security-hawk.md`
are especially recommended as the platform evolves. Once the action items in a report
have all been triaged, they should be deleted (but remain in git history to show
what was done).

If this repo does not have a docs/architecture.md file, always propose creating one
using `generate-arch-doc.md`.

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
<!-- END AGENTPREP MANAGED BLOCK -->
