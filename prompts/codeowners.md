# Ensure CODEOWNERS reflects active maintainers

You are running **headless, non-interactively**, inside a disposable git
worktree that gitbulk created for you on a fresh branch off the repository's
default branch. Your job is to make this repository have a correct
`CODEOWNERS` file, committing your change **locally only**. Do **not** push and
do **not** open a pull request — gitbulk does that after you exit (it inspects
your commit, pushes the branch, and creates the PR under `--apply`).

## Definition of "correct"

`CODEOWNERS` must assign a global owner line listing **exactly** the set of
GitHub users who BOTH:

1. have **direct push access** to this repository, AND
2. have **authored at least one commit in the last 60 days**.

## Steps

1. Determine the repo slug: `git remote get-url origin` → parse `owner/repo`.
2. **Push-rights set** — list collaborators with push (or higher):
   ```
   gh api --paginate "repos/<owner>/<repo>/collaborators" \
     --jq '.[] | select(.permissions.push == true) | .login'
   ```
3. **Recent-committers set** — GitHub-resolved logins of commit authors in the
   last 60 days (use the API so you get logins, not raw git emails):
   ```
   gh api --paginate "repos/<owner>/<repo>/commits" -f since="$(date -u -d '60 days ago' +%Y-%m-%dT%H:%M:%SZ)" \
     --jq '.[].author.login' | sort -u
   ```
   Ignore null/empty logins (unattributed commits).
4. **Owners** = the sorted, de-duplicated intersection of (2) and (3). If the
   intersection is empty, leave the repo unchanged and report why (do not write
   an ownerless file).
5. Target file: `.github/CODEOWNERS` (GitHub also honors root and `docs/`; if a
   `CODEOWNERS` already exists in any of those locations, update that one
   in place rather than creating a second).
6. Desired content is a single global rule:
   ```
   # Managed by gitbulk dispatch-repo (codeowners). Owners = push-access users
   # with a commit in the last 60 days. Edit via gitbulk, not by hand.
   *    @owner1 @owner2 ...
   ```
   (logins sorted, each prefixed with `@`).
7. **Decide**:
   - If no CODEOWNERS exists anywhere → create `.github/CODEOWNERS`.
   - If one exists but its global-rule owner set is **missing any** login in
     the desired set → update it to the desired global rule (preserve any
     unrelated path-specific rules below the global line).
   - If it already lists every desired owner on the global rule → **make no
     change**.
8. If you changed a file, stage and commit **locally** with:
   ```
   git add -A && git commit -m "chore: update CODEOWNERS to active maintainers"
   ```
   Do not amend, rebase, push, or create a PR.

## Output

Print a short, machine-readable summary as your final line, one of:
- `CODEOWNERS: no change needed (owners already current: @a @b)`
- `CODEOWNERS: committed (owners: @a @b)`
- `CODEOWNERS: skipped (empty owner intersection)`

Keep all reasoning brief; this runs unattended.
