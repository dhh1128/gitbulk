# Architecture — gitbulk

> **Status: all planned subcommands are implemented (v0.7.1).** Read-only
> triage (`report`), agent-assisted summarization (`summarize`), and headless
> agent dispatch into per-PR worktrees (`dispatch`) run end-to-end on real
> fleets, and the Phase-5+ mutating subcommands — `merge`, `rebase-pr`,
> `close-stale`, `prune-branches`, `prune-worktrees` — are all live (each
> defaults to dry-run; `--apply` is required to act). `gitbulk show` reads
> prior run artifacts. The agent boundary is pluggable (Claude Code by
> default, plus Gemini / Copilot / Cursor / a custom CLI). Locking is now
> resource-scoped (node `rsclk7nq`): the old single global lock has been
> retired.

This document is the human-readable map of how gitbulk's pieces fit together.
The authoritative source for *why* each piece is shaped the way it is is
[`this.i`](../this.i); node ids (e.g., `7mxr4pql`) appear inline below as
cross-references.

---

## 1. What this tool is

gitbulk is a personal nightly fleet-maintenance tool for a developer who
contributes to ~150 git repositories (goal `q3kfzm7n`). It runs unattended
from cron and produces structured reports, automated PR progressions, and
local-clone cleanup, all while never modifying any working tree the user is
actively editing.

Two scopes of object are first-class:

- **Pull requests** — triage (`report`), prioritize (`summarize`), fix via
  agent dispatch (`dispatch`), rebase onto their default branch (`rebase-pr`),
  auto-merge under policy (`merge`), and close when stale (`close-stale`).
- **Local repos themselves** (decision `xq4npk7r`) — orphaned worktrees
  (`prune-worktrees`), undeleted post-merge branches (`prune-branches`), and
  proactive discovery of repos that need work no PR yet exists for (deferred,
  `jw3kpn4q`).

## 2. Where it fits

gitbulk sits between the user's cron table and the GitHub REST/GraphQL API
(via `gh`), with sibling tools and local clones as both inputs and outputs:

```
              ┌───────────┐
              │  crontab  │
              └─────┬─────┘
                    │  (via bin/gitbulk-cron wrapper)
                    ▼
   ┌────────────────────────────────┐         ┌──────────────────────┐
   │           gitbulk               │ ◀────── │  ~/code/<repo>/      │
   │   (read + mutating subcommands │         │   (user's clones,    │
   │    all implemented)            │         │    read-only here)   │
   └──────┬───────────────────┬──────┘         └──────────────────────┘
          │                   │
          │ subprocess        │ subprocess (AgentBackend)
          ▼                   ▼
       ┌──────┐         ┌────────────┐         ┌──────────────────────┐
       │  gh  │         │ agent CLI  │ ──────▶ │  per-repo artifacts  │
       │ CLI  │         │ (claude…)  │         │  (runs/, dashboard)  │
       └──┬───┘         └────────────┘         └──────────────────────┘
          │
          ▼
      GitHub API
```

- **Input fleet:** the user's clones under `~/code/` and the configured repo
  list at `~/.config/gitbulk/repos.txt`.
- **Output state:** `~/.cache/gitbulk/` — run artifacts, dashboard, ATTENTION
  sentinel, locks, worktrees, org-members cache.
- **External boundaries:**
  - `gh` (constraint `hp4nck2v`) — exclusive channel for GitHub network,
    fronted by the `GHClient` Protocol (node `ghclmp7n`).
  - **Coding-agent CLI** (`smprmpt4n`, `execk7nm`, `agbknd7q`) — text-producing
    boundary used by `summarize` (one-shot) and `dispatch` (parallel kernel),
    fronted by the `AgentBackend` Protocol (generalized from `ClaudeClient`). A
    single `plan()` yields one `AgentInvocation` (argv + stdin + env + timeout)
    that both call paths agree on. `gitbulk.agent` adds config-driven backends —
    built-in presets (claude/gemini/copilot/cursor) plus a custom argv-template
    — selected by `default_agent` / per-repo `agent:` / `--agent`
    (`agprof4k`). For `dispatch`, **gitbulk owns every networked git op**: it
    pre-fetches the base and force-pushes itself after verifying the agent's
    result (`agpriv8n`); the agent can therefore run env-scoped (`agenv6q`) and
    inside an unprivileged bubblewrap sandbox (`agsbx3k`). See
    [`pluggable-agents.md`](pluggable-agents.md).
  - `git worktree` (subprocess) — used only by `dispatch` to create
    disposable per-PR checkouts. The main clone is never `git checkout`-ed.

