# Resource-scoped locking

> **Authoritative source:** [`../../this.i`](../../this.i) decision node
> `rsclk7nq` (resource-scoped locking) is the binding record; this file is the
> narrative explainer. It resolves tension `rlkrcn3p` and supersedes the
> two-lock model of `lj5pqn4kr` and extends the locks API of `hk5pq3nm`.

## 1. The problem

gitbulk historically took a **single global advisory lock** (`run.lock`,
`fcntl.flock`) for the entire duration of every run — shared for read-only
subcommands, exclusive for mutating ones (`lj5pqn4kr`). That one lock was
silently doing three unrelated jobs at once:

1. serializing mutating runs against each other,
2. keeping the per-subcommand run-state (`latest-<sub>` symlink + run-dir GC)
   consistent for readers, and
3. masking writer-vs-writer races on the shared caches (org-members,
   default-branches) and the ATTENTION sentinel.

The user-visible symptom: `gitbulk show prune-worktrees` (read-only) **blocks**
for the entire multi-minute run of `gitbulk prune-branches` — even though the
two commands touch *disjoint* state. A `show` of one subcommand's output has no
data dependency on a different subcommand's mutation, yet the coarse global lock
serialized them anyway.

The principle this design applies: **lock the resource, not the operation.** A
lock protects a specific piece of shared state from concurrent corruption, and
its scope should be exactly that state — no wider. Contention should follow
data, not command identity.

## 2. Shared-resource inventory

A resource needs a lock only if two gitbulk processes can touch it
concurrently. **Not shared** (and therefore needing no lock): each run's own
`runs/<runid>-<sub>/` dir (the runid makes it unique); config files
(`repos.txt`, `gitbulk.yaml`) are read-only at runtime — the user edits them out
of band.

| # | Shared resource | Key | Writers | Readers | Race / failure if unlocked |
|---|---|---|---|---|---|
| 1 | Run-state surface of a subcommand: `latest-<sub>` symlink **+** `gc.prune_runs(<sub>)` deleting old run dirs | subcommand | any run of `<sub>` at `complete()` | `show <sub>`, dashboard | gc `rmtree`s a run dir while `show` reads it → ENOENT mid-read; two same-sub runs collide on `latest-<sub>.tmp`; double-prune `rmtree`s the same dir twice |
| 2 | org-members cache `org-members/<org>.yaml` | org | `refresh_cache`→`save_cache` (any gh-touching cmd) | classifier, `org.members.fresh` invariant | tmp-name collision crash (writers); lost update (benign — both write authoritative data) |
| 3 | default-branch cache `default-branches.yaml` (one file, all slugs) | singleton | `prime_default_branches` load→merge→save | same flow + gates | **lost update is real** (merge drops/keeps entries → a concurrent run can resurrect a deleted branch or lose a fetch); highest-probability tmp collision (every prefetch shares one tmp) |
| 4 | ATTENTION sentinel `ATTENTION` | singleton | `set_attention` (non-atomic!), `clear_*` (unlink) | `show`, `has_attention`, **external tmux** | torn read of half-written JSON; set-vs-clear is check-then-act |
| 5 | dashboard `dashboard.md` | singleton | dashboard render | external tmux | tmp collision (low frequency) |
| 6 | **Local git clone** `~/code/<repo>` (refs, `.git/worktrees/`, index) | slug | rebase-pr (worktree add, fetch, force-push), prune-worktrees (worktree remove, delete local branch), dispatch (worktree add) | prune-worktrees (`git worktree list`, unpushed count) | two gitbulk instances run git on same clone → index/ref-lock errors; prune-worktrees removes a worktree dispatch just added |
| 7 | **Remote repo state on GitHub** (merge / close / branch-delete / head-push) | slug | merge, close-stale, prune-branches, rebase-pr | — | two mutating runs act on the same repo's PRs/branches at once |
| 8 | `findings/<slug>/` | slug | dispatch | report (?) | per-slug; covered when folded into the repo lock |
| 9 | watchdog ack cache `watchdog-acked.yaml` (one file) | singleton | `record_ack` load→modify→save (non-atomic write) | report's recent-merges watchdog (`load_acked`) | torn read (non-atomic write); lost update (load-modify-save) |

