# Running unattended

gitbulk is built to run from cron alongside your ongoing development work. This
page covers the cron entry point, the exit codes that drive your inbox and your
shell prompt, and how to surface "needs attention" without email spam.

## The cron entry point

`bin/gitbulk-cron` is the recommended way to invoke gitbulk from cron. It:

- serializes overlapping invocations (a global advisory lock),
- captures full stdout/stderr to a timestamped log under
  `~/.cache/gitbulk/cron/`,
- maintains exit-code-aware symlinks (`last-failure.log`, `last-attention.log`,
  `last-audit.log`),
- prunes logs older than `$GITBULK_CRON_RETAIN_DAYS` (default 30).

The inner `gitbulk` exit code is preserved as the wrapper's own exit code. The
wrapper writes its status line to stdout — the thing cron mails — **only** on a
structural failure (exit 1 or an unexpected code). So `MAILTO` is a
failure-only channel: routine "PRs need attention" nights stay quiet on email
and surface through the [`ATTENTION` sentinel](#surfacing-attention-in-your-shell)
instead.

## A typical crontab

```cron
# MAILTO: a structural failure (exit 1 / unexpected) emails you here; quiet
# nights send nothing. Don't leave it UNSET — cron then mails the bare local
# user, which a real relay (e.g. msmtp -> Gmail) rejects as an invalid address.
# Set a real address, or MAILTO="" to disable mail entirely.
MAILTO=you@example.com

# Tell the wrapper where gitbulk lives only if it isn't on ~/.local/bin:
#   GITBULK_BIN=/home/you/venvs/gitbulk/bin/gitbulk
# Otherwise the wrapper finds a ~/.local/bin install via its default PATH.

# Read-only report at 03:00 on weekdays (Mon-Fri here; drop the `1-5` for every
# day). Safe alongside local work; exit 2/3 refreshes ~/.cache/gitbulk/ATTENTION
# for your shell prompt to flag.
0 3 * * 1-5 /home/you/code/gitbulk/bin/gitbulk-cron report

# Weekly Claude-assisted triage at 04:00 Mondays. Reads the latest report,
# sends it through Claude, writes a prioritized summary.md.
0 4 * * 1 /home/you/code/gitbulk/bin/gitbulk-cron summarize

# Weekly dispatch at 05:00 Saturdays. --apply is the explicit opt-in; without
# it the run is a dry-run that prints what it WOULD do. Create the prompt file
# first. Per-PR worktrees live under ~/.cache/gitbulk/worktrees/<runid>/ and
# are cleaned up automatically.
0 5 * * 6 /home/you/code/gitbulk/bin/gitbulk-cron dispatch --apply --prompt ~/.config/gitbulk/prompts/dispatch.md
```

## Sandboxing dispatched agents

`dispatch` runs a coding agent with auto-approval inside a worktree. By default
that is Claude Code; you can point it at another agent (Gemini, Copilot, Cursor,
or a custom CLI) via [`agents:` / `default_agent`](configuration.md#agents--default_agent--which-coding-agent-to-drive).
For unattended runs against untrusted PR content, prefer to confine non-Claude
agents:

- Set the profile's `sandbox: fs+no-net` (and an `env:` allowlist). gitbulk
  fetches the base and pushes the result itself, so the agent needs no network
  or credentials for conflict resolution and can run fully offline + isolated.
- The sandbox uses [bubblewrap](https://github.com/containers/bubblewrap): the
  host needs `bwrap` installed **and** unprivileged user namespaces enabled
  (true on most Linux incl. WSL2; some hardened distros disable them). gitbulk
  capability-probes at runtime.
- If a requested sandbox isn't available, `sandbox_fallback` decides: the
  default `refuse` makes gitbulk **skip/abort rather than run unconfined** —
  which in cron means a failed run you'll see, not a silent unsandboxed one. Set
  `warn-run` only if you accept running unsandboxed when bwrap is missing.

## Exit codes

The exit code drives both your inbox and the `ATTENTION` sentinel:

| Code | Meaning |
|---|---|
| 0 | Nothing to flag. |
| 1 | Structural failure (bad config, `gh` not authed, network, lock timeout). |
| 2 | At least one PR needs your attention — `ATTENTION` sentinel set. |
| 3 | At least one repo skipped by an invariant — `ATTENTION` sentinel set. |
| 4 | Run with `--skip-check` overrides applied (audit signal only). |
| 99 | Subcommand not yet implemented. |

Email is reserved for exit 1 / unexpected. Exit 2/3 would fire almost nightly
for a large fleet, so they don't email; they refresh the `ATTENTION` sentinel,
which your shell prompt can surface instead.

## Surfacing attention in your shell

Each run writes `~/.cache/gitbulk/ATTENTION` (JSON: exit code, run id, one-line
summary) when PRs need a look or a run failed. Add a prompt indicator that reads
it directly — no `gitbulk` process is spawned per prompt. For bash, in
`~/.bashrc`:

```bash
__gitbulk_attention() {
    local f="${XDG_CACHE_HOME:-$HOME/.cache}/gitbulk/ATTENTION"
    [ -r "$f" ] || return 0
    local code=""
    command -v jq >/dev/null 2>&1 && code=$(jq -r '.exit_code // empty' "$f" 2>/dev/null)
    if [ "$code" = "1" ]; then
        printf '%s' $'\001\e[1;31m\002✖ gitbulk\001\e[0m\002 '   # exit 1: red
    else
        printf '%s' $'\001\e[1;33m\002⚠ gitbulk\001\e[0m\002 '   # attention: yellow
    fi
}
# Prepend once (idempotent if this file is re-sourced).
case "$PS1" in *__gitbulk_attention*) ;; *) PS1='$(__gitbulk_attention)'"$PS1" ;; esac
```

A red `✖ gitbulk` means a structural failure (you were also emailed); a yellow
`⚠ gitbulk` means PRs need attention. Run `gitbulk show report` for the detail.

### Clearing the sentinel

The sentinel clears as soon as you've actually looked — you rarely need
[`gitbulk ack`](commands.md#ack):

- **`gitbulk show <sub>`** clears the sentinel when the run you're viewing is
  the one that raised it (it matches on subcommand + run id). Viewing a
  *different* subcommand's run leaves the alert in place, so a glance at `show
  report` never silently dismisses, say, a `dispatch` failure.
- **`gitbulk show`** with no argument (the dashboard) clears whatever alert is
  outstanding — the dashboard surfaces every subcommand's latest summary.
- **A clean run supersedes its own alert**: if last night's `report` flagged
  PRs and today's `report` comes back clean (exit 0), the stale alert clears
  itself. Only the *same* subcommand supersedes — a clean `report` won't clear
  a `merge` alert.
- **`gitbulk ack`** is the explicit catch-all: it clears any sentinel,
  including a legacy/corrupt one or a fallback alert with no recorded run id.

When `show` clears an alert it prints a one-line note to stderr (so it never
corrupts an artifact you're piping from stdout).