## 3. Stack / technology choices

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Constraint `6jz4n2pq`; modern type-hint syntax matters for clarity. Runtime check in `cli.py`. |
| Platform | POSIX (Linux primary, macOS works) | Decision `posqx2nm` — `fcntl.flock` + POSIX symlink semantics are load-bearing. Windows not supported. |
| Distribution | self-contained zipapp via GitHub Releases (`gitbulk install` / `update`); `pip install -e .` for development | Single-file binary needs only Python 3.10+; built by `scripts/build_release_assets.py`. Still no PyPI/wheel. |
| GitHub network | `gh` CLI subprocess + `GHClient` Protocol | Constraint `hp4nck2v` — reuse user's auth, free GraphQL, no second credential surface. Tests inject `FakeGHClient`. |
| Agent calls (Claude et al.) | agent CLI subprocess via the pluggable `AgentBackend`; the single production backend is `agent.CommandAgentBackend` (claude included, SEC-F1) | Symmetric to gh shape; no retries (a bad prompt is a thinking problem, not transient). Backend selected by `default_agent` / per-repo `agent:` / `--agent` and run env-scoped (`agenv6q`). |
| Git network | SSH | Constraint `ks52rg4w` — reuse user's ssh-agent. |
| Config | YAML + plain-text | Decision `ws2pn4kr` — `repos.txt` plain, `gitbulk.yaml` for policy. |
| State | Files under `~/.cache/gitbulk/` | Decision `tp4kq2nr` — file-based, no DB, no external services. Schema-versioned (node `schv4nrm`). |
| Locking | `fcntl.flock` advisory | Resource-scoped (node `rsclk7nq`, superseding the global+per-repo model of `lj5pqn4kr`): one keyed lock per shared resource, held only around the section that touches it; reads take shared, mutations exclusive. Bounded timeouts per `tmlk5pq3`. |
| Tests | `pytest` + `pytest-cov` | TDD mandatory per AGENTS.md; 100% branch coverage gate per `cn4pk7zq`. |
| Parallel agent runs | In-tree kernel (`exec.py`) | Resolved tension `mp7kn4qz` via decision `execk7nm` — vendored rather than subprocessing multiprompt. |

## 4. Key architectural decisions

A curated index into [`this.i`](../this.i). Read the nodes themselves for
the rebuttal-surface rationale.

- **Local-git safety contract** `7mxr4pql` — the most important rule. Never
  touch the working tree, index, or current branch of a user clone.
- **Invariants framework** `c4jzm5pn` — operations are chains of named
  invariants; suppressions are explicit and audited. Three kinds: UNIVERSAL,
  PER_REPO, PER_PR (`ph2inv4n`).
- **Cmdline wins over config for overrides** `r4nzp7kq` — asymmetric audit
  (relaxing trips exit 4 + WARNING; tightening logs INFO).
- **Mutating subcommands default to dry-run** `2vqp4nk6` — misconfigured
  cron must not silently merge.
- **Ready to merge stricter than GitHub** `zk3r4nqp` — adds unresolved-thread
  check (including bot threads) on top of `mergeable_state == clean`.
- **Worktree root under XDG cache** `mw6kp2nq` —
  `~/.cache/gitbulk/worktrees/<runid>/<owner>__<repo>__pr<N>/`. The path-
  verification step in `worktree.py` is the load-bearing defense from
  AGENTS.md "Worktree path verification."
- **gh Client Protocol** `ghclmp7n` — Protocol + Production + Fake; tests
  inject the Fake, no network in test runs.
- **Subprocess.Popen directly in exec kernel** `execk7nm` — the parallel
  dispatch path needs live process handles (timeout escalation, CTRL+C
  drain), which the blocking `run_prompt` Protocol does not expose; the
  kernel takes a resolved `AgentInvocation` (argv/env/timeout) and does
  its own Popen.