Resources 6, 7, 8 all key on **slug** — unified into one **`repo:<slug>`** lock
meaning *"any gitbulk mutation to this repository, local clone or remote."* That
serializes the dangerous overlaps while still letting two instances work
*different* repos in parallel — the whole point of escaping the global lock.

## 3. Lock registry

Seven keyed locks replace the single `global_lock`. All reuse the existing
`_file_lock(path, mode, timeout, subcommand)` machinery (timeout, holder
metadata, pid-liveness — unchanged). Lock files live under `locks_dir()`.

| Constructor | Lock file | Modes | Protects |
|---|---|---|---|
| `run_state_lock(sub, mode)` | `runstate-<sub>.lock` | EX (writers), SH (`show`) | resource #1 |
| `repo_lock(slug, mode)` *(exists — now activated)* | `<slug>.lock` | EX (mutating), SH (read-only git) | resources #6/#7/#8 |
| `default_branches_lock()` | `default-branches.lock` | EX | resource #3 |
| `org_lock(org)` | `org-<org>.lock` | EX | resource #2 |
| `sentinel_lock()` | `attention.lock` | EX | resource #4 |
| `dashboard_lock()` | `dashboard.lock` | EX | resource #5 |
| `watchdog_ack_lock()` | `watchdog-acked.lock` | EX | resource #9 (around load→modify→save) |

**Governing rule:** any gitbulk git invocation against clone `<slug>` holds
`repo_lock(slug)` — SHARED for read-only git, EXCLUSIVE for mutating git; any
gitbulk remote mutation to repo `<slug>` holds `repo_lock(slug)` EXCLUSIVE. The
clone and the GitHub repo are treated as one resource keyed by slug.

`global_lock` has been retired — every command now takes only the resource
locks it needs (the `global_lock_file()` path helper remains only as a holder
placeholder in a few tests).

## 4. The refactor pattern

Every fleet handler today is `load → with global_lock(...): _run_under_lock(...)`.
The change: **delete the outer `global_lock`** and wrap the short critical
sections *inside* `_run_under_lock`, each acquired and **released before the
next**. The multi-minute gh-fetch and preflight phases run under **no** lock.

```
_run_under_lock:
  rs = RunState.begin(...)                          # no lock; runid is unique
  with org_lock(org):            ensure_org_members_fresh(...)
  with default_branches_lock():  prime_default_branches(...)
  <preflight invariants, gh fetch>                  # no lock
  for repo in passing_repos:                        # APPLY loop
      with repo_lock(repo.slug, "exclusive"):  <mutate that repo>
  with sentinel_lock():          sentinel.set_attention(...)
  with run_state_lock(sub, "exclusive"):  rs.complete(...)
```

## 5. Per-command critical-section spec

Read-only commands (`report`, `summarize`, `show`, `ack`, `invariants`) take no
`repo_lock`. The mutators take `repo_lock(slug)` exclusive per repo around their
apply work.

| Command | Locks (acquired in sequence, released before the next where possible) |
|---|---|
| `show <sub>` | `run_state_lock(sub, SH)` around the artifact read |
| `show` (dashboard) | `run_state_lock(sub, SH)` acquired→read→released **sequentially per sub**; then `sentinel_lock()` for clear-and-describe |
| `report` | `org_lock` (refresh) · `default_branches_lock` (prime) · `sentinel_lock` (set) · `run_state_lock("report", EX)` (complete) |
| `summarize` | `run_state_lock("report", SH)` spanning the `latest-report` resolve **and** its `state.yaml` read · `sentinel_lock` · `run_state_lock("summarize", EX)` |
| `merge` / `close-stale` / `prune-branches` | `org_lock` · `default_branches_lock` · `repo_lock(slug, EX)` per repo · `sentinel_lock` · `run_state_lock(sub, EX)` |
| `rebase-pr` / `dispatch` / `prune-worktrees` | `org_lock` · `default_branches_lock` · `repo_lock(slug, SH)` for clone preflight reads, `repo_lock(slug, EX)` per repo apply · `sentinel_lock` · `run_state_lock(sub, EX)` |
| `ack` | `sentinel_lock` |
| `invariants` | none (touches no shared state) |

`prune-worktrees` reads `git worktree list` *and* removes worktrees in one
per-repo loop — wrap the whole iteration in `repo_lock(slug, EX)` (read+write
are one unit).

