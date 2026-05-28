# Architecture — gitbulk

> **Status: Phase 1A (foundations).** Most components below are designed and
> their intent recorded in [`this.i`](../this.i), but only the CLI shell exists
> in code. Stage status is called out per section.

This document is the human-readable map of how gitbulk's pieces fit together.
The authoritative source for *why* each piece is shaped the way it is is
[`this.i`](../this.i); node ids (e.g., `7mxr4pql`) appear inline below as
cross-references.

---

## 1. What this tool is

gitbulk is a personal nightly fleet-maintenance tool for a developer who
contributes to ~150 git repositories (see goal `q3kfzm7n`). It runs unattended
from cron and produces structured reports, automated PR progressions, and
local-clone cleanup, all while never modifying any working tree the user is
actively editing.

Two scopes of object are first-class:

- **Pull requests** — triage, merge, rebase-onto-default, close-stale,
  dispatch agents to fix.
- **Local repos themselves** (decision `xq4npk7r`) — orphaned worktrees,
  undeleted post-merge branches, stale local refs, and proactive discovery
  of repos that need work no PR yet exists for.

## 2. Where it fits

gitbulk sits between the user's cron table and the GitHub REST/GraphQL API
(via `gh`), with sibling tools as both inputs and outputs:

```
              ┌───────────┐
              │  crontab  │
              └─────┬─────┘
                    │
                    ▼
   ┌────────────────────────────────┐         ┌──────────────────────┐
   │           gitbulk               │ ◀────── │  ~/code/<repo>/      │
   │  (this tool — Phase 1A onward) │         │   (user's clones,    │
   └──────┬───────────────────┬──────┘         │    read-only here)   │
          │                   │                 └──────────────────────┘
          │ subprocess        │ subprocess
          ▼                   ▼
       ┌──────┐         ┌────────────┐         ┌──────────────────────┐
       │  gh  │         │ multiprompt│ ──────▶ │  per-repo artifacts  │
       │ CLI  │         │  (Phase 4) │         │  (findings, reports) │
       └──┬───┘         └────────────┘         └──────────────────────┘
          │
          ▼
      GitHub API
```

- **Input fleet:** the user's clones under `~/code/` and the configured repo
  list at `~/.config/gitbulk/repos.txt`.
- **Output state:** `~/.cache/gitbulk/` (run artifacts, dashboard, ATTENTION
  sentinel, locks, worktrees).
- **Sibling tools:**
  - `gh` (constraint `hp4nck2v`) — exclusive channel for GitHub network.
  - `multiprompt.py` (tensions `mp7kn4qz`, `fw5kq6np`, `ck7n4pqr`) — used
    for both **dispatch** (executing claude in worktrees) and **scan**
    (collecting per-repo findings).
  - `agentprep` (per AGENTS.md managed block) — runs per-dispatch in any
    worktree where claude will be invoked.

## 3. Stack / technology choices

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Constraint `6jz4n2pq`; modern type-hint syntax matters for clarity |
| Distribution | `pip install -e .` (no wheel/PyPI yet) | Personal tool; v1 doesn't need a release pipeline |
| GitHub network | `gh` CLI subprocess | Constraint `hp4nck2v` — reuse user's auth, free GraphQL, no second credential surface |
| Git network | SSH | Constraint `ks52rg4w` — reuse user's ssh-agent |
| Config | YAML + plain-text | Decision `ws2pn4kr` — `repos.txt` plain, `gitbulk.yaml` for policy |
| State | Files under `~/.cache/gitbulk/` | Decision `tp4kq2nr` — file-based, no DB, no external services |
| Locking | `fcntl.flock` advisory | Decision `lj5pqn4kr` — global + per-repo, lets parallel reads coexist |
| Tests | `pytest` + `pytest-cov` | TDD mandatory per AGENTS.md; 100% branch coverage gate per `cn4pk7zq` |
| Parallel agent runs | `multiprompt.py` (TBD as kernel) | Tension `mp7kn4qz` — Phase 4 |

## 4. Key architectural decisions

This section is a curated index into [`this.i`](../this.i). Read the nodes
themselves for the rebuttal-surface rationale.

- **Local-git safety contract** `7mxr4pql` — the most important rule.
- **Invariants framework** `c4jzm5pn` — operations are chains of named
  invariants; suppressions are explicit and audited.
- **Cmdline wins over config for overrides** `r4nzp7kq` — asymmetric audit
  (relaxing trips exit 4 + WARNING; tightening logs INFO).
- **Mutating subcommands default to dry-run** `2vqp4nk6` — misconfigured cron
  must not silently merge.
- **Ready to merge stricter than GitHub** `zk3r4nqp` — adds unresolved-thread
  check (including bot threads) on top of `mergeable_state == clean`.
