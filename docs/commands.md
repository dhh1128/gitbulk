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
| [`recover-branch`](#recover-branch) | Restore a branch that `prune-branches` deleted, from that run's audit log. | Yes (`--apply`) |
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
Fork (cross-repo) PRs are **skipped** — their head branch lives on the fork,
not on the repo gitbulk pushes to — so gitbulk only rebases PRs whose head
branch is on `origin`.

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
      overridable). Just-merged work is left alone. Pass `--min-age-days DAYS`
      to override the default for one run (e.g. `--min-age-days 2` to sweep work
      merged 2+ days ago instead of 7+); `0` removes the grace entirely. It
      rewrites the *default* only — a repo with its own `prune_min_age_days`
      override still uses that.
    - **No data loss** — a remote branch is deleted only when its tip is the
      merged PR's recorded head SHA *or* it is fully contained in the default
      branch; a worktree's branch is removed only when it has no unpushed
      commits. Anything with unique work is kept, with a reason.
    - **Sacred branch names** — neither command will ever delete a branch named
      `main`/`master`, one matching a repo's GitHub default branch, or any name
      in [`sacred_branches`](configuration.md#sacred_branches--branches-the-prune-commands-must-never-delete).
      The same set guards local *and* remote deletion.

### `prune-branches`

Deletes remote branches whose only PRs are merged or closed. It **never
deletes** the default branch, a protected branch, a [sacred-named
branch](configuration.md#sacred_branches--branches-the-prune-commands-must-never-delete)
(`main`/`master` or a configured name), the head of an open PR, or the base of
an open PR (the stacked-PR case), and never touches fork branches.
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

#### No-PR safe states

A worktree's branch need not have a closed PR to be prunable — there are three
additional states where removing it is safe. Each still respects the grace
period and the no-data-loss rule:

- **Empty worktree behind its local base** — the branch has *no commits of its
  own* relative to the **local** branch it was created from (its local default
  branch) and that base has since moved ahead, and the worktree has sat
  untouched (by the worktree's HEAD reflog — when it was last created, checked
  out, or committed onto) past the grace period. A created-but-abandoned scratch
  tree the world moved past. A *fresh* empty worktree (base hasn't moved) is
  kept, so this never reaps a worktree you just made.
- **All commits already on a remote** — every commit on the branch is present
  on some remote-tracking branch, so removing the local worktree loses nothing
  (you can re-create it from the remote). Stale-gated on the ref's reflog age
  (when it last moved locally, not the tip commit's date) to avoid reaping
  freshly-created or freshly-pushed work.
- **Merged into the local default branch** *(opt-in: `--trust-local-default`)* —
  the branch was merged into your local default branch directly instead of
  through a PR, and its commits may live **only** locally. Off by default
  because a later reset of your local default could orphan those commits; when
  enabled, the branch is force-deleted only after re-verifying it is still
  contained in the local default.

A clean `--apply` run is quiet (no `ATTENTION`); only failures and skipped
repos surface. Both accept the `--org`/`--repo`/`--filter` fleet filters.

### `recover-branch`

Restores a branch that `prune-branches --apply` deleted. It reads the deleting
run's audit trail — every deleted branch leaves a row in that run's
`state.yaml` carrying the tip SHA recorded just before deletion — and
re-creates the ref through the GitHub ref API. Recovery is reliable because
`prune-branches` only ever deletes a branch whose tip is either a merged PR's
head (pinned forever by `refs/pull/N/head`) or already contained in the default
branch, so the recorded SHA is never garbage-collected.

Scope is positional, on the one command:

- `gitbulk recover-branch` — restore **every** branch the latest
  `prune-branches` run deleted.
- `gitbulk recover-branch owner/repo` — restore only that repo's deletions.
- `gitbulk recover-branch owner/repo my-branch` — restore one branch.
- `--run <runid>` reads a specific prune-branches run instead of the latest
  (find ids with `gitbulk show prune-branches --path`).

Like the other mutating commands it **defaults to dry-run**; pass `--apply` to
actually create the refs. A branch that already exists is reported and left
untouched (never overwritten, even at a different SHA), so re-running is safe.

## Next steps

To run these on a schedule, see [Running unattended](running-unattended.md).
To understand what each run writes to disk, see
[Inspecting runs](reference.md#inspecting-runs).
