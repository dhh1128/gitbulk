# gitbulk recover/undo subcommand: first-class restore of branches deleted by prune-branches --apply
kind: idea
tags: prune, recovery
created: 2026-06-07T04:29Z

- 2026-06-07T04:29Z Promote the recover-branch script (see [[7slq]]) into gitbulk itself, e.g. 'gitbulk recover-branch <slug> <branch>' / 'gitbulk undo prune-branches [--run <id>]'. All data needed already exists in run state: state.yaml branch rows carry sha + disposition (deleted/already-gone), and errors.log has the deleted-branch audit events with full SHA + PR. Subcommand would: locate the deletion in the latest (or named) run, re-POST the ref, and report. Consider a dry-run/confirm gate and a batch 'undo whole run' mode. Context: prune-branches gained a sacred-branch backstop in commits 80f04a8 (worktrees) + a9f23ae (branches); an audit of all 13 historical prune-branches runs (511 deletions) found ZERO main/master/default-branch deletions, so this is a safety-net feature, not a cleanup of past damage.