- **Per-call timeout kwarg, hardcoded retry** `ghclmp7n.d` / `.e` — each
  `GHClient` method takes its own `timeout`; retry policy lives in
  `ProductionGHClient` (not configurable), so callers can't accidentally
  disable it.
- **Notification + exit-code model** `tp4kq2nr` — single decision covers
  both the four-layer file-based notification (runs/, dashboard.md,
  ATTENTION sentinel, exit codes) and the exit-code semantics that drive
  the sentinel; the two concerns are inseparable in that design.
- **Cache artifact schema versioning** `schv4nrm` — every YAML carries
  `schema_version: <int>`; every JSONL event carries `"v": <int>`.
  Initial value 1 everywhere; future bumps document the breaking change
  as their own decision node.

Tensions (deferred decisions, do not resolve silently):

- `kw2pn7qz` Summarize prompt design — **resolved** in Phase 3 via
  `smprmpt4n` (default prompt ships in-tree; `--prompt PATH` overrides).
- `mp7kn4qz` Dispatch execution kernel — **resolved** in Phase 4 via
  `execk7nm` (in-tree reimplementation, not subprocess multiprompt).
- `fw5kq6np` Multiprompt packaging future — **resolved** via `mprmpkg4`
  (multiprompt stays in origin-platform; gitbulk does not depend on it).
- `ck7n4pqr` Scan / findings artifact convention — still open; deferred
  to whenever a `scan` subcommand is actually built.
- `jw3kpn4q` Repo cleanup subcommand scope — **resolved**: shipped as
  `prune-branches` (delete remote branches whose only PRs are merged/closed)
  and `prune-worktrees` (remove orphaned local worktrees). Proactive
  no-PR-yet discovery remains deferred.
- `rj7p4kqn` Default-branch rename handling — still open.

## 5. Internal structure

