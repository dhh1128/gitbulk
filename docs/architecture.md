# Architecture — gitbulk

> **Status: Phase 4 complete; Phase 5 (mutating subcommands) and the
> throwaway test repo are still ahead.** Read-only triage (`report`),
> Claude-assisted summarization (`summarize`), and headless agent dispatch
> into per-PR worktrees (`dispatch`) all run end-to-end on real fleets.
> Polish-phase additions (`gitbulk show`, README crontab examples, this
> document) landed in Phase 6.

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
  agent dispatch (`dispatch`), and eventually merge / rebase-onto-default /
  close-stale (Phase 5).
- **Local repos themselves** (decision `xq4npk7r`) — orphaned worktrees,
  undeleted post-merge branches, stale local refs, and proactive discovery
  of repos that need work no PR yet exists for (deferred, `jw3kpn4q`).

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
   │   (Phases 0–4 implemented;     │         │   (user's clones,    │
   │    Phase 5 mutators pending)   │         │    read-only here)   │
   └──────┬───────────────────┬──────┘         └──────────────────────┘
          │                   │
          │ subprocess        │ subprocess (Phase 3+4)
          ▼                   ▼
       ┌──────┐         ┌────────────┐         ┌──────────────────────┐
       │  gh  │         │   claude   │ ──────▶ │  per-repo artifacts  │
       │ CLI  │         │     -p     │         │  (runs/, findings/)  │
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
| Distribution | `pip install -e .` (no wheel/PyPI yet) | Personal tool; v1 doesn't need a release pipeline. |
| GitHub network | `gh` CLI subprocess + `GHClient` Protocol | Constraint `hp4nck2v` — reuse user's auth, free GraphQL, no second credential surface. Tests inject `FakeGHClient`. |
| Claude calls | `claude -p` subprocess + `ClaudeClient` Protocol | Symmetric to gh shape; no retries (a bad prompt is a thinking problem, not transient). |
| Git network | SSH | Constraint `ks52rg4w` — reuse user's ssh-agent. |
| Config | YAML + plain-text | Decision `ws2pn4kr` — `repos.txt` plain, `gitbulk.yaml` for policy. |
| State | Files under `~/.cache/gitbulk/` | Decision `tp4kq2nr` — file-based, no DB, no external services. Schema-versioned (node `schv4nrm`). |
| Locking | `fcntl.flock` advisory | Decision `lj5pqn4kr` — global + per-repo, lets parallel reads coexist. Bounded timeouts per `tmlk5pq3`. |
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
  drain), which the blocking `ClaudeClient.run_prompt` Protocol does not
  expose; the kernel reads `claude_path`/`default_model` from the client
  but does its own Popen.
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
- `jw3kpn4q` Repo cleanup subcommand scope — deferred to Phase 5/6.
- `rj7p4kqn` Default-branch rename handling — still open.

## 5. Internal structure

