# gitbulk

**gitbulk** is a nightly fleet-maintenance tool for a developer who works
across many GitHub repositories.

Give it a list of repos you contribute to, and `gitbulk` reports the state of
your open pull requests, flags the ones that need your attention, and — when
you ask it to — launches Claude Code agents to fix common problems, rebases PRs
onto their default branches, auto-merges PRs that meet a policy you configure,
and closes stale PRs. It also treats your local clones as first-class:
discovering and cleaning up post-merge cruft (orphaned worktrees, undeleted
branches) and surfacing repos that need work no PR yet exists for.

!!! tip "Safe to run from cron"
    gitbulk is designed to run unattended alongside your ongoing development
    work on the same clones. It **never touches your working tree, index, or
    current branch** — every operation that needs a checkout uses a disposable
    worktree. See the [local-git safety contract](reference.md#local-git-safety-contract).

## Get started

- **[Install](install.md)** — grab the single binary (or install from source)
  and put it on your `PATH`.
- **[Configure](configuration.md)** — point gitbulk at your repos and set your
  merge/triage policy.
- **[Commands](commands.md)** — the full subcommand reference: `report`,
  `merge`, `dispatch`, `prune-branches`, and the rest.
- **[Running unattended](running-unattended.md)** — wire it into cron and
  surface attention in your shell prompt.

## How a typical day looks

1. **Overnight**, cron runs `gitbulk report` against your fleet and writes a
   structured triage report. If anything needs you, it refreshes an
   `ATTENTION` sentinel.
2. **In the morning**, your shell prompt shows a quiet `⚠ gitbulk` marker.
   You run [`gitbulk show`](commands.md#show) to read the dashboard.
3. **On a schedule you choose**, gitbulk can `merge` PRs that satisfy your
   policy, `rebase-pr` the ones that fell behind, `dispatch` AI agents at
   common fixes, and `prune-branches` / `prune-worktrees` to clean up after
   merged work.

Every run is recorded under `~/.cache/gitbulk/runs/`, so you can always go
back and read exactly what happened — see [Inspecting runs](reference.md#inspecting-runs).

## Requirements

gitbulk shells out to the [GitHub CLI](https://cli.github.com/) (`gh`) and
`git` for everything, so both must be installed and `gh` must be authenticated
for your account. The binary needs only Python 3.10+ on the machine.

---

Looking to contribute? The [developer README](https://github.com/dhh1128/gitbulk#readme)
covers cloning, running the tests, and the project's intent-first
[methodology](methodology.md).
