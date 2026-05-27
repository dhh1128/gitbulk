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
`config --get`, `remote get-url`, `branch --show-current`, `log`) are fine.

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

### Coverage standard

100% branch coverage on `src/gitbulk/`, enforced in CI. A gap requires
an approved `deviation:` node in `this.i` (see `docs/methodology.md` §6);
a gap without one is a defect, not a judgment call. The framing — "a bug
in gitbulk can damage real work in real repos" — applies most acutely
to the local-git safety contract above, where an untested fallback could
be the branch that writes to the main clone instead of a worktree. The
decision is recorded in `this.i` as node `cn4pk7zq`.

### Sign off every commit

DCO is enforced in repos this tool operates on, and the same discipline
applies here. Use `git commit -s` on every commit, including amends.

---

## Language and runtime

- **Python 3.10 or later.** Enforce with a runtime check in `cli.py`.
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
