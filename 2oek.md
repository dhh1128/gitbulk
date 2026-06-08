# rebase-pr passes PR base_ref/head_ref/head_sha to git as positional args with no -- terminator or validation (arg-injection/RCE)
kind: debt
tags: security, rce
created: 2026-06-08T19:33Z

- 2026-06-08T19:34Z Severity HIGH. Source: review-panel-2026-05-29 SEC-F1 (+LOW SEC-F2). Verified outstanding on main 2026-06-08. rebase.py:115 _git(...,'fetch','origin',base_ref), :122 'rebase' f'origin/{base_ref}', :215 f'--force-with-lease={head_ref}:{expected_sha}', :217 f'HEAD:{head_ref}' — all positional, no '--' terminator. base_ref/head_ref/head_sha come from gh GraphQL (pr_info.py:139 / gh.py _pr_info_from_graphql_node) with NO validation. A '-'-leading ref becomes a git option (e.g. --upload-pack=<cmd>) -> RCE under cron. FOLDS SEC-F2: gh.py fetch_check_runs also interpolates an unvalidated sha into the REST path. Fix: insert '--' before positional ref/sha args in rebase.py/worktree.py AND validate refs (reject leading '-') + sha (^[0-9a-f]{7,40}$) at the gh boundary.