- **3 business days from continuously ready** `bg4pqn7m` — auto-merge age
  threshold; M–F local TZ, no holiday awareness.
- **Worktree root under XDG cache** `mw6kp2nq` —
  `~/.cache/gitbulk/worktrees/<runid>/<owner>__<repo>/`.
- **Serial + GraphQL coalescing, no rate limiter** `gd4kp7nz` — ~300 calls
  per run under a 5000/hr budget; serial sidesteps secondary limits.
- **Local repos are first-class citizens** `xq4npk7r` — fleet = repos × PRs.

Tensions (deferred decisions, do not resolve silently):

- `kw2pn7qz` Summarize prompt design — defer to Phase 3.
- `mp7kn4qz` Dispatch execution kernel (multiprompt) — defer to Phase 4.
- `fw5kq6np` Multiprompt packaging future — multiprompt's own question.
- `ck7n4pqr` Scan and findings artifact convention — defer to Phase 4.
- `jw3kpn4q` Repo cleanup subcommand scope — defer to Phase 5/6.
- `rj7p4kqn` Default branch rename handling — open.

## 5. Internal structure

```
src/gitbulk/
  cli.py                  — argparse shell, subcommand dispatch, exit codes
                            (Phase 0; subcommands exit 99 until later phases)
  __main__.py             — `python -m gitbulk` entry point
                            (Phase 0)
  paths.py                — XDG-aware path helpers (~/.config, ~/.cache)
                            (Phase 1C)
  config/
    repos.py              — repos.txt parser           (Phase 1C)
    policy.py             — gitbulk.yaml parser + schema (Phase 1C)
  util/
    businessdays.py       — M–F arithmetic, local TZ  (Phase 1C)
  invariants/
    base.py               — Invariant ABC, Pass/Skip/Fail result types
    registry.py           — central registration       (Phase 1C)
    runner.py             — chain runner with override semantics (Phase 1C)
    catalog/              — concrete invariants        (Phase 2+)
  runstate.py             — per-run dir, manifest, log appenders (Phase 1C)
  locks.py                — global + per-repo flock    (Phase 1C)
  sentinel.py             — ATTENTION file management  (Phase 1C)
  dashboard.py            — dashboard.md rewriter      (Phase 1C)

tests/                    — pytest tests, mirror src/ layout
```

### Run state layout (Phase 1C)

```
~/.cache/gitbulk/
  run.lock                            # global advisory lock
  locks/<owner>__<repo>.lock          # per-repo locks
  worktrees/<runid>/<owner>__<repo>/  # disposable worktrees (Phase 4+)
  runs/
    <timestamp>-<subcommand>/
      state.yaml                      # full per-repo decisions
      summary.md                      # human-readable
      errors.log
      invariants.log                  # every skip/fail with reason
      manifest.yaml                   # argv, config snapshot, version
    latest-<subcommand> ───────────▶  symlink to newest run
  dashboard.md                        # single-screen latest-state view
  ATTENTION                           # sentinel, present when exit 2 or 3
  findings/<owner>__<repo>/           # scan artifacts (Phase 4)
```

## 6. Lifecycle of a run

Every subcommand follows the same outer skeleton:

1. **Parse args**, validate Python version, build the parser.
2. **Acquire global lock** (shared for read-only subcommands, exclusive for
   mutating ones — see `lj5pqn4kr`).
3. **Run universal preflight invariants**: `gh.authenticated`,
   `config.parseable`, `org.members.fresh`.
4. **For each configured repo** (serially per `gd4kp7nz`):
   - Acquire the per-repo lock (only for mutating subcommands).
   - Run per-repo preflight invariants: `local.exists` (only for subcommands
     that need a clone per `5xqp2nkr`), `local.remote_matches`,
     `local.default_branch_in_sync`, `github.reachable`.
   - For each open PR (where applicable):
     - Run per-PR baseline + subcommand-specific invariants.
     - Record Pass/Skip/Fail with reason into the run state.
     - If Pass and the subcommand is mutating, perform the operation behind
       the `--dry-run` / `--apply` gate.
5. **Write `summary.md`, `state.yaml`, `errors.log`, `invariants.log`,
   `manifest.yaml`** to the run directory.
6. **Rewrite `dashboard.md`** to reflect the latest state across all
   subcommands.
7. **Create or remove the `ATTENTION` sentinel** based on the exit code.
8. **Release locks**, exit with the appropriate code.

Exit codes (decision `tp4kq2nr` — the same node defines the four-layer
file-based notification model AND the exit-code semantics that drive
the ATTENTION sentinel; the two concerns are inseparable in that
design):

