# recover-branch helper script: recreate a deleted remote branch from the prune-branches audit log
kind: todo
tags: prune, recovery
created: 2026-06-07T04:29Z

- 2026-06-07T04:29Z Standalone script (suggested ~/code/devenv/gitbulk-recover-branch). Reads the durable audit trail in ~/.cache/gitbulk/runs/*-prune-branches/: errors.log JSONL events with context.action=='deleted-branch' carry {slug, branch, sha (full), pr}. For a given slug+branch (or a whole run), emit/run: gh api -X POST repos/<slug>/git/refs -f ref=refs/heads/<branch> -f sha=<sha>. Recovery is robust because prune-branches' data-loss guard only deletes branches whose tip is the merged PR head SHA (pinned forever by refs/pull/N/head) or is contained in the default branch (reachable from history -> never GC'd). Verified live 2026-06-06: a deleted ui-kit branch's commit was still fetchable by SHA. Also list the GitHub 'Restore branch' button (per-PR) and 'git fetch origin refs/pull/N/head' as fallbacks.