```
src/gitbulk/
  __main__.py             — `python -m gitbulk` entry point
  cli.py                  — argparse shell, subcommand dispatch, exit codes,
                            ATTENTION fallback wiring, logging, umask
  subcommands.py          — typed registry of every subcommand (node
                            smodlpr3); single source of truth for invariant
                            chains (scinv4qm), lock mode, and whether the
                            subcommand needs a clone (5xqp2nkr)
  paths.py                — XDG-aware path helpers; slug-normalization
                            (security-hawk F1 hardening); compact UTC runids
  config/
    repos.py              — repos.txt parser
    policy.py             — gitbulk.yaml parser + dataclass schema
  util/
    businessdays.py       — M–F arithmetic in local TZ (per bg4pqn7m)
    lockstatus.py         — TTY lock-status reporter: live one-line notice
                            while a run is BLOCKED waiting on another's lock
    progress.py           — TTY progress bar for long-running --apply runs
  invariants/
    base.py               — Invariant ABC, Pass/Skip/Fail result types,
                            InvariantKind enum
    registry.py           — central registration table
    runner.py             — chain runner; first Fail aborts, Skips collected
    catalog.py            — concrete invariants: gh.authenticated,
                            config.parseable, org.members.fresh,
                            github.reachable, github.not_archived,
                            pr.base_is_default, pr.author_known,
                            local.exists, local.remote_matches,
                            local.default_branch_in_sync
  classifier.py           — humans-vs-bots resolution (hbcls4pq)
  org_members_cache.py    — org members cache reader/writer (CachedMembers,
                            save/load, refresh_cache, freshness check)
  gh.py                   — GHClient Protocol + ProductionGHClient +
                            FakeGHClient; includes the mutating methods the
                            mutating subcommands need (merge_pr, close_pr,
                            delete_branch_ref, closed_prs_for_head, …)
  claude.py               — ClaudeClient/AgentBackend Protocols + AgentInvocation
                            + FakeClaudeClient (no retry; bad prompts are
                            thinking problems). The production backend is
                            agent.CommandAgentBackend (SEC-F1 removed the
                            native ProductionClaudeClient).
  pr_info.py              — frozen PRInfo dataclass (node prdtm4kn)
  ready.py                — compute a PR's continuous merge-ready window
                            (ready_since); consumed by the PER_PR age-threshold
                            invariant that merge gates on
  filters.py              — fleet-subset selection around the invariant chain
                            (node flt7arg2): the --repo / --filter / --author
                            flags shared by the fleet subcommands
  exec.py                 — in-tree parallel agent executor (execk7nm);
                            ExecTarget / ExecResult / execute_targets;
                            bounded pool, timeout escalation, CTRL+C drain
  worktree.py             — `git worktree add` + path verification +
                            disposal; CONFLICT.md preservation (vp7n2krq)
  isolated_clone.py       — self-contained agent workspaces for sandboxed
                            dispatch (agecln4k); SEC-F1: a linked worktree
                            isn't usable inside the bubblewrap sandbox
  sandbox.py              — bubblewrap OS sandbox for dispatched agents
                            (agsbx3k); defense-in-depth over env scoping
  rebase.py               — git rebase + force-push for `rebase-pr`, inside a
                            disposable worktree only
  default_branch_cache.py — cached repo default branches (resource #3)
  watchdog_ack.py         — ack cache for the post-merge CD watchdog (#9)
  runstate.py             — per-run dir orchestration: manifest.yaml,
                            state.yaml, summary.md, invariants.log,
                            errors.log; atomic writes; latest-* symlink;
                            calls gc.prune_runs on complete
  agent.py                — AgentProfile + CommandAgentBackend (the single
                            production agent backend, claude included per
                            SEC-F1); built-in presets (claude/gemini/copilot/
                            cursor) + custom argv-template; backend_for()
                            resolves default_agent / per-repo agent: / --agent
  locks.py                — resource-scoped flock (node rsclk7nq): per-resource
                            context managers (run_state_lock, repo_lock,
                            org_lock, default_branches_lock, sentinel_lock,
                            dashboard_lock, watchdog_ack_lock) with JSON
                            holder metadata + pid-liveness probe in
                            LockTimeoutError; the legacy global lock is retired
  sentinel.py             — ATTENTION file management (one-line JSON per
                            schv4nrm; supersedes the old whitespace format)
  dashboard.py            — dashboard.md rewriter (excerpt-per-subcommand)
  gc.py                   — retention pruning for runs/<runid>-<sub>/
                            beyond policy.defaults.retain_runs
  commands/
    report.py             — read-only PR triage
    summarize.py          — agent-driven prioritization of a prior report
    dispatch.py           — headless agent dispatch into per-PR worktrees
    merge.py              — auto-merge PRs that satisfy the per-repo policy
    rebase_pr.py          — rebase behind/conflicting PRs onto their base
    close_stale.py        — close PRs inactive past the threshold (warn→close)
    prune_branches.py     — delete remote branches whose only PRs are
                            merged/closed (parallel scan)
    prune_worktrees.py    — remove local worktrees whose branch's only PRs
                            are merged/closed
    show.py               — read prior run artifacts (no new run)
  bundle.py / install.py / update.py
                          — zipapp build (zpapb4n7) + self-install (bootp4mq)
                            + self-update (updnc5kr); back the hidden
                            `bundle` and the `install` / `update` subcommands

tests/                    — pytest tests; mirrors src/ layout. 100% branch
                            coverage enforced.
bin/gitbulk-cron          — cron wrapper (the only shell script in the repo)
config/*.example          — example user config
prompts/                  — pluggable prompts for summarize & dispatch
```

### Run state layout