`summarize` is the one spot where a lock is held across the
handler/`_run_under_lock` boundary: today the `latest-report` symlink is
resolved in the handler and its `state.yaml` read later under the lock, so the
`run_state_lock("report", SH)` must span both to close the resolve→read TOCTOU
against `gc.prune_runs("report")`.

## 6. Deadlock argument

The structure is **predominantly flat** — each `with` block closes before the
next opens, so in almost all paths at most one lock is held at a time (the
per-repo `repo_lock` is the only one taken inside a loop, and it is released
each iteration; `org`/`default_branches` are primed *before* the loop, so no
`repo_lock`-while-holding-cache-lock nesting occurs).

The **one sanctioned nesting** is `show <sub>`'s sentinel clear: it runs under
`sentinel_lock` while still holding `run_state_lock(sub, SH)`. That is
`run_state → sentinel`, which is the canonical order below, and nothing ever
acquires those two in the reverse order (the mutators take `run_state` at
`complete()` and `sentinel` at `set_attention` *separately*, never nested), so
no cycle can form. Deadlock is therefore still impossible by construction.

**Backstop** for any future code that *must* nest — one total acquisition order,
documented at the lock definitions:

```
org → default_branches → repo(slug) → run_state(sub) → sentinel → dashboard
```

## 7. Prerequisite hardening (Phase 0)

These remove the crash-races the global lock currently masks; fine-grained locks
expose more concurrency, so they ship first, independently of the lock model:

1. **Unique tmp names** in every atomic writer — `runstate._atomic_write_text` /
   `_atomic_write_symlink`, `org_members_cache.save_cache`,
   `default_branch_cache.save_cache`, `dashboard`. Switch the fixed
   `name + ".tmp"` to `tempfile.mkstemp(dir=path.parent)` (as `update.py`
   already does). Kills every writer-vs-writer tmp collision.
2. **Atomic `set_attention`** — tmp+`os.replace` instead of bare `write_text`,
   so external tmux readers never see a torn line. Same fix for
   `watchdog_ack.record_ack` (resource #9), whose `write_text` was also
   non-atomic. (The lost-update half of #9 waits for `watchdog_ack_lock` in
   Phase 2.)
3. **runid uniquifier** — `RunState.begin` does `mkdir(exist_ok=False)` on a
   second-resolution runid; two same-subcommand runs in the same second crash.
   Append a short pid/counter suffix, or retry on `FileExistsError`. (A lock
   cannot fix this — both processes compute the same runid.)

## 8. Deliberate consequences / open points

- **Two `merge --apply` runs can now overlap** (user-approved 2026-06-03):
  `repo_lock(slug)` guarantees they never touch the *same* repo at once, but
  they interleave across *different* repos. The nightly cron + a manual run is
  the realistic case; it is safe. This is the deliberate departure from the old
  "one mutating gitbulk at a time" guarantee.
- **`org_lock` is an optimization, not correctness** — the lost update is benign
  (both write authoritative membership); the lock just dedups redundant GitHub
  fetches. Lower priority than the rest.
- **`prime_default_branches` lost update is real** — its `default_branches_lock`
  is mandatory.
- **`repo_lock` does not exclude the user's own git** in `~/code/<repo>`. It
  only serializes gitbulk-vs-gitbulk; that is the existing reality.

## 9. Rollout

- **Phase 0** — §7 hardening (unique tmp names, atomic sentinel, runid fix).
  Independent of the lock model; removes the crash-races. Ship first.
- **Phase 1** — switch `show` and `summarize` off `global_lock` onto
  `run_state_lock`. Smallest change; directly fixes the reported symptom.
- **Phase 2** — activate `repo_lock` and add the cache/org/sentinel locks in the
  six mutators; retire `global_lock` last, command by command.

## 10. Test plan

- Unit: each new constructor (path, mode mapping) — extend `test_locks.py`.
- Concurrency regression for the reported bug: a process holding
  `run_state_lock("prune-branches")` must not block `show prune-worktrees`
  (different key).
- Writer-vs-writer: two `save_cache` racing produce no `ENOENT` (Phase-0 unique
  tmp).
- Two same-second `RunState.begin("merge")` both succeed (runid fix).
- `repo_lock` mutual exclusion: two `prune_worktrees` on the same slug
  serialize; on different slugs run concurrently.

All inter-process contention tests use subprocess spawns (per `hk5pq3nm.h` —
`fcntl.flock` is per-process; threads in one process all see the lock as held).