| Code | Meaning |
|---|---|
| 0 | nothing to flag |
| 1 | structural failure (bad config, gh not authed, network) |
| 2 | ran successfully but PRs need user attention |
| 3 | ran successfully but at least one repo skipped by an invariant |
| 4 | ran with `--skip-check` overrides applied (audit signal) |
| 99 | subcommand not implemented (Phase 0 scaffold sentinel) |

## 7. Test strategy

- TDD is mandatory (AGENTS.md). Tests are written before or alongside
  implementation, never after.
- **No network in tests** (AGENTS.md). `gh` and git network operations are
  dependency-injected so tests run offline.
- **100% branch coverage on `src/gitbulk/`**, enforced by CI (decision
  `cn4pk7zq`). Any gap requires an approved `deviation:` node in
  [`this.i`](../this.i) per [`methodology.md`](methodology.md) §6.
- **Subprocess and filesystem dependencies are injected** so per-test
  fixtures can simulate locks held, repos missing, gh responses, etc.

## 8. Safety & trust model

gitbulk is single-user, runs locally, and has no network-exposed surface —
the conventional "threat model" frame doesn't quite fit. What matters is
the **trust the user places in gitbulk to operate on their clones**. Three
boundaries:

1. **Working-tree integrity** (constraint `7mxr4pql`). Every code path that
   writes to disk must verify it is inside a configured worktree root, not
   the main clone. Worktree path verification is a hard rule in AGENTS.md.
2. **Default-branch authority** (AGENTS.md). Every PR-touching operation
   verifies that the PR's `baseRefName` equals the repo's current GitHub
   default branch; otherwise the PR is skipped with a prominent reason.
   `--allow-non-default-base` is the explicit escape hatch.
3. **Mutation gates** (`2vqp4nk6`). Every mutating subcommand defaults to
   `--dry-run`; `--apply` is required to act. Every invariant suppression
   logs a WARNING into `invariants.log` and trips exit code 4.

No secrets are stored in this repo or in `~/.cache/gitbulk/`. Authentication
is delegated entirely to `gh` (GitHub) and the user's `ssh-agent` (git).

## 9. Configuration reference

Two files, defaulting to `~/.config/gitbulk/`:

- **`repos.txt`** — one `owner/repo` slug per line. `#` comments and blank
  lines ignored.
- **`gitbulk.yaml`** — policy and classification. Shipping example at
  [`config/gitbulk.yaml.example`](../config/gitbulk.yaml.example). Key keys
  (Phase 1C parser):
  - `defaults.merge_policy: strict | ci-only | never`
  - `defaults.min_business_days: int` (default `3`, per `bg4pqn7m`)
  - `defaults.unresolved_burden: me | other | either` (default `me`, per `hj3nq5kp`)
  - `defaults.bot_threads_block: bool` (default `true`, per `zk3r4nqp`)
  - `defaults.stale_age_days`, `defaults.stale_cooloff_days`
  - `humans.org`, `humans.exceptions`, `humans.always_human`
  - `bots:` list
  - `repos.<slug>:` per-repo overrides
  - `worktree_root: str` (default `~/.cache/gitbulk/worktrees`, per `mw6kp2nq`)

## 10. Known gaps & future work

- **No production code beyond the CLI shell yet.** Phase 1C will land
  config loader, invariants framework, run state, locks, sentinel,
  dashboard. Phase 2 lands the `gh` wrappers and `report`.
- **Multiprompt integration is unresolved** (tensions `mp7kn4qz`,
  `fw5kq6np`, `ck7n4pqr`). Phase 4 entry will pick between subprocess,
  shared kernel, or re-implementation.
- **No remote/CI history yet.** The repo is local-only until
  `gh repo create dhh1128/gitbulk --public` runs (decision `6xp4kq2n`).
  CI badge in README is a placeholder until then.
- **No license file.** TODO before first remote push.
- **`gitbulk gc`** (cleanup subcommand, tension `jw3kpn4q`) is not yet
  designed in detail. Phase 5/6.

## 11. Contributor reading order

For a new contributor (human or AI) coming cold:

1. [`AGENTS.md`](../AGENTS.md) — the behavioral contract. **Read first.**
2. [`this.i`](../this.i) — the design-decision tree. Skim the goal node
   `q3kfzm7n`, then the constraints, then the most relevant decisions.
3. This file (`docs/architecture.md`) — the map you are currently in.
4. [`docs/design-notes.md`](design-notes.md) — narrative explainer with
   pointers into `this.i`.
5. [`docs/methodology.md`](methodology.md) — the development discipline
   this repo follows. Required reading before any change that meets the
   §3 trigger list.
6. [`src/gitbulk/cli.py`](../src/gitbulk/cli.py) — current code surface.
7. [`tests/test_cli.py`](../tests/test_cli.py) — current test surface.