```
~/.cache/gitbulk/
  locks/                              # resource-scoped flocks (rsclk7nq),
                                      #   each with JSON holder metadata:
                                      #   runstate-<sub>.lock (run-state #1)
                                      #   <owner>__<repo>.lock (per-repo #6-8)
                                      #   org-<org>.lock (org cache #2)
                                      #   default-branches.lock (#3)
                                      #   attention.lock (sentinel #4)
                                      #   dashboard.lock (#5)
                                      #   watchdog-acked.lock (#9)
  worktrees/<runid>/<owner>__<repo>__pr<N>/  # disposable worktrees
  runs/
    <UTC-timestamp>-<subcommand>/
      manifest.yaml                   # argv, config snapshot, version, exit_code
      state.yaml                      # full per-repo decisions (PR records)
      summary.md                      # human-readable digest
      invariants.log                  # JSONL: every pass/skip/fail with reason
      errors.log                      # JSONL: warnings + errors with context
    latest-<subcommand> ───────────▶  symlink to newest completed run
  dashboard.md                        # single-screen latest-state view
  ATTENTION                           # sentinel, present when exit 2 or 3
  org-members/<org>.yaml              # cached org members + fetched_at
  default-branches.yaml               # cached repo default branches
  findings/<owner>__<repo>/           # scan artifacts (deferred, ck7n4pqr)
  cron/                               # gitbulk-cron output logs
```

## 6. The `GHClient` / agent-backend Protocol pattern

Both external CLI boundaries follow the same Protocol + Production + Fake
shape (node `ghclmp7n`):

1. **Protocol** (`@runtime_checkable`) defines the surface. `GHClient`
   methods accept a per-call `timeout` kwarg; callers pass it explicitly.
2. **Production implementation** subprocesses to the real CLI and parses
   output. For gh this is `ProductionGHClient` (with hardcoded retry policy
   so callers cannot disable it). For the agent side the single production
   implementation is `agent.CommandAgentBackend`, which drives any agent —
   claude included — from its `AgentProfile`; SEC-F1 removed the former
   native `ProductionClaudeClient`, so there is no agent-specific production
   client.
3. **Fake** (`FakeGHClient` / `FakeClaudeClient`) holds in-memory canned
   data and exposes a small builder API for tests. Per AGENTS.md, every test
   uses the Fake; the production gh client is exercised only by a single
   integration test (`test_gh_production.py`) gated behind explicit opt-in.

Tests inject by `monkeypatch.setattr("gitbulk.commands.report.ProductionGHClient", lambda: fake)`,
not by passing through the API — this lets the production code stay
written as `gh = ProductionGHClient()` (cleaner) while still being fully
substitutable.

The agent side deliberately omits retry (decision `ghclmp7n.f` carry-over):
an agent call that fails is almost always a prompt problem, and retrying
the same prompt would burn API budget without changing the outcome.

## 7. Lifecycle of a typical run

Each `commands/<sub>.py` handler follows the same skeleton, refined since
the Phase-1A snapshot to make every stage testable in isolation:

1. **Load policy + repos** via `config.policy.load_policy()` and
   `config.repos.load_repos(code_root=...)`. `--code-root` overrides the
   default `~/code/`.
2. **Acquire resource locks as needed** (node `rsclk7nq`): a writer takes
   `run_state_lock(sub, "exclusive", ...)` around its run-state symlink
   swap, plus `repo_lock(slug, ...)` and the singleton cache/sentinel/
   dashboard locks only around the sections that touch each resource — held
   briefly, never nested. Timeouts are bounded per `tmlk5pq3`;
   `LockTimeoutError` surfaces as exit 1, no ATTENTION.
3. **`RunState.begin(subcommand, argv, config_snapshot)`** — creates the
   `~/.cache/gitbulk/runs/<UTC-timestamp>-<sub>/` directory, writes the
   initial empty `state.yaml`, and stamps `manifest.yaml` with argv and an
   inline self-contained config snapshot (per `kp7nw4mq.a`).
4. **Partition the subcommand's invariant chain** by `InvariantKind` —
   UNIVERSAL → PER_REPO → PER_PR (per `c4jzm5pn`).
5. **Run UNIVERSAL preflight** once. First Fail → exit 1.
6. **For each repo**, run PER_REPO; intrinsic Skip drops the repo (counts
   toward exit 3); Fail aborts; cmdline-`--skip-check` Skip is bypassed
   (audit signal only).
7. **Coalesced GitHub fetch** (`gh.my_open_prs([surviving slugs])`) — one
   GraphQL round-trip across all surviving repos per `gd4kp7nz`.
8. **For each PR**, run PER_PR invariants; record a PR record into
   `state.yaml`. A PR is "attention" iff no Fail and no intrinsic Skip.
