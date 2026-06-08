# Reference

Details you'll reach for occasionally: where runs are stored and how to read
them, how color output behaves, and the safety guarantee that makes gitbulk
safe to run unattended.

## Inspecting runs

Every subcommand writes a per-run directory under
`~/.cache/gitbulk/runs/<timestamp>-<subcommand>/` containing:

| File | Contents |
|---|---|
| `summary.md` | Human-readable summary of the run. |
| `state.yaml` | Structured records (e.g. PR state). |
| `invariants.log` | JSONL audit trail of every invariant evaluated. |
| `errors.log` | JSONL error records. |
| `manifest.yaml` | The `argv`, a config snapshot, and the acting GitHub identity (`actor`) for the run. |

A `latest-<subcommand>` symlink points at the newest run of each subcommand,
and `dashboard.md` aggregates one excerpt per subcommand.

[`gitbulk show`](commands.md#show) is the human-facing way to read them:

```bash
gitbulk show                       # dashboard (~/.cache/gitbulk/dashboard.md)
gitbulk show report                # latest report's summary.md
gitbulk show report --state        # state.yaml (structured PR records)
gitbulk show report --invariants   # invariants.log (JSONL audit trail)
gitbulk show report --errors       # errors.log (JSONL)
gitbulk show report --manifest     # manifest.yaml (argv, config snapshot)
gitbulk show report --path         # just the run-dir path (for scripting)
```

## Output and color

gitbulk colorizes its summary and error lines with a semantic outcome marker —
green `✓` for a clean run, yellow `⚠` when something needs attention, red `✗` /
red text for errors. Color is purely emphasis: it is auto-suppressed whenever
it would be noise or corruption, so **piped and redirected output is
byte-identical to a no-color run** and safe to parse.

Resolution is per stream (stdout and stderr independently), in this precedence:

| Signal | Effect |
| --- | --- |
| `NO_COLOR` set (any value) | force **off** — the [no-color.org](https://no-color.org) standard; wins over `FORCE_COLOR` |
| `FORCE_COLOR` / `CLICOLOR_FORCE` set | force **on**, even when piped (e.g. `… \| less -R`) |
| `TERM=dumb` | off |
| stream is a TTY | on, else off |

There is no `--color` flag; the environment variables cover the cases:

```bash
NO_COLOR=1 gitbulk report                          # disable color
FORCE_COLOR=1 gitbulk report | tee run.log         # keep color through a pipe
```

Status glyphs fall back to ASCII (`[ok] [!] [x]`) on terminals whose encoding
can't render Unicode.

## Local-git safety contract

This is the guarantee that makes gitbulk safe to run from cron while you're
working in the same clones:

!!! warning "gitbulk never modifies your working state"
    `gitbulk` never modifies the working tree, index, or current branch of any
    local clone under `~/code/`. Any operation that needs a checkout uses `git
    worktree add` into a disposable path and cleans up afterward.

Two copies of gitbulk may run concurrently: a global advisory lock allows
multiple read-only runs in parallel but serializes mutating operations.

The one blessed exception is [`prune-worktrees`](commands.md#prune-worktrees),
which may run `git worktree remove` on a *linked* worktree (never the primary
clone you edit) and `git branch -d` on a fully-merged branch. It never touches
the working tree, index, `HEAD`, or current branch of the primary clone. The
full contract is spelled out in
[AGENTS.md](https://github.com/dhh1128/gitbulk/blob/main/AGENTS.md) and the
[architecture overview](architecture.md).
