# gitbulk design notes

Captured from the design conversation that produced the Phase 0 scaffold
and the Phase 1A foundations session.

> **Authoritative source:** As of 2026-05-27, [`../this.i`](../this.i) is
> the source of truth for design decisions. This file is the *narrative
> explainer* that points into it. When `this.i` and this file disagree,
> `this.i` wins.
>
> Node ids (e.g., `7mxr4pql`) below cross-reference into `this.i`. See
> [`methodology.md`](methodology.md) for what intent nodes are and why
> they exist as a separate layer above the code.

This is not a spec. AGENTS.md is the contract; `this.i` is the
"why we chose what we chose" record; this file is the human-readable
walking tour of both.

---

## 1. Problem statement

The user contributes to ~150 git repos (most under `provenant-dev`, some
open source). Open PRs across that set are too many to triage by hand
each day. `gitbulk` runs unattended (typically from cron) and:

- reports the state of those PRs (ready-to-merge, blocked-by-CI,
  awaiting-human-response, awaiting-bot-response, conflicts,
  non-default-base, stale, etc.);
- can launch headless Claude Code agents against PRs matching a filter,
  with a pluggable prompt;
- can rebase the user's PRs onto their repo's current default branch;
- can auto-merge PRs that pass a per-repo policy and have aged past a
  threshold;
- can close PRs that have gone stale.

All of this must be safe to run concurrently with active development on
the same local clones.

## 2. Configuration model

Two files, defaulting to `~/.config/gitbulk/`.

**`repos.txt`** — plain `owner/repo` per line. `#` comments and blank
lines ignored. Local clone resolved as `~/code/<basename(repo)>` unless
`--code-root` overrides. Format chosen for minimal friction; richer
formats (YAML, inline tags) were considered and rejected for v1.

**`gitbulk.yaml`** — policy and classification. See
`config/gitbulk.yaml.example`. Highlights:

- `defaults.merge_policy` — `strict` (approval + green CI + clean +
  default-branch-target + age), `ci-only` (drops the approval gate),
  or `never`.
- `defaults.min_age_days` — auto-merge age threshold. Default value
  **TBD** (see open questions).
- `defaults.stale_age_days` / `stale_cooloff_days` — close-stale knobs.
- `humans.org`, `humans.exceptions`, `humans.always_human` — see
  classification model below.
- `bots:` — known non-human accounts.
- `repos:` — per-repo overrides for any of the above, plus per-repo
  `skip_checks` and `extra_checks`.

## 3. Humans-vs-bots classification

Unknown accounts default to **non-human**, because the set of bots will
grow over time and silently treating a new bot as human would be the
wrong failure mode.

Resolution order, evaluated per comment/review author:

1. `login in always_human` → human.
2. `login in bots` → non-human.
3. `login in org_members AND login not in exceptions` → human.
4. Otherwise → non-human.

Org members are fetched once via `gh api orgs/<org>/members` and cached
for `cache_ttl_hours`. CCR, Dependabot, GitHub Actions, etc. are
seeded in the example config.

## 4. Subcommands

| Subcommand | Purpose |
|---|---|
| `report` | Structured + human-readable state of every open PR across configured repos. |
| `summarize` | Run Claude over the latest report to surface what most needs attention. |
| `dispatch` | Launch headless `claude -p` per matching PR, inside a worktree, logs captured. |
| `merge` | Auto-merge PRs that pass per-repo policy. Default `--dry-run`. |
| `rebase-onto-default` | Rebase user's PRs onto their repo's default branch. Default `--dry-run`. |
| `close-stale` | Close inactive PRs with a configurable message. Default `--dry-run`. |
| `show` | Cat the latest summary of a given subcommand's run. |
| `ack` | Clear the `ATTENTION` sentinel after review. |
| `invariants` | List the invariant registry and which subcommands use each. |

Every mutating subcommand defaults to `--dry-run` and requires `--apply`
to act. A misconfigured cron entry must not silently mutate state.

## 5. Local-git safety contract

The most important invariant in the tool. Restated from AGENTS.md so
this doc is self-contained:

- Never modify the working tree, index, or HEAD of any clone under
  `~/code/`.
- Any operation that requires a checkout creates a `git worktree` under
  a disposable root (`/tmp/gitbulk/` by default — see open questions)
  and cleans it up in `finally`.
- Before writing inside a worktree, verify the path actually resolves
  under the worktree root and is not the main clone (defensive against
  worktree-creation bugs).
- Read-only `git -C <path> <subcmd>` is fine for status/config/log/etc.

## 6. Concurrency

Two `gitbulk` processes must be safe to run at the same time.