9. **Subcommand-specific work** — `report` just emits `summary.md`;
   `summarize` pipes `state.yaml` through the agent; `dispatch` creates a
   worktree per attention-PR and feeds the executor; the mutating
   subcommands (`merge`, `rebase-pr`, `close-stale`, `prune-branches`,
   `prune-worktrees`) act on each surviving PR/branch/worktree only when
   `--apply` is passed, taking the relevant `repo_lock` around each
   mutation.
10. **`_finish()`** — write `summary.md` if not already written, set
    ATTENTION sentinel iff exit ∈ {2, 3}, call `RunState.complete(exit_code,
    retain_runs=policy.defaults.retain_runs)` which atomically points
    `latest-<sub>` at the run dir and prunes older runs.

`gitbulk show` is the inverse path: it skips step 3 (does not create a
new run) and reads from `latest-<sub>` while holding
`run_state_lock(sub, "shared")` so a concurrent run of that subcommand
cannot swap the symlink mid-read.

Exit codes (decision `tp4kq2nr`):

| Code | Meaning |
|---|---|
| 0 | nothing to flag |
| 1 | structural failure (bad config, gh not authed, network, lock timeout) |
| 2 | PRs need user attention |
| 3 | at least one repo skipped by an invariant |
| 4 | `--skip-check` overrides applied (audit signal) |
| 99 | subcommand not implemented |

## 8. The `dispatch` flow specifically

`dispatch` is the most involved subcommand. Its pipeline (the mutating
subcommands `merge` / `rebase-pr` / `close-stale` / `prune-*` follow the
same §7 skeleton with their own per-PR/per-branch action step):

1. **Validate `--prompt PATH`** is provided, exists, non-empty. Without
   `--apply`, the run is a dry-run that still resolves the eligible PR
   set and writes `summary.md`, but the executor is never called.
2. **Invariant chain (`_CLONE_TOUCHING_CHAIN`)** runs through step 8 of
   §7 above. Note this chain adds the `local.*` invariants
   (`local.exists`, `local.remote_matches`,
   `local.default_branch_in_sync`) because dispatch needs a local clone
   to make a worktree from.
3. **For each attention-PR**, build an `ExecTarget`:
   - `worktree.create_worktree(clone_path, runid, slug, pr_number)` runs
     `git worktree add` into
     `~/.cache/gitbulk/worktrees/<runid>/<owner>__<repo>__pr<N>/`. The
     path-verification step in `worktree.py` is the load-bearing defense
     from AGENTS.md: if `git worktree add` silently failed, the
     `is_relative_to(worktree_root)` check rejects the fallback eagerly
     before any write happens.
   - `agentprep verify` runs in the worktree before claude is invoked
     (AGENTS.md managed block).
4. **`exec.execute_targets(targets, ...)`** — bounded ThreadPoolExecutor
   (`--concurrency`, default 2), per-target wall-clock timeout
   (`--timeout`, default 1800s). Each worker thread spawns its own
   `subprocess.Popen` from the resolved `AgentInvocation` argv (the agent
   binary + flags from the selected `AgentProfile`; `claude -p …` by
   default), `cwd=worktree`, env-scoped.
   - On per-target timeout: SIGTERM → 5s grace → SIGKILL.
   - On first CTRL+C: stop dequeueing new targets; in-flight finish.
   - On second CTRL+C within 10s: SIGTERM all in-flight, 5s, SIGKILL.
   - Results return in input order regardless of completion order.
5. **Teardown** — for each result, `worktree.remove_worktree(...)` runs
   `git worktree remove --force`. **Exception:** if the worktree shows
   merge/rebase conflicts (per `vp7n2krq`), it is preserved on disk and
   a `CONFLICT.md` is written into the run dir so the user can resolve
   at the next sitting.
6. **`_finish()`** writes the standard run artifacts as in §7.

Concurrency note (`rsclk7nq`): dispatch holds `repo_lock(slug)` exclusively
around each repo's worktree work, so two gitbulk runs cannot operate on the
same repo at once while leaving unrelated repos free. The bounded
`--concurrency` pool (default 2) is for parallelism *within* the single
dispatch invocation.

## 9. Test strategy

