# gitbulk

[![CI](https://github.com/dhh1128/gitbulk/actions/workflows/ci.yml/badge.svg)](https://github.com/dhh1128/gitbulk/actions/workflows/ci.yml)
[![Docs](https://github.com/dhh1128/gitbulk/actions/workflows/deploy-docs.yml/badge.svg)](https://dhh1128.github.io/gitbulk/)

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

## 📖 Documentation

**Full user documentation lives at <https://dhh1128.github.io/gitbulk/>** —
install, configuration, the command reference, running from cron, and more.

Quick install (requires [`gh`](https://cli.github.com/) and `git`,
authenticated for your account):

```bash
gh release download --repo dhh1128/gitbulk --pattern gitbulk --dir /tmp \
  && chmod +x /tmp/gitbulk \
  && /tmp/gitbulk install
```

The rest of this README is for **contributors**. If you just want to *use*
gitbulk, head to the [documentation site](https://dhh1128.github.io/gitbulk/).

---

## Developing

```bash
git clone https://github.com/dhh1128/gitbulk
cd gitbulk
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
pytest
```

That last `pytest` should come back green at **100% branch coverage on
`src/gitbulk/`** — that gate is enforced in CI, and any gap requires an
approved `deviation:` node (see below).

### The rules

This repo follows a structured, intent-first methodology. Two documents are
load-bearing and must be read before you change anything:

- **[`AGENTS.md`](AGENTS.md)** — the non-negotiable behavioral contract for
  anyone, human or AI, modifying this repo. TDD is mandatory here
  (`read-run-change-run-commit`); the [local-git safety
  contract](https://dhh1128.github.io/gitbulk/reference/#local-git-safety-contract)
  is the most important rule.
- **[`this.i`](this.i)** — the authoritative design-decision tree. Every
  load-bearing decision is a node with an opaque base32 id and a
  rebuttal-surface rationale, committed **before** the code commit it
  justifies.

Supporting docs (also published under **Development** on the docs site):

- [`docs/methodology.md`](docs/methodology.md) — the development discipline.
- [`docs/architecture.md`](docs/architecture.md) — component and data-flow
  overview.
- [`docs/design-notes.md`](docs/design-notes.md) — narrative explainer for
  `this.i`; the phase plan.

### Editing the documentation site

The published site is built from [`docs/`](docs/) with
[Zensical](https://github.com/squidfunk/zensical) (the config is
[`zensical.toml`](zensical.toml)). Preview your changes locally:

```bash
uv run --group docs zensical serve     # live-reload preview
uv run --group docs zensical build     # one-off build into ./site
```

A push to `main` that touches `docs/` or `zensical.toml` redeploys the site via
[`.github/workflows/deploy-docs.yml`](.github/workflows/deploy-docs.yml).

## Releasing

Releases are cut by a maintainer (never an AI agent — pushes to `main` and
tags are reserved for humans). From a clean, in-sync `main`:

```bash
python scripts/release.py --patch   # 0.0.1 -> 0.0.2
python scripts/release.py --minor -m "new subcommand"
python scripts/release.py --major -m "rewrite"
```

The script verifies the tree is clean and in sync with `origin/main`, runs
the full test suite at the 100% branch-coverage gate, bumps the version in
`pyproject.toml` (the single source of truth — `__version__` is derived
from it), commits with sign-off, and pushes the tag. The tag triggers
[`.github/workflows/release.yml`](.github/workflows/release.yml), which
builds the single-file bundle, generates `update.json` (`latest_version` /
`script_url` / `sha256`), and publishes both as release assets — the
`gitbulk` asset is what the install one-liner downloads.

To build the artifact locally (e.g. to inspect it) without tagging:

```bash
gitbulk bundle ./dist/gitbulk
```

## See also

- [Documentation site](https://dhh1128.github.io/gitbulk/) — the user manual.
- [`AGENTS.md`](AGENTS.md) — non-negotiable rules for AI agents (and humans) modifying this repo.
- [`this.i`](this.i) — authoritative design-decision tree (intent layer).

## License

Licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE).
Copyright 2026 Daniel Hardman.