```
src/gitbulk/
  __main__.py             — `python -m gitbulk` entry point
  cli.py                  — argparse shell, subcommand dispatch, exit codes,
                            ATTENTION fallback wiring, logging, umask
  subcommands.py          — typed registry of every subcommand (node
                            smodlpr3); single source of truth for invariant
                            chains (scinv4qm), lock mode (lj5pqn4kr), and
                            whether the subcommand needs a clone (5xqp2nkr)
  paths.py                — XDG-aware path helpers; slug-normalization
                            (security-hawk F1 hardening); compact UTC runids
  config/
    repos.py              — repos.txt parser
    policy.py             — gitbulk.yaml parser + dataclass schema
  util/
    businessdays.py       — M–F arithmetic in local TZ (per bg4pqn7m)
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
  gh.py                   — GHClient Protocol + ProductionGHClient + FakeGHClient
  claude.py               — ClaudeClient/AgentBackend Protocols + AgentInvocation
                            + FakeClaudeClient (no retry; bad prompts are
                            thinking problems). The production backend is
                            agent.CommandAgentBackend (SEC-F1 removed the
                            native ProductionClaudeClient).
  pr_info.py              — frozen PRInfo dataclass (node prdtm4kn)
  exec.py                 — in-tree parallel claude executor (execk7nm);
                            ExecTarget / ExecResult / execute_targets;
                            bounded pool, timeout escalation, CTRL+C drain
  worktree.py             — `git worktree add` + path verification +
                            disposal; CONFLICT.md preservation (vp7n2krq)
  runstate.py             — per-run dir orchestration: manifest.yaml,
                            state.yaml, summary.md, invariants.log,
                            errors.log; atomic writes; latest-* symlink;
                            calls gc.prune_runs on complete
  locks.py                — global + per-repo flock with JSON metadata,
                            pid-liveness probe in LockTimeoutError
  sentinel.py             — ATTENTION file management (one-line JSON per
                            schv4nrm; supersedes the old whitespace format)
  dashboard.py            — dashboard.md rewriter (excerpt-per-subcommand)
  gc.py                   — retention pruning for runs/<runid>-<sub>/
                            beyond policy.defaults.retain_runs
  commands/
    report.py             — read-only PR triage (Phase 2)
    summarize.py          — Claude-driven prioritization (Phase 3)
    dispatch.py           — agent dispatch into worktrees (Phase 4)
    show.py               — read prior run artifacts (Phase 6)

tests/                    — pytest tests; mirrors src/ layout. 100% branch
                            coverage enforced.
bin/gitbulk-cron          — cron wrapper (the only shell script in the repo)
config/*.example          — example user config
prompts/                  — pluggable prompts for summarize & dispatch
```

### Run state layout

```
~/.cache/gitbulk/
  run.lock                            # global advisory lock (JSON metadata)
  locks/<owner>__<repo>.lock          # per-repo locks
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
  findings/<owner>__<repo>/           # scan artifacts (deferred, ck7n4pqr)
  cron/                               # gitbulk-cron output logs
```

## 6. The `GHClient` / `ClaudeClient` Protocol pattern

Both external CLI boundaries use the same three-layer shape (node `ghclmp7n`):

1. **Protocol** (`@runtime_checkable`) defines the read/produce-text surface.
   Methods accept a per-call `timeout` kwarg; callers pass it explicitly.
2. **`Production*Client`** subprocesses to the real CLI and parses output.
   Retry policy (gh) is hardcoded inside the production client so callers
   cannot inadvertently disable it.
3. **`Fake*Client`** holds in-memory canned data and exposes a small
   builder API for tests. Per AGENTS.md, every test in the project uses
   the Fake; the production client is exercised only by a single integration
   test (`test_gh_production.py`) gated behind explicit opt-in.

Tests inject by `monkeypatch.setattr("gitbulk.commands.report.ProductionGHClient", lambda: fake)`,
not by passing through the API — this lets the production code stay
written as `gh = ProductionGHClient()` (cleaner) while still being fully
substitutable.

The Claude side deliberately omits retry (decision `ghclmp7n.f` carry-over):
a Claude call that fails is almost always a prompt problem, and retrying
the same prompt would burn API budget without changing the outcome.

## 7. Lifecycle of a typical run

Each `commands/<sub>.py` handler follows the same skeleton, refined since
the Phase-1A snapshot to make every stage testable in isolation:

1. **Load policy + repos** via `config.policy.load_policy()` and
   `config.repos.load_repos(code_root=...)`. `--code-root` overrides the
   default `~/code/`.
2. **Acquire the global lock** (`global_lock("shared", timeout=300, ...)`
   for read-only; `"exclusive", timeout=1800, ...` for mutating — both
   from `tmlk5pq3`). `LockTimeoutError` surfaces as exit 1, no ATTENTION.
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
   `summarize` pipes `state.yaml` through Claude; `dispatch` creates a
   worktree per attention-PR and feeds the executor.
10. **`_finish()`** — write `summary.md` if not already written, set
    ATTENTION sentinel iff exit ∈ {2, 3}, call `RunState.complete(exit_code,
    retain_runs=policy.defaults.retain_runs)` which atomically points
    `latest-<sub>` at the run dir and prunes older runs.