- **TDD is mandatory** (AGENTS.md). Tests are written before or alongside
  implementation, never after.
- **No network in tests** (AGENTS.md). `gh`, `git fetch`, `claude`, and
  any other network-touching subprocess are dependency-injected.
- **100% branch coverage on `src/gitbulk/`**, enforced by CI (decision
  `cn4pk7zq`). Any gap requires an approved `deviation:` node in
  [`this.i`](../this.i) per [`methodology.md`](methodology.md) §6.
- **Subprocess and filesystem dependencies are injected** via:
  - Protocol + Fake pairs (`GHClient`, the agent backend).
  - `_popen_factory` seam in `exec.py` for the parallel kernel.
  - `monkeypatch.setattr("gitbulk.commands.X.ProductionYClient", lambda: fake)`
    at the call site.
  - XDG fixture: every test that writes under `~/.cache/gitbulk/` first
    points `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` at a tmp dir.
- **Run isolation**: tests that exercise `RunState` create real run dirs
  under the tmp XDG cache; there is no in-memory mode. This keeps the
  test suite honest about the artifact layout downstream consumers
  (like `gitbulk show` and `dashboard.py`) actually read.

Baseline at the time of writing (v0.7.1): ~1780 hermetic tests
(`pytest -m "not e2e"`), all green at 100% branch coverage on
`src/gitbulk/`.

## 10. Safety & trust model

gitbulk is single-user, runs locally, and has no network-exposed surface —
the conventional "threat model" frame doesn't quite fit. What matters is
the **trust the user places in gitbulk to operate on their clones**. Five
boundaries:

1. **Working-tree integrity** (constraint `7mxr4pql`). Every code path
   that writes to disk must verify it is inside a configured worktree
   root, not the main clone. Worktree path verification is a hard rule
   in AGENTS.md and is implemented in `worktree.create_worktree`.
2. **Default-branch authority** (AGENTS.md). Every PR-touching operation
   verifies that the PR's `baseRefName` equals the repo's current
   GitHub default branch via the `pr.base_is_default` invariant. The
   explicit escape hatch is the audited `--skip-check pr.base_is_default`
   override (trips exit 4 + a WARNING per `r4nzp7kq`).
3. **Mutation gates** (`2vqp4nk6`). Every mutating subcommand defaults
   to `--dry-run`; `--apply` is required to act. Every invariant
   suppression logs a WARNING into `invariants.log` (well, into
   `errors.log` per the current schema) and trips exit code 4.
4. **gh CLI deprecation hygiene** (AGENTS.md "Verify gh invocations…"
   + `ghclmp7n`). Every `gh` command in `ProductionGHClient` is
   verified to not emit a deprecation warning at integration time;
   verification dates are recorded as call-site comments.
5. **Private umask** (security-hawk F3, 2026-05-28). `cli.main()` calls
   `os.umask(0o077)` at startup so every file under `~/.cache/gitbulk/`
   is owner-only — acceptable defense-in-depth on shared hosts and
   bind-mounted containers.

No secrets are stored in this repo or in `~/.cache/gitbulk/`.
Authentication is delegated entirely to `gh` (GitHub) and the user's
`ssh-agent` (git).

## 11. Configuration reference

Two files, defaulting to `~/.config/gitbulk/`:

- **`repos.txt`** — one `owner/repo` slug per line. `#` comments and
  blank lines ignored. Slug shape is validated against a strict regex
  (security-hawk F1, 2026-05-28) that rejects `.`/`..` segments.
- **`gitbulk.yaml`** — policy and classification. Shipping example at
  [`config/gitbulk.yaml.example`](../config/gitbulk.yaml.example). Key
  keys (loaded by `config/policy.py`):
  - `defaults.merge_policy: strict | ci-only | never`
  - `defaults.min_business_days: int` (default `3`, per `bg4pqn7m`)
  - `defaults.unresolved_burden: me | other | either` (default `me`,
    per `hj3nq5kp`)
  - `defaults.bot_threads_block: bool` (default `true`, per `zk3r4nqp`)
  - `defaults.stale_age_days`, `defaults.stale_cooloff_days`
  - `defaults.retain_runs: int` (consumed by `RunState.complete` →
    `gc.prune_runs`)
  - `humans.org`, `humans.exceptions`, `humans.always_human`
  - `bots:` list
  - `repos.<slug>:` per-repo overrides
  - `worktree_root: str` (default `~/.cache/gitbulk/worktrees`, per
    `mw6kp2nq`)

