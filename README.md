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

**Phase 1A — foundations.** Scaffolding, intent layer (`this.i`), methodology
documentation, and CI are in place; no production code yet. Subcommands exist
in the CLI shell but exit with code 99 until later phases. See
[`docs/design-notes.md`](docs/design-notes.md) for the narrative phase plan
and [`this.i`](this.i) for the load-bearing design decisions and their
rationale.

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
