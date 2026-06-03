# Install

gitbulk ships two ways. Both require the
[GitHub CLI](https://cli.github.com/) (`gh`) and `git`, authenticated for your
account — gitbulk shells out to them for everything.

- **[As a single binary](#as-a-single-binary)** — recommended if you just want
  to *use* gitbulk.
- **[From source](#from-source)** — if you want to develop or contribute.

## As a single binary

Download the release asset with `gh` (this works even while the repo is
private, since `gh` is authenticated) and let it install itself onto your
`PATH`:

```bash
gh release download --repo dhh1128/gitbulk --pattern gitbulk --dir /tmp \
  && chmod +x /tmp/gitbulk \
  && /tmp/gitbulk install
```

`gitbulk install` copies the binary into `~/.local/bin` (the XDG user-bin
directory, and exactly where `bin/gitbulk-cron` looks), marks it executable,
and prints a shell-specific `PATH` hint if that directory isn't already on your
`PATH`. Pass `--dir <path>` to install elsewhere.

!!! note "If the one-liner can't run"
    See [`src/gitbulk/manual-install-instructions.md`](https://github.com/dhh1128/gitbulk/blob/main/src/gitbulk/manual-install-instructions.md)
    in the repo for a step-by-step fallback.

The binary is a self-contained zipapp; it needs only **Python 3.10+** on the
machine (PyYAML is vendored in). It is *not* truly standalone — it runs under
the system `python3`.

## Updating

```bash
gitbulk update            # download + verify (sha256) + atomically replace
gitbulk update --check    # just report whether a newer release exists
```

`update` never replaces the binary mid-command and never fires from cron: a
"newer version available" notice only appears on an interactive terminal, and
is suppressed by `--no-update-check` or `GITBULK_NO_UPDATE_CHECK=1` (which
`bin/gitbulk-cron` sets).

If you installed gitbulk with `pip`/`pipx` instead of the binary, `gitbulk
update` declines to clobber it and points you at `pip install -U gitbulk` /
`pipx upgrade gitbulk`.

## From source

For development and contributing:

```bash
git clone https://github.com/dhh1128/gitbulk
cd gitbulk
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
pytest
```

The [developer README](https://github.com/dhh1128/gitbulk#readme) has the full
contributor workflow, and [AGENTS.md](https://github.com/dhh1128/gitbulk/blob/main/AGENTS.md)
is the authoritative behavioral contract for anyone — human or AI — changing
the code.

## Next steps

Once gitbulk is on your `PATH`, [configure it](configuration.md) and then run
your first [`report`](commands.md#report).