- **Global advisory lock** (`~/.cache/gitbulk/run.lock`, `fcntl.flock`):
  shared for read-only subcommands, exclusive for mutating ones.
  Multiple `report` runs may overlap; a `merge` waits for reports to
  finish and vice versa.
- **Per-repo lock** (`~/.cache/gitbulk/locks/<owner>__<repo>.lock`):
  held for the duration of any mutating op on that repo. Lets one
  process merge repo A while another reports on repo B.
- **Run state** (`~/.cache/gitbulk/runs/<timestamp>-<subcommand>/`):
  durable record of decisions, survives crashes, supports a future
  `--resume`.

## 7. Invariants framework

Each subcommand runs an ordered chain of named **invariants** before
acting. An invariant is a small function
`(repo, pr_or_none, context) -> Pass | Skip(reason) | Fail(reason)`:

- **Pass** → proceed.
- **Skip** → skip this repo/PR, log reason, continue with others.
- **Fail** → abort the whole run (structural problem: bad auth, etc.).

All invariants live in a registry. `gitbulk invariants` and
`gitbulk <subcmd> --list-checks` print them. Cmdline `--require NAME`
and `--skip-check NAME` add or suppress individual invariants for a
single run. Every suppression is logged into run state with a WARNING
so an audit can find every loosened operation.

### Default catalog

**Universal preflight (once per run):**
- `gh.authenticated`
- `config.parseable`
- `org.members.fresh`

**Per-repo preflight (every subcommand):**
- `local.exists`
- `local.remote_matches`
- `local.default_branch_in_sync`
- `github.reachable`
- `github.not_archived`

**Per-PR baseline (report, summarize, dispatch, merge, rebase, close):**
- `pr.base_is_default`
- `pr.author_known`

**Mutating only (merge, rebase, close, dispatch):**
- `local.no_uncommitted_in_pr_branch`
- `local.recent_push_quiescence`
- `repo.not_in_deny_list`

**Merge-only:**
- `pr.mergeable_state_clean`
- `pr.required_checks_green`
- `pr.approved_per_policy`
- `pr.no_blocking_label`
- `pr.age_threshold`
- `pr.no_unresolved_threads`

**Rebase-onto-default-only:**
- `pr.author_is_me`
- `pr.no_automerge_pending`
- `pr.force_push_allowed`

**Close-stale-only:**
- `pr.inactive`
- `pr.previously_warned`  (refuses to close on first sight; needs
  persistent state across runs)

**Dispatch-only:**
- `repo.agentprep_verified` — runs `agentprep verify` in the clone
- `repo.agentprep_initialized`
- `prompt.exists_and_nonempty`
- `system.resources_available`

## 8. Notification & error visibility (layers 1–4 in v1)

v1 ships the file-based layers only. External adapters (ntfy.sh, slack,
desktop notifications) are deliberately deferred.

**Layer 1 — structured run artifacts:**
```
~/.cache/gitbulk/runs/<timestamp>-<subcommand>/
  state.yaml         # full run state — every repo, every decision
  summary.md         # human-readable summary
  errors.log         # warnings and errors only
  invariants.log     # every skip/fail with reason
  manifest.yaml      # subcommand, config, flags, version
```
Symlinks `~/.cache/gitbulk/runs/latest-<subcommand>` always point at
the newest run of that subcommand.

**Layer 2 — exit codes:**
- `0` nothing to flag
- `1` structural failure (bad config, gh not authed, network)
- `2` ran successfully but PRs need user attention
- `3` ran successfully but at least one repo skipped by an invariant
- `4` ran with `--skip-check` overrides applied (audit signal)
- `99` subcommand not implemented (Phase 0 scaffold sentinel)

**Layer 3 — dashboard:** `~/.cache/gitbulk/dashboard.md` rewritten on
every run with a single-screen view of the most recent state across all
subcommands. Designed to be `cat`-able at shell start.

**Layer 4 — ATTENTION sentinel:** `~/.cache/gitbulk/ATTENTION` created
when exit code is 2 or 3. Shell-prompt or tmux-statusline integration
shows a glyph while it exists. `gitbulk ack` removes it.

**Cron wrapper convention:** `bin/gitbulk-cron` captures all stdout/stderr
to `~/.cache/gitbulk/cron/<timestamp>-<subcommand>.log` and symlinks
`last-failure.log` on non-zero exit.

## 9. Phase plan

- **Phase 0 — Scaffold** *(done)*: repo init, AGENTS.md, package
  skeleton, argparse shell, smoke tests passing.
- **Phase 1 — Core infra (no network)**: config loader, invariant
  registry + chain runner, run state, dashboard, ATTENTION sentinel,
  `ack`, exit-code wiring, locking. All unit-tested, network-free.
