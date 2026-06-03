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
matching `defaults.` value; list fields (`skip_checks`, `extra_checks`) are
**appended** to the defaults rather than replacing them.

```yaml
repos:
  provenant-dev/origin-experimental-svc:
    merge_policy: ci-only
    min_business_days: 1
  provenant-dev/origin-platform:
    unresolved_burden: other   # acting as maintainer here, not contributor
```

See [`config/gitbulk.yaml.example`](https://github.com/dhh1128/gitbulk/blob/main/config/gitbulk.yaml.example)
for the complete annotated reference, including the disposable
`worktree_root`.

## Next steps

With both files in place, run [`gitbulk report`](commands.md#report) to see
your fleet's PR state, then read it back with [`gitbulk show`](commands.md#show).