## 12. Phase plan

Crossed-out phases are landed and verified.

- ~~**Phase 0** — Scaffold (CLI shell, AGENTS.md, this.i)~~ ✓
- ~~**Phase 1A–1D** — Foundations (paths, config, locks, sentinel,
  runstate, dashboard, invariant runner)~~ ✓
- ~~**Phase 2** — Read-only GitHub: `gh.py`, `pr_info.py`,
  `classifier.py`, `org_members_cache.py`, invariant catalog,
  `gitbulk report`~~ ✓
- ~~**Phase 3** — `summarize`: `claude.py` Protocol + Production +
  Fake, `commands/summarize.py`, packaged default prompt, `--prompt`
  / `--model` overrides~~ ✓
- ~~**Phase 4** — `dispatch`: `worktree.py`, `exec.py` bounded
  parallel kernel, `commands/dispatch.py`, dry-run default,
  agentprep integration~~ ✓
- ~~**Phase 5** — Mutating subcommands: `rebase-pr`, `merge`,
  `close-stale`. Added the mutating methods on the `GHClient` Protocol
  (`merge_pr`, `close_pr`, `delete_branch_ref`, …) and per-repo exclusive
  locking; every mutator defaults to dry-run.~~ ✓
- ~~**Phase 6** — Polish: `gitbulk show`, README crontab examples,
  architecture refresh (this document)~~ ✓
- ~~**Fleet cleanup** — `prune-branches` and `prune-worktrees` (resolving
  tension `jw3kpn4q`); resource-scoped locking rework (`rsclk7nq`).~~ ✓

## 13. Known gaps & future work

- **Scan / findings subcommand is undesigned** (tension `ck7n4pqr`).
  Will land when there's a real workflow that needs per-repo findings
  separate from PR triage. The `findings/` cache dir is reserved for it.
- **Proactive no-PR-yet discovery is deferred** (the open remainder of
  tension `jw3kpn4q`). The fleet-cleanup half of that scope shipped as
  `prune-branches` / `prune-worktrees`; discovering repos that need work
  before any PR exists is still future work.
- **Default-branch rename handling is open** (tension `rj7p4kqn`).
  Today the `pr.base_is_default` invariant compares against whatever
  GitHub currently returns; an in-flight rename will skip the PR
  rather than silently merge against the old name. Acceptable for v1.
- **No dedicated integration sandbox.** The hermetic suite is fully
  offline; a single opt-in integration test (`test_gh_production.py`)
  exercises real `gh` calls against a user-supplied repo, but there is
  no standing sandbox repo set wired into CI for adversarial `--apply`
  runs of the mutating subcommands.

## 14. Contributor reading order

For a new contributor (human or AI) coming cold:

1. [`AGENTS.md`](../AGENTS.md) — the behavioral contract. **Read first.**
2. [`this.i`](../this.i) — the design-decision tree. Skim the goal node
   `q3kfzm7n`, then the constraints, then the most relevant decisions
   (`tp4kq2nr`, `ghclmp7n`, `execk7nm`, `mw6kp2nq`).
3. This file (`docs/architecture.md`) — the map you are currently in.
4. [`docs/design-notes.md`](design-notes.md) — narrative explainer with
   pointers into `this.i`.
5. [`docs/methodology.md`](methodology.md) — the development discipline
   this repo follows. Required reading before any change that meets
   the §3 trigger list.
6. [`src/gitbulk/subcommands.py`](../src/gitbulk/subcommands.py) — the
   typed registry; the cleanest map from "name the user types" to
   "what the code does."
7. [`src/gitbulk/commands/report.py`](../src/gitbulk/commands/report.py)
   — the canonical handler shape; everything else mirrors it.
8. [`tests/test_report.py`](../tests/test_report.py) — the canonical
   test shape; FakeGHClient injection, isolated XDG fixture, exit-code
   coverage per branch.
