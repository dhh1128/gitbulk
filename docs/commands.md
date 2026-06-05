# Commands

Every gitbulk subcommand operates across your whole [configured
fleet](configuration.md). The mutating ones **default to dry-run** — they print
what they *would* do and change nothing until you pass `--apply`. Most accept
the `--org`/`--repo`/`--filter` fleet filters to narrow the set of repos they
act on.

| Subcommand | What it does | Mutating? |
|---|---|---|
| [`report`](#report) | Run the invariant chain against your open PRs and write a structured triage report (`summary.md` + `state.yaml`). | No |
| [`summarize`](#summarize) | Feed a recent `report` run through Claude with a triage prompt to prioritize. | No |
| [`dispatch`](#dispatch) | Spawn headless Claude agents inside disposable worktrees against PRs matching a filter. | Yes (`--apply`) |
| [`merge`](#merge) | Auto-merge PRs that satisfy the per-repo merge policy. | Yes (`--apply`) |
| [`rebase-pr`](#rebase-pr) | Rebase your behind/conflicting PRs onto their current base and force-push (with lease). | Yes (`--apply`) |
| [`close-stale`](#close-stale) | Warn, then close, PRs inactive past the configured threshold. | Yes (`--apply`) |
| [`prune-branches`](#prune-branches) | Delete remote branches whose only PRs are merged/closed, with guardrails. | Yes (`--apply`) |
| [`prune-worktrees`](#prune-worktrees) | Remove local linked worktrees whose branch's only PRs are merged/closed, then delete the merged local branch. | Yes (`--apply`) |
| [`show`](#show) | Print the latest run's artifacts for any subcommand, or the dashboard. | No |
| [`ack`](#ack) | Clear the `ATTENTION` sentinel after you've reviewed it. | No |
| [`invariants`](#invariants) | List the invariant registry and which subcommands use each. | No |

## Read-only commands

### `report`

Runs the invariant chain against your open PRs and writes a structured triage
report — `summary.md` (human-readable) and `state.yaml` (structured PR
records) — under a new run directory. This is the command you'll schedule
nightly; it's safe to run alongside local work and refreshes the
[`ATTENTION` sentinel](running-unattended.md#surfacing-attention-in-your-shell)
when a PR needs a look.

### `summarize`

Feeds a recent `report` run through a coding agent (Claude by default) with a
triage prompt to prioritize what needs your attention first, writing a
prioritized `summary.md`. `--model NAME` overrides the model; `--agent NAME`
selects a different agent (see [pluggable agents](configuration.md#agents--default_agent--which-coding-agent-to-drive)).

### `show`

The human-facing way to read what a run produced:

```bash
gitbulk show                       # dashboard (~/.cache/gitbulk/dashboard.md)
gitbulk show report                # latest report's summary.md
gitbulk show report --state        # state.yaml (structured PR records)
gitbulk show report --invariants   # invariants.log (JSONL audit trail)
gitbulk show report --errors       # errors.log (JSONL)
gitbulk show report --manifest     # manifest.yaml (argv, config snapshot)
gitbulk show report --path         # just the run-dir path (for scripting)
```

Viewing a run also clears the `ATTENTION` sentinel when it's the run that
raised it — see [Inspecting runs](reference.md#inspecting-runs) and
[Surfacing attention](running-unattended.md#surfacing-attention-in-your-shell).

### `ack`

Explicitly clears any outstanding `ATTENTION` sentinel — the catch-all when
`show` hasn't already cleared it (e.g. a legacy or corrupt sentinel). You
rarely need it.

### `invariants`

Lists the invariant registry — the named checks gitbulk runs — and which
subcommands use each. Handy when configuring `skip_checks` / `extra_checks`.

## Mutating commands

All default to dry-run; add `--apply` to act.

### `dispatch`

Spawns headless coding agents (Claude Code by default) inside disposable
worktrees against PRs matching a filter, to fix common problems. Per-PR
worktrees live under `~/.cache/gitbulk/worktrees/<runid>/` and are cleaned up
automatically. You supply the instructions with `--prompt <file>`.

`--agent NAME` selects which coding agent to drive (a preset — `claude`,
`gemini`, `copilot`, `cursor` — or a profile from `gitbulk.yaml`), `--model
NAME` overrides its model. See [pluggable agents](configuration.md#agents--default_agent--which-coding-agent-to-drive).
For conflict-resolution work **gitbulk owns the network**: it fetches the base
and performs the `force-push-with-lease` itself after verifying the agent's
result, so the agent never pushes — which also lets it run sandboxed with no
network or credentials.

By default `dispatch` only acts on PRs **you authored** (a stranger's PR is
attacker-controllable input to an auto-approve agent); `--allow-foreign-authors`
opts in and is refused in unattended/cron mode.

### `merge`

Auto-merges PRs that satisfy the [per-repo merge policy](configuration.md#defaults-merge-and-stale-policy)
— the `merge_policy`, `min_business_days`, and thread-resolution rules. The
merge method (`rebase`/`merge`/`squash`) comes from config.

### `rebase-pr`

Rebases your behind-or-conflicting PRs onto their current base and force-pushes
with a lease (`--force-with-lease`), so it never clobbers a concurrent push.

### `close-stale`

Warns, then later closes, PRs inactive past `stale_age_days`, respecting
`stale_cooloff_days` between the warning and the close. Governed by
`stale_policy`.

## Fleet cleanup: `prune-branches` and `prune-worktrees`

These remove the cruft that accumulates around a large fleet: post-merge
branches GitHub didn't auto-delete, and worktrees left over from finished PR
work. Both default to dry-run and take `--apply`; the guardrails are built so
`--apply` is **safe to run unattended** — you should rarely need to study a dry
run first.

!!! info "Shared guardrails"
    - **Grace period** — a branch/worktree is only touched once its PR has been
      merged/closed for at least `prune_min_age_days` (default 7, per-repo
      overridable). Just-merged work is left alone.
    - **No data loss** — a remote branch is deleted only when its tip is the
      merged PR's recorded head SHA *or* it is fully contained in the default
      branch; a worktree's branch is removed only when it has no unpushed
      commits. Anything with unique work is kept, with a reason.

### `prune-branches`

Deletes remote branches whose only PRs are merged or closed. It **never
deletes** the default branch, a protected branch, the head of an open PR, or
the base of an open PR (the stacked-PR case), and never touches fork branches.
Deletion goes through the GitHub ref API (not `git push --delete`), and the
deleted SHA is recorded for recovery.

### `prune-worktrees`

Removes local *linked* worktrees whose branch's only PRs are merged/closed,
then deletes the fully-merged local branch. It **never touches** the primary
clone (the [local-git safety contract](reference.md#local-git-safety-contract)),
a dirty worktree (uncommitted changes, untracked files unless
`--include-untracked`, or a merge/rebase in progress), or a locked one. It uses
`git worktree remove` *without* `--force`, then `git branch -d` (merged-only),
so an unmerged branch is kept.

A clean `--apply` run is quiet (no `ATTENTION`); only failures and skipped
repos surface. Both accept the `--org`/`--repo`/`--filter` fleet filters.

## Next steps

To run these on a schedule, see [Running unattended](running-unattended.md).
To understand what each run writes to disk, see
[Inspecting runs](reference.md#inspecting-runs).
