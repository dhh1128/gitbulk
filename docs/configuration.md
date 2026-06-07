# Configuration

gitbulk reads two files, by default from `~/.config/gitbulk/`:

| File | Purpose |
|---|---|
| `repos.txt` | The fleet — one repo per line. |
| `gitbulk.yaml` | Policy (merge/stale rules) and bot/human classification. |

Annotated examples ship in the repo's [`config/`](https://github.com/dhh1128/gitbulk/tree/main/config)
directory (`repos.txt.example`, `gitbulk.yaml.example`). Copy them into place
and edit:

```bash
mkdir -p ~/.config/gitbulk
cp config/repos.txt.example    ~/.config/gitbulk/repos.txt
cp config/gitbulk.yaml.example ~/.config/gitbulk/gitbulk.yaml
```

## `repos.txt` — the fleet

One repo per line; blank lines and `#` comments (including inline) are ignored.
Three forms can be mixed freely:

1. **Canonical slug** — `owner/repo`. The local clone is assumed at
   `<code-root>/<repo-name>` (default code-root `~/code/`, override with
   `--code-root PATH`).
2. **GitHub URL** — HTTPS or SSH; gitbulk parses the slug from it.
3. **Local path** — gitbulk runs `git -C <path> remote get-url origin` to
   discover the slug *and* pins the clone location to that path. Use this when
   your clones aren't organized by basename.

```text
# Provenant
provenant-dev/origin-platform
https://github.com/provenant-dev/origin-agent-svc
~/code/work/special/nested-clone
```

Entries that can't be canonicalized to `owner/repo` (non-github.com hosts,
missing directories, repos without an `origin` remote) fail with a friendly
error. Duplicate slugs: first wins, the rest are warned and skipped.

## `gitbulk.yaml` — policy and classification

Every key is optional; missing keys fall back to documented defaults. **Unknown
keys are rejected loudly** — a typo like `min_buisness_days` fails validation
rather than silently doing nothing.

### `defaults` — merge and stale policy

The policy applied to every repo unless overridden. The most important keys:

| Key | Default | Meaning |
|---|---|---|
| `merge_policy` | `strict` | `strict` \| `ci-only` \| `never` — how strict the merge gate is. |
| `merge_method` | `rebase` | `rebase` \| `merge` \| `squash`, passed to `gh pr merge`. |
| `min_business_days` | `3` | Business days (Mon–Fri, local TZ) a PR must be "ready" before merge. |
| `unresolved_burden` | `me` | `me` \| `other` \| `either` — who must mark review threads resolved. |
| `bot_threads_block` | `true` | Whether unresolved *bot* threads also block "ready". |
| `stale_age_days` | `90` | `close-stale` inactivity threshold. |
| `stale_cooloff_days` | `7` | Minimum time since the warning before `close-stale` closes. |
| `stale_policy` | `warn-and-close` | `warn-and-close` \| `warn-only` \| `never`. |

### `humans` and `bots` — who counts as a reviewer

gitbulk needs to tell human reviewers from bots when it decides whether a PR is
"ready". It enumerates your org's members via `gh api orgs/<org>/members`
(cached for `cache_ttl_hours`, default 7 days), and treats the logins listed
under `bots` as non-human. Use `humans.exceptions` for org members that are
actually bots, and `humans.always_human` for humans outside the org.

### `repos` — per-repo overrides

Keys are `owner/repo` strings from `repos.txt`. Scalar fields override the
matching `defaults.` value; list fields (`skip_checks`, `extra_checks`,
`sacred_branches`) are **appended** to the defaults rather than replacing them.

```yaml
repos:
  provenant-dev/origin-experimental-svc:
    merge_policy: ci-only
    min_business_days: 1
  provenant-dev/origin-platform:
    unresolved_burden: other   # acting as maintainer here, not contributor
    sacred_branches: [release/prod]   # never let prune-worktrees sweep this one
```

### `sacred_branches` — branches the prune commands must never delete

Both prune commands already refuse to delete a branch named `main`/`master` or
one matching a repo's GitHub **default branch** (`prune-branches` also honours
GitHub branch protection). Set `defaults.sacred_branches` (or a per-repo
override) to extend that always-protected set with your own conventions —
`develop`, `trunk`, `release`, integration branches, etc.

The same set applies to **both** `prune-worktrees` (local branch/worktree
removal) and `prune-branches` (remote branch deletion): a name you protect from
local deletion is equally protected from remote deletion. Matching is exact and
case-sensitive, and the configured names are *unioned* with the built-in
protections, so this can only ever keep more branches.

```yaml
defaults:
  sacred_branches: [develop, trunk]
```

See [`config/gitbulk.yaml.example`](https://github.com/dhh1128/gitbulk/blob/main/config/gitbulk.yaml.example)
for the complete annotated reference, including the disposable
`worktree_root`.

### `agents` / `default_agent` — which coding agent to drive

`dispatch` and `summarize` shell out to a CLI coding agent. By default that is
Claude Code (the `claude` preset), and **if you set nothing here, behavior is
identical to before this feature existed.** You can point gitbulk at a different
agent with one line, or define a fully custom one. The full design and security
model live in [`pluggable-agents.md`](pluggable-agents.md).

```yaml
default_agent: gemini            # built-in presets: claude | gemini | copilot | cursor

agents:
  gemini:
    model: gemini-2.5-pro        # override just one field of a preset
  myagent:                       # a fully custom backend
    command: [mytool, run, "{prompt}"]   # argv LIST (never a shell string)
    model_args: [--model, "{model}"]     # appended only when a model is set
    prompt_via: arg              # arg | stdin
    env: [MYTOOL_API_KEY]        # allowlist — see below
    sandbox: fs+no-net           # none | fs-only | fs+no-net

repos:
  someorg/some-repo:
    agent: copilot               # per-repo override
```

Selection order: `--agent` flag → per-repo `agent:` → `default_agent` →
`claude`.

Security-relevant fields:

- **`command` is an argv list, never a shell string** (a string is rejected).
  `{prompt}`/`{model}` substitute *within a single token*, so prompt text can't
  inject arguments, and the binary is pinned via `which` at load.
- **`env`** is an allowlist: only the named variables (plus a minimal safe base —
  `PATH`, `HOME`, locale) are passed to the agent. Omit it to inherit the full
  environment (the backward-compatible default, which hands the agent your
  `GH_TOKEN`/SSH/cloud creds — prefer an allowlist for non-Claude agents).
- **`sandbox`** runs the agent in an unprivileged [bubblewrap](https://github.com/containers/bubblewrap)
  namespace. `fs+no-net` is the tightest (no network; `~/.ssh`/`~/.aws`/other
  clones not mounted) and is appropriate for conflict-resolution, which gitbulk
  arranges to need no network or credentials. Requires `bwrap` + unprivileged
  user namespaces; see [running unattended](running-unattended.md). When a
  requested sandbox isn't available, the top-level `sandbox_fallback`
  (`refuse` default, or `warn-run`) decides whether gitbulk refuses to run or
  runs unsandboxed with a warning.

## Next steps

With both files in place, run [`gitbulk report`](commands.md#report) to see
your fleet's PR state, then read it back with [`gitbulk show`](commands.md#show).
