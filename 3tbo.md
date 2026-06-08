# rebase-pr force-push and merge --delete-branch do not gate on fork/cross-repo PRs
kind: debt
tags: safety, fork
created: 2026-06-08T19:34Z

- 2026-06-08T19:34Z Severity MEDIUM. Source: review-panel-2026-05-29 ARC-F1 (+ARC-F4). Verified outstanding 2026-06-08. PRInfo.head_repo_slug EXISTS (pr_info.py:139) and is populated on the REST path (gh.py:1862) used by prune commands, but the GraphQL path _pr_info_from_graphql_node (gh.py:2055-2076) does NOT set it and the query (gh.py:1044-1045) does not request isCrossRepository. rebase_pr.py:249 calls force_push_with_lease(worktree, pr.head_ref, pr.head_sha) UNCONDITIONALLY — a user's own fork PR has its head branch on the fork, not origin, so the lease push targets the wrong ref. ARC-F4: merge.py passes delete_branch=True unconditionally. Fix: populate head_repo_slug on the GraphQL path + request isCrossRepository, then skip/refuse cross-repo PRs in rebase-pr and fork-aware delete in merge.
- 2026-06-08T20:45Z Fixed: PRInfo gains is_cross_repository (from GraphQL isCrossRepository) + head_repo_slug (from headRepository.nameWithOwner); query requests both. New PER_PR invariant pr.head_on_origin SKIPS cross-repo PRs in rebase-pr (head branch is on the fork, not origin) -> never force-pushed. merge passes delete_branch=not pr.is_cross_repository (fork PRs merge, fork branch left intact). Design node this.i frkrep5q. 100% cov.
