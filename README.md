# gitbulk

[![CI](https://github.com/dhh1128/gitbulk/actions/workflows/ci.yml/badge.svg)](https://github.com/dhh1128/gitbulk/actions/workflows/ci.yml)

Nightly fleet-maintenance tool for a developer who works across many GitHub
repositories.

Given a list of repos you contribute to, `gitbulk` reports the state of your
open pull requests, flags ones that need your attention, can launch Claude Code
agents to fix common problems, can rebase PRs onto their default branches, can
auto-merge PRs that meet a configurable policy, and can close stale PRs. It
also treats local clones themselves as first-class — discovering and cleaning
up post-merge cruft (orphaned worktrees, undeleted branches) and surfacing
repos that need work no PR yet exists for.

It is designed to be safe to run from cron alongside ongoing development work
on the same local clones — it never touches your working tree or current branch.

## Status

Read-only and read-then-act phases have landed; mutating operations
(`merge`, `rebase-onto-default`, `close-stale`) are still ahead. Implemented
today:

| Subcommand | What it does | Mutating? |
|---|---|---|
| `report` | Run the invariant chain against your open PRs and write a structured triage report (`summary.md` + `state.yaml`). | No |
| `summarize` | Feed a recent `report` run through Claude with a triage prompt to prioritize. | No |
| `dispatch` | Spawn headless Claude agents inside disposable worktrees against PRs matching a filter. Defaults to dry-run. | Yes (with `--apply`) |
| `show` | Print the latest run's artifacts for any subcommand, or the dashboard. | No |
| `ack` | Clear the `ATTENTION` sentinel after you've reviewed it. | No |
| `invariants` | List the invariant registry and which subcommands use each. | No |

`merge`, `rebase-onto-default`, `close-stale` are scaffolded in the CLI and
return exit code 99 until Phase 5 lands.

## Install (for development)

```bash
cd ~/code/gitbulk
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
pytest
```

## Configuration

Two files, by default at `~/.config/gitbulk/`:

- `repos.txt` — one `owner/repo` per line; `#` comments and blank lines ignored.
- `gitbulk.yaml` — policy and bot/human classification config.

Examples ship in `config/`.

## Inspecting runs

Every subcommand writes a per-run directory under
`~/.cache/gitbulk/runs/<timestamp>-<subcommand>/` containing
`summary.md`, `state.yaml`, `invariants.log`, `errors.log`, and
`manifest.yaml`. A `latest-<subcommand>` symlink points at the newest
run, and `dashboard.md` aggregates one excerpt per subcommand.

`gitbulk show` is the human-facing way to read them:

```bash
gitbulk show                       # dashboard (~/.cache/gitbulk/dashboard.md)
gitbulk show report                # latest report's summary.md
gitbulk show report --state        # state.yaml (structured PR records)
gitbulk show report --invariants   # invariants.log (JSONL audit trail)
gitbulk show report --errors       # errors.log (JSONL)
gitbulk show report --manifest     # manifest.yaml (argv, config snapshot)
gitbulk show report --path         # just the run-dir path (for scripting)
```

## Running from cron

`bin/gitbulk-cron` is the recommended cron entry point: it serializes overlapping
invocations, captures full stdout/stderr to a timestamped log under
`~/.cache/gitbulk/cron/`, maintains exit-code-aware symlinks
(`last-failure.log`, `last-attention.log`, `last-audit.log`), and prunes logs
older than `$GITBULK_CRON_RETAIN_DAYS` (default 30). The exit code from the
inner `gitbulk` invocation is preserved so `MAILTO` in your crontab still works.

Typical crontab:

```cron
# Set MAILTO so non-zero exits actually reach you (cron's default is silent).
MAILTO=you@example.com

# Tell the wrapper where gitbulk lives. Pick ONE of these per line, e.g.:
#   GITBULK_BIN=/home/you/venvs/gitbulk/bin/gitbulk
# or rely on ~/.local/bin via the wrapper's default PATH.

# Nightly read-only report at 03:00. Safe to run alongside any local work;
# exit 2 / 3 sets ~/.cache/gitbulk/ATTENTION so your shell prompt can flag it.
0 3 * * * /home/you/code/gitbulk/bin/gitbulk-cron report

# Weekly Claude-assisted triage at 04:00 Mondays. Reads the latest report,
# sends it through Claude, writes a prioritized summary.md.
0 4 * * 1 /home/you/code/gitbulk/bin/gitbulk-cron summarize

# Weekly dispatch at 05:00 Saturdays. --apply is the explicit opt-in; without
# it the run is a dry-run that prints what it WOULD do. Per-PR worktrees live
# under ~/.cache/gitbulk/worktrees/<runid>/ and are cleaned up automatically.
0 5 * * 6 /home/you/code/gitbulk/bin/gitbulk-cron dispatch --apply --prompt ~/.config/gitbulk/prompts/dispatch.md
```

Exit-code semantics that drive both your inbox and the `ATTENTION` sentinel
(this.i node `tp4kq2nr`):

| Code | Meaning |
|---|---|
| 0 | Nothing to flag. |
| 1 | Structural failure (bad config, gh not authed, network, lock timeout). |
| 2 | At least one PR needs your attention — `ATTENTION` sentinel set. |
| 3 | At least one repo skipped by an invariant — `ATTENTION` sentinel set. |
| 4 | Run with `--skip-check` overrides applied (audit signal only). |
| 99 | Subcommand not yet implemented. |

## Local-git safety contract

`gitbulk` never modifies the working tree, index, or current branch of any
local clone under `~/code/`. Any operation that needs a checkout uses
`git worktree add` and cleans up afterward. Two copies of `gitbulk` may run
concurrently; a global advisory lock allows multiple read-only runs in parallel
but serializes mutating operations.

## Development methodology

This repo follows a structured, intent-first methodology (see
[`docs/methodology.md`](docs/methodology.md)): every load-bearing design
decision is recorded in [`this.i`](this.i) as a node with an opaque base32 id
and a rebuttal-surface rationale, committed **before** the code commit it
justifies. Phase boundaries are explicit gates with named adversarial reviewer
roles. TDD is mandatory; 100% branch coverage on `src/gitbulk/` is enforced
in CI, with any gap requiring an approved `deviation:` node.

If you are an AI agent or human contributor, [`AGENTS.md`](AGENTS.md) is the
authoritative behavioral contract — read it before any change.

## See also

- [`AGENTS.md`](AGENTS.md) — non-negotiable rules for AI agents (and humans) modifying this repo.
- [`this.i`](this.i) — authoritative design-decision tree (intent layer).
- [`docs/methodology.md`](docs/methodology.md) — the development discipline this repo follows.
- [`docs/design-notes.md`](docs/design-notes.md) — narrative explainer for `this.i`; phase plan.
- [`docs/architecture.md`](docs/architecture.md) — high-level component and data-flow overview.

## License

TODO — to be decided before the first remote push.
