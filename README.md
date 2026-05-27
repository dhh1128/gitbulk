# gitbulk

Nightly PR-triage tool for a developer who works across many GitHub repositories.

Given a list of repos you contribute to, `gitbulk` reports the state of your
open pull requests, flags ones that need your attention, can launch Claude Code
agents to fix common problems, can rebase PRs onto their default branches, can
auto-merge PRs that meet a configurable policy, and can close stale PRs.

It is designed to be safe to run from cron alongside ongoing development work
on the same local clones — it never touches your working tree or current branch.

## Status

Phase 0 scaffold. Subcommands exist but are not yet implemented; running one
exits with code 99 and a "not yet implemented" message. See the design plan
in conversation history for the phase roadmap.

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

## Local-git safety contract

`gitbulk` never modifies the working tree, index, or current branch of any
local clone under `~/code/`. Any operation that needs a checkout uses
`git worktree add` and cleans up afterward. Two copies of `gitbulk` may run
concurrently; a global advisory lock allows multiple read-only runs in parallel
but serializes mutating operations.

## See also

- `AGENTS.md` — non-negotiable rules for AI agents (and humans) modifying this repo.