- **Phase 2 — Read-only GitHub**: `gh` wrappers, human/bot classifier
  with cached org members, worktree helper with path verification.
  `report` working end-to-end. `invariants` + `--list-checks` introspection.
- **Phase 3 — `summarize`**: consume Phase 2 report runs, invoke
  `claude -p`, write LLM output into the run dir.
- **Phase 4 — `dispatch`**: per-PR worktree, headless `claude`,
  log capture, concurrency cap of 2 (matches user's subagent rule).
  `repo.agentprep_verified` wired in.
- **Phase 5 — Mutating subcommands**: `rebase-onto-default` first
  (teaches the push/lease patterns), then `merge`, then `close-stale`
  (which depends on persistent state for the `pr.previously_warned`
  cooloff).
- **Phase 6 — Polish**: `gitbulk show`, README crontab examples,
  end-to-end docs.

Each phase is its own commit (or small PR-equivalent set), with TDD
discipline per phase.

## 10. Out of scope for v1

- A web UI.
- External notification adapters (ntfy, slack, desktop). Layers 5+ in
  the design — explicitly deferred.
- ~~A bundled single-file executable like agentprep ships. `pip install
  -e .` is fine for v1.~~ **Superseded 2026-05-29** (`this.i` node
  `dstbr5kq`): gitbulk now ships a hybrid distribution — it stays
  pip-installable AND ships a single self-contained zipapp fetched from a
  GitHub release, with a `gitbulk install` self-installer and a
  notice-only `gitbulk update`. See the Install & Distribution subtree in
  `this.i`.
- A GitHub remote for this repo. Local-only until the user runs
  `gh repo create` themselves.
- Cron file installation. The user wires their own crontab around
  `bin/gitbulk-cron`.

## 11. Resolution status of the Phase-0 open questions

Phase-0 closed with seven open questions. All seven now have positions
recorded in `this.i` — six as decisions, one as a deferred tension:

| # | Question | Resolution | `this.i` node |
|---|---|---|---|
| 1 | Default `min_age_days` for auto-merge | 3 business days (M–F, local TZ, no holidays) since `ready_since`; "ready" itself is stricter than GitHub-clean (bot threads block too) | `bg4pqn7m` + `zk3r4nqp` |
| 2 | Worktree root location | `~/.cache/gitbulk/worktrees/<runid>/<owner>__<repo>/` — survives reboot for crash forensics, XDG-conventional | `mw6kp2nq` |
| 3 | `summarize` prompt design | Deferred to Phase 3 entry — depends on `report`'s structured output, which doesn't exist yet | `kw2pn7qz` (tension) |
| 4 | `rebase-onto-default` UX on conflicts | Keep the conflicted worktree; write `CONFLICT.md` with fix-up commands; `gitbulk gc` skips worktrees in conflict state | `vp7n2krq` |
| 5 | Per-repo policy precedence | Cmdline always wins. Relaxing (`--skip-check`) trips exit 4 + WARNING; tightening (`--require`) logs INFO only | `r4nzp7kq` |
| 6 | `gh` rate limiting | Serial per-repo + GraphQL coalescing, no limiter in v1 (~300 calls vs 5000/hr budget) | `gd4kp7nz` |
| 7 | Missing local clone | Skip-with-warning, scoped to subcommands that need a clone; never auto-clone, never fail the whole run | `5xqp2nkr` |

### New decisions and tensions opened during the Phase-1A session

| Topic | `this.i` node | Status |
|---|---|---|
| Personal account owns the public repo (dhh1128/gitbulk, not provenant-dev) | `6xp4kq2n` | decision |
| Local repos are first-class citizens — fleet = (repos × PRs), not just PRs | `xq4npk7r` | decision |
| Methodology adoption (`this.i`, speculative interview, gates, adversarial review) | `nh4kp2rq` | decision |
| 100% branch coverage on `src/gitbulk/`, enforced in CI | `cn4pk7zq` | decision |
| `dispatch` execution kernel — subprocess multiprompt, shared kernel, or reimplement | `mp7kn4qz` | tension (Phase 4) |
| Multiprompt packaging future — multiprompt's own this.i / CI / release story | `fw5kq6np` | tension (Phase 4) |
| Scan and findings artifact convention — format, location, lifecycle | `ck7n4pqr` | tension (Phase 4) |
| Repo cleanup subcommand scope — worktrees, branches, refs | `jw3kpn4q` | tension (Phase 5/6) |
| Default branch rename handling | `rj7p4kqn` | tension |

When in doubt, bias toward "skip with reason logged" over "do something
risky."
