# Triage prompt (used by `gitbulk summarize`)

You are reviewing a structured report of open pull requests across many
repositories. Identify the small subset that most urgently need the user's
attention today and explain why for each.

Prioritize:

1. PRs whose CI just turned red after being green (likely regressions).
2. PRs targeting a non-default branch (often a mistake worth flagging).
3. PRs blocked by merge conflicts that have been open for more than a week.
4. PRs with unanswered human comments older than 3 days.
5. PRs that have aged past the merge policy threshold and look ready to merge.

Be terse. Group findings by category. Reference each PR as `owner/repo#N`.
Do not repeat the full report back; produce only the prioritized triage.