`gitbulk show` is the inverse path: it skips step 3 (does not create a
new run) and reads from `latest-<sub>` while holding the shared global
lock so a concurrent mutating run cannot swap the symlink mid-read.

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

`dispatch` is the most involved subcommand and the only one (today) that
mutates outside `~/.cache/gitbulk/`. Its pipeline:

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
   `subprocess.Popen(["claude", "-p", prompt_text], cwd=worktree, ...)`.
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

Concurrency note (`lj5pqn4kr`): dispatch takes the exclusive global lock,
so no other gitbulk process can be running. The bounded `--concurrency`
pool is for parallelism *within* the single dispatch invocation.

## 9. Test strategy

- **TDD is mandatory** (AGENTS.md). Tests are written before or alongside
  implementation, never after.
- **No network in tests** (AGENTS.md). `gh`, `git fetch`, `claude`, and
  any other network-touching subprocess are dependency-injected.
- **100% branch coverage on `src/gitbulk/`**, enforced by CI (decision
  `cn4pk7zq`). Any gap requires an approved `deviation:` node in
  [`this.i`](../this.i) per [`methodology.md`](methodology.md) §6.
- **Subprocess and filesystem dependencies are injected** via:
  - Protocol + Fake pairs (`GHClient`, `ClaudeClient`).
  - `_popen_factory` seam in `exec.py` for the parallel kernel.
  - `monkeypatch.setattr("gitbulk.commands.X.ProductionYClient", lambda: fake)`
    at the call site.
  - XDG fixture: every test that writes under `~/.cache/gitbulk/` first
    points `XDG_CONFIG_HOME` / `XDG_CACHE_HOME` at a tmp dir.
- **Run isolation**: tests that exercise `RunState` create real run dirs
  under the tmp XDG cache; there is no in-memory mode. This keeps the
  test suite honest about the artifact layout downstream consumers
  (like `gitbulk show` and `dashboard.py`) actually read.

Baseline at the time of writing (Phase 6 polish): 611 tests, ~2087
statements, ~596 branches, all at 100%.

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
   GitHub default branch via the `pr.base_is_default` invariant.
   `--allow-non-default-base` is the planned explicit escape hatch
   (Phase 5).
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
- **Phase 5** — Mutating subcommands: `rebase-onto-default` first,
  then `merge`, then `close-stale`. Each requires new mutating
  methods on the `GHClient` Protocol (intentionally absent today
  so no Phase-2 invariant can accidentally mutate) and per-repo
  exclusive locking.
- ~~**Phase 6** — Polish: `gitbulk show`, README crontab examples,
  architecture refresh (this document)~~ ✓

## 13. Known gaps & future work

- **Phase 5 mutating subcommands are still ahead.** `merge`,
  `rebase-onto-default`, and `close-stale` are scaffolded in
  `subcommands.py` and the CLI but return exit 99. The `GHClient`
  Protocol intentionally has no mutating methods until Phase 5 — see
  `ghclmp7n` — so no current code path can accidentally mutate.
- **Throwaway test repo for integration runs is still pending.** The
  current test suite is fully offline; a single opt-in integration
  test (`test_gh_production.py`) exercises real `gh` calls against a
  user-supplied repo, but there is no dedicated `gh-cli-sandbox`
  style repo set up for adversarial CI runs of `dispatch --apply`.
- **Scan / findings subcommand is undesigned** (tension `ck7n4pqr`).
  Will land when there's a real workflow that needs per-repo findings
  separate from PR triage.
- **Repo cleanup subcommand scope is open** (tension `jw3kpn4q`).
  Worktrees, undeleted branches, stale refs — Phase 5/6.
- **Default-branch rename handling is open** (tension `rj7p4kqn`).
  Today the `pr.base_is_default` invariant compares against whatever
  GitHub currently returns; an in-flight rename will skip the PR
  rather than silently merge against the old name. Acceptable for v1.
- **No license file.** TODO before any public-facing remote push.

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
