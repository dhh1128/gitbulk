# Incremental, parallel, plan-based `prune-branches`

> **Status:** IMPLEMENTED 2026-06-04 on branch `feat/prune-branches-incremental`
> across four commits (P1 parallel scan → P4 re-validation). Binding record:
> `this.i` nodes `prnpl3kq`/`prnsc7nr`/`prnsh5kp`/`prnpf8nq`/`prnrv6kq` and
> tension `prntn9kp` under `prnbr4kq`. This file is the narrative explainer.
> Touches the run-state surface governed by `rsclk7nq` (resource-scoped
> locking) and the data-loss guard `prdls2nq`.
>
> **Where the build refined this design (read before trusting the prose below):**
> - The runstate envelope `SCHEMA_VERSION` was **not** bumped to 2 — only this
>   subcommand's payload changed, so the plan version lives in the
>   `prune_plan.version` extra (2) instead. §3's `schema_version: 2` is the
>   one inaccuracy.
> - Per-branch SHA reuse (§5, `prnsh5kp`) is realized by storing **all**
>   deep-classified branches in the plan — including the unsurfaced "no closed
>   PR" skips — so a stale rescan cache-hits the bulk of branches, not just
>   delete verdicts. The summary still filters to PR-citing rows, so the report
>   is unchanged. `--force-scan` / `--max-age 0` disable SHA reuse too.
> - P4 re-validation (§7.1) additionally does a **fresh open-PR fetch** per
>   candidate repo (cached) to catch the "used again" case; an unverifiable tip
>   *or* open-PR fetch biases to `refused`.
> - Freshness (§6) is evaluated **per repo** for both dry-run and apply; a
>   second run within the window reuses the whole repo with zero network.

---

## 1. The problem

Two distinct pains, one root cause.

### 1.1 The scan is slow (15–30 min on a ~150-repo fleet)

The scan loop (`commands/prune_branches.py:370-400`) is **sequential** and its
cost is **per branch**, not per repo:

| Call | Where | Frequency | Cost |
|---|---|---|---|
| `default_branch` | `gh.py:1136` | per repo, **cached** by the GraphQL prefetch (`prune_branches.py:335`) | ~0 |
| `list_branches` | `gh.py:1668` | 1 / repo (paginated) | cheap |
| `my_open_prs` | `gh.py:1339` | 1 / repo (search) | cheap |
| **`closed_prs_for_head`** | `gh.py:1702` | **1 / branch** that clears the cheap guards | **dominant** |
| `branch_ahead_by` | `gh.py:1758` | 1 / merged-but-moved or non-merged-closed branch | secondary |

Every `gh api` is a fresh subprocess (~0.3–0.5 s spawn) plus a network
round-trip. ~150 repos × ~10 branches ⇒ **1,000–2,000 serial subprocess+network
calls**, almost all of them `closed_prs_for_head`. That is the 15–30 min.
Everything else combined is a rounding error.

### 1.2 The report "resets" after a partial `--apply`

Every invocation — dry-run *or* apply — calls `RunState.begin`
(`prune_branches.py:288`), mints a **new** run dir, and on completion repoints
the `latest-prune-branches` symlink (`runstate.py:217`). `show prune-branches`
reads whatever that symlink points at.

There is no "apply a subset of a prior report" path today. To apply to a subset
you pass a repo filter, which makes `--apply` **re-scan only that subset**
(`passing_repos` shrinks → `state.yaml` holds only the subset → the symlink now
points at the narrow run). The broad dry-run report isn't deleted, but `show` is
shadowed by the narrow apply. So you re-run the 30-min scan to see what's left.

### 1.3 Root cause

The expensive **analysis** and the cheap **action** are fused into one
non-reusable run. Every idea below is a facet of *separating the analysis (a
reusable, accumulating plan) from the action (apply)*, and making both fast and
safe.

---

## 2. Decisions (resolved with the maintainer, 2026-06-04)

| # | Fork | Decision |
|---|---|---|
| D1 | How does `--apply` get its work list? | **Reuse the latest plan**, but only entries that are **fresh** (within `--max-age`) **and in scope**. Anything the plan doesn't cover — missing repos, or stale entries — is **auto re-scanned** as part of the apply (the apply can therefore take a while if the plan is stale/absent; that's accepted). |
| D2 | Subset granularity for apply | **Repo/fleet filters only** (the existing `--repo`/`--org`/fleet selectors). No per-branch selection UI. Multiple filtered applies **accumulate** in one report. |
| D3 | Where the plan lives | **The run-dir `state.yaml`, carried forward.** The dry-run writes the full plan; each `--apply` inherits the latest plan, updates its scope's branches, writes its *own* run, and advances the symlink — so `show` always shows the full plan with accumulated dispositions. Reuses existing artifacts, locking, and GC. |
| D4 | Reuse is safe only when… | Scope reuse is decided **per repo** by *resolved slug* membership + per-repo freshness (not by `FilterSpec` algebra). A narrower apply reuses everything; a broader apply re-scans the extra repos. |
| D5 | Deleting from a (possibly stale) plan | Each delete is **re-validated immediately before acting** (§7). Stale-but-safe drift (branch already gone) is tolerated; stale-and-unsafe drift (tip moved, or branch used again) **refuses**. This is the price of reuse and upholds `prdls2nq`. |

---

## 3. The plan artifact (D3)

The plan is the existing `state.yaml` `repos` map (`runstate.py:168`,
`prune_branches.py:576`), enriched. No new file, no new lock, no new GC path.

```yaml
schema_version: 2                 # bump from 1 (schv4nrm convention)
repos:
  org/repo-a:
    analyzed_at: 2026-06-04T09:00:01Z   # NEW — per-repo freshness stamp (D1/§6)
    default_branch: main                # NEW — recorded so apply needn't re-fetch
    branch_count: 3
    branches:
      - branch: feature/foo
        sha: 1a2b3c4...                  # already recorded — the change key (§5)
        decision: delete                 # delete | skip | error
        pr_number: 412
        pr_state: MERGED
        reason: "merged PR #412"
        disposition: pending             # NEW — pending|deleted|failed|refused|already-gone
        acted_at: null                   # NEW — when the disposition last changed
        acted_mode: null                 # NEW — "apply" run that touched it
prune_plan:                              # NEW top-level extra (record_extra, runstate.py:172)
  scope_slugs: [org/repo-a, org/repo-b]  # resolved slugs this plan analyzed (D4)
  concurrency: 12
  fleet_digest: "sha256:…"               # digest of repos.txt at plan time (advisory)
```

- `decision` is the **analysis** (what the guardrails concluded). It is what gets
  cached and reused.
- `disposition` is the **action** state, mutated by `--apply`. The dry-run writes
  every candidate as `pending`.
- `analyzed_at` per repo drives freshness (§6); `scope_slugs` records what the
  plan covers (§4).

`SCHEMA_VERSION` goes 1 → 2 (`runstate.py:31`) with a `this.i` note per the
`schv4nrm` schema-versioning convention. `show`/report read either version
(missing fields default to `pending`/`null`).

---

## 4. Scope: capture and reuse (D2, D4)

`prune-branches` uses only **repo-level** filter dimensions (`orgs`,
`repo_globs`; `filters.py:69` `constrains_repos`). So a run's scope is concretely
**the sorted set of repo slugs surviving `select_repos`** — not the raw
`FilterSpec`. The plan records `scope_slugs`.

This makes reuse a clean **per-repo set test**, immune to glob-spelling
differences (`*/origin-*` vs `provenant-dev/origin-*` resolve to the same slugs):

```
for slug in apply_scope_slugs:           # the resolved slugs of THIS invocation
    entry = plan.repos.get(slug)
    if entry and fresh(entry, max_age):  # in-plan and within --max-age
        reuse entry                       # zero network for analysis (D1 narrowing case)
    else:
        live_scan(slug)                   # auto re-scan missing/stale (D1)
```

A **narrower** apply touches a subset of `scope_slugs`, all present+fresh → full
reuse, no analysis network. A **broader** apply touches slugs absent from the
plan → those (and only those) get live-scanned. The "reuse iff narrower" rule the
maintainer asked for falls out of the per-repo test for free; no global subset
gate is needed.

---

## 5. The change key is the SHA, not a date (idea #4)

`state.yaml` already records each branch's tip `sha` (`prune_branches.py:170`).
`list_branches` returns every branch's current sha in one cheap paginated call.
So we never need a separate "last changed" timestamp — **the tip SHA is the exact
change detector.**

When a repo *is* re-analyzed (stale or `--force-scan`), per branch:

1. Evaluate the **cheap guards fresh, always**: default branch, protected,
   open-PR head/base (`prune_branches.py:173-184`). `my_open_prs` is re-fetched
   this invocation, so the "used again" signal is never stale.
2. If the branch clears the cheap guards **and** a cached entry exists with the
   **same sha**, reuse the cached `decision`/`pr_number`/`reason` — **skip the
   expensive `closed_prs_for_head` + `branch_ahead_by`.**
3. Otherwise (new branch, or sha moved) run the full classification.

Why this is safe — the only directions a same-sha branch's verdict can change:

- *Toward keep* ("used again"): a new open PR from/to it → caught by step 1's
  fresh `my_open_prs`.
- *Toward delete*: its merged PR ages past the grace period (`prgrc3kp`) → a
  `delete` only becomes *more* valid with time; a cached `skip:"<grace> not yet"`
  is the one verdict we must **not** blindly reuse, so grace-pending skips are
  re-classified rather than cached as final (cheap: they already have a known PR).

Net: even a "cold-ish" re-scan of a fleet where most branches are unchanged costs
~1 cheap call per repo plus full classification for only the *moved/new* branches.

---

## 6. Freshness threshold (idea #2)

Per-repo `analyzed_at` + a threshold:

- **CLI:** `--max-age DURATION` (`30m`, `6h`, `2d`). Reuse plan entries younger
  than this; re-scan older ones. `--force-scan` ignores the plan entirely
  (≡ `--max-age 0`).
- **Policy default:** `prune_plan_max_age_minutes: int = 720` (12 h) added beside
  `prune_min_age_days`/`retain_runs` (`config/policy.py:62-96`), overridable
  per-repo like the other prune knobs (`policy_for`).
- **Dry-run** honors the same threshold, so a second dry-run within the window is
  near-instant. Want guaranteed-fresh truth? `--force-scan`.
- **Apply** defaults to reuse-when-fresh (D1); stale/absent → auto re-scan.

Freshness is evaluated **per repo**, so a 150-repo plan with 2 stale repos
re-scans exactly 2.

---

## 7. Plan-based `--apply` with safe re-validation (ideas #1, #5)

```
1. Resolve filters → apply_scope_slugs.
2. Load latest plan (latest-prune-branches/state.yaml).
3. For each slug in apply_scope_slugs:
       reuse fresh in-plan entry, else live-scan it (§4, parallel §8).
4. candidates = rows with decision == "delete" in scope.
5. For each candidate, under repo_lock(slug, exclusive):       # rsclk7nq res #7
       tip = gh.branch_ref_sha(slug, branch)   # NEW cheap GET, immediately pre-delete
       if tip is None (404):        disposition = already-gone   # TOLERATE (idempotent)
       elif tip != plan.sha:        disposition = refused        # tip moved → would lose work
       elif branch in open_heads|open_bases:  disposition = refused   # used again
       else:
           gh.delete_branch_ref(slug, branch)
           disposition = deleted        # AUDIT sha as today (prune_branches.py:436)
6. Under run_state_lock("prune-branches"):           # serialize the carry-forward
       reread latest plan
       merge: this scope's branches ← new dispositions; everything else unchanged
       write THIS run's state.yaml = merged full plan; advance symlink.
```

### 7.1 Safe-vs-unsafe staleness (idea #5, upholding `prdls2nq`)

The data-loss guard `prdls2nq` says a branch is deleted only if no commits are
lost. Reusing an hours-old plan reintroduces a TOCTOU the always-fresh scan
avoided, so apply re-validates the *governing facts* right before each delete:

| Drift since analysis | Direction | Action |
|---|---|---|
| Branch ref already deleted (someone else pruned it) | safe | **tolerate** → `already-gone`, counts as success |
| Tip SHA moved (new push after the merge) | **unsafe** | **refuse** → `refused`, reason "tip moved since analysis" |
| Now head/base of an open PR (reopened / new PR) | **unsafe** | **refuse** → `refused`, reason "branch is used again" |

`delete_branch_ref` deletes by **ref name, not sha** (`gh.py:1782`), so a stale
sha cannot be made conditional at the API. We close the window by re-GETting the
tip immediately before the DELETE (`branch_ref_sha`, one cheap call; candidates
are few by construction). The residual sub-second GET→DELETE window is the
inherent limit of the ref-delete API and is no worse than today's final window.

### 7.2 Carry-forward & accumulation (D3)

The merge in step 6 **inherits the full plan** (all repos, not just this apply's
scope) and updates only the in-scope branches' dispositions. So:

```
dry-run     -> run A/state.yaml = full plan, all "pending"
apply a/b   -> run B/state.yaml = plan, a/b branches "deleted"
apply fleet x -> run C/state.yaml = plan, a/b + x "deleted", rest still "pending"
show        -> latest (run C): the whole plan, dispositions accumulated
```

Two concurrent applies of different subsets: the GitHub deletes serialize on
`repo_lock(slug)`; the carry-forward read-merge-write serializes on
`run_state_lock("prune-branches")` so neither loses the other's dispositions
(this is exactly the run-state "lost update" risk catalogued in `rsclk7nq` §2
row 1 — held for the *quick* merge only, never across the networked deletes).

---

## 8. Parallel fetch (idea #3)

The scan is read-only (deletes happen later, individually, under `repo_lock`), so
fan-out is safe against the locking model. **Reuse the existing pattern** at
`exec.py:608`: a bounded `ThreadPoolExecutor` with pre-allocated ordered result
slots, a progress callback, and the SIGINT handler.

Two parallel passes keep the pool saturated:

- **Pass A (over repos):** `list_branches` + `my_open_prs` + cheap guards + §5
  SHA-cache reuse → yields each repo's list of *needs-full-classification*
  branches. (Repos reused wholesale from a fresh plan skip Pass A entirely.)
- **Pass B (flattened over branches):** `closed_prs_for_head` (+ `branch_ahead_by`
  where needed) for the needs-classification branches across *all* repos in one
  flat queue — so the dominant cost runs at full width regardless of how branches
  are distributed across repos.

- **Knob:** `--concurrency N`; policy default `prune_scan_concurrency: int = 12`.
- **Rate limits:** GitHub REST secondary limits (concurrent-request cap + points/
  min). `_run`'s retry/backoff already handles 5xx; **add 403/429 secondary-rate
  handling** (honor `Retry-After`, exponential backoff) so a wide pool degrades
  gracefully instead of erroring. 12 workers is comfortably under the caps; the
  knob lets the user dial back if throttled.

Expected: cold full scan ~25 min → ~2–3 min. Warm/incremental runs (§5/§6) → low
seconds.

---

## 9. CLI & policy surface

New `prune-branches` flags (added in `_add_prune_common_args`, `cli.py:709`):

| Flag | Meaning | Default |
|---|---|---|
| `--max-age DURATION` | reuse plan entries younger than this; re-scan older | policy `prune_plan_max_age_minutes` (12 h) |
| `--force-scan` | ignore the plan; full fresh scan (≡ `--max-age 0`) | off |
| `--concurrency N` | parallel fetch workers | policy `prune_scan_concurrency` (12) |

New policy defaults (`config/policy.py`, mirroring `prune_min_age_days`):
`prune_plan_max_age_minutes: int = 720`, `prune_scan_concurrency: int = 12`,
both per-repo-overridable via `policy_for`.

`--apply` semantics unchanged on the surface (still the `--apply` opt-in,
AGENTS.md "mutating subcommands default to dry-run"); its *implementation* gains
plan reuse (§7).

---

## 10. `show` / report changes

`_build_summary_md` (`prune_branches.py:513`) and `show prune-branches`
(`commands/show.py`) gain disposition awareness:

- Header: **Plan freshness** — `analyzed_at` min/max across repos; a ⚠ marker on
  repos older than `--max-age`.
- Sections: **Deleted** (with run/sha), **Pending — would delete**, **Refused on
  apply** (tip moved / used again — the §7.1 unsafe cases, surfaced loudly),
  **Kept (guardrail)**, **Errors**.
- "Mode" line becomes "living plan; last action: <apply scope> at <ts>" rather
  than a single APPLY/DRY-RUN label — the artifact is now both.

`refused` dispositions are **attention-worthy** (exit ladder → `EXIT_ATTENTION_NEEDED`)
because they mean reality diverged from the plan in an unsafe way the user should
see; `already-gone` is quiet (routine).

---

## 11. `this.i` nodes to add (proposed)

Written at implementation start per methodology rule 5 (decision-before-code).
All children of `prnbr4kq`:

- `prnpl…` (**decision**, "incremental plan"): prune-branches persists a reusable
  analysis plan in run-dir `state.yaml`; `--apply` reuses fresh in-scope entries
  and auto-re-scans the rest; subset applies accumulate via carry-forward. *why:*
  separate the 30-min analysis from the action; stop the report reset.
- `prnsc…` (**decision**, "scope by resolved slug"): reuse is a per-repo
  resolved-slug + freshness test, not `FilterSpec` algebra (D4).
- `prnsh…` (**decision**, "SHA change key"): tip sha is the cache key; same-sha
  branches skip the expensive classification, with the cheap guards always fresh.
- `prnpar…` (**decision**, "parallel fetch"): bounded thread pool (default 12)
  over a flattened branch queue; reuses `exec.py` fan-out.
- `prnrv…` (**decision**, "safe re-validation"): pre-delete re-GET; tolerate gone,
  refuse moved/reused. *Resolves a* **tension** *between plan reuse and the
  always-fresh-truth that `prdls2nq` relied on.*
- `schv4nrm`: note `state.yaml` schema 1 → 2.

---

## 12. Phasing (TDD, each phase ships independently)

| Phase | Delivers | Idea | Std-alone value |
|---|---|---|---|
| **P1** | Parallel scan + `--concurrency` + 429/secondary-limit backoff | #3 | 25 min → 2–3 min on its own; **no schema change** |
| **P2** | Plan schema v2 + carry-forward `--apply` + `show` dispositions | #1 | Partial applies stop resetting the report; accumulation works |
| **P3** | `--max-age`/`--force-scan` + per-repo freshness + SHA-keyed reuse | #2, #4 | Warm/incremental runs drop to seconds |
| **P4** | Pre-delete re-validation: tolerate-gone / refuse-moved/reused | #5 | Makes plan-reuse safe; mandatory before P2/P3 reuse is trusted on apply |

Ordering note: P1 is pure win, lands first. P4's re-validation is the safety gate
that P2/P3 reuse depends on — if P2/P3 land before P4, gate their reuse behind a
fresh re-validation stub so no unsafe delete is ever possible in between.

### Test plan (offline, per AGENTS.md "no network in tests")

- **P1:** `FakeGHClient` with per-call sleeps/ordering → assert results land in
  input order, one repo's `GHError` doesn't sink the run, SIGINT cancels cleanly,
  `--concurrency 1` == today's behavior.
- **P2:** dry-run writes `pending`; apply subset marks only its slugs `deleted`
  and carries the rest forward; two sequential subset applies accumulate; `show`
  renders the union. Schema-1 state.yaml still loads.
- **P3:** stale repo re-scanned while fresh repos reused; sha-unchanged branch
  reuses verdict (assert `closed_prs_for_head` *not* called); sha-moved + new
  branch reclassified; grace-pending skip re-classified not cached.
- **P4:** 404 → `already-gone` success; moved sha → `refused` (no delete call);
  reopened-PR branch → `refused`; all three surfaced in summary + exit ladder.

---

## 13. Risks & open points

- **Secondary rate limits** are the only real external risk of P1; mitigated by
  the `--concurrency` knob + `Retry-After` backoff. Validate against the live
  ~150-repo fleet before trusting the default 12 (this is a cron-path-adjacent
  change → a one-shot live shakedown per AGENTS.md is warranted for P1).
- **Plan staleness UX:** a very old plan reused on apply could surface many
  `refused` rows; that's correct (loud, safe) but verbose — `show` groups them.
- **`fleet_digest`** is advisory only (repos.txt edits between plan and apply are
  handled by the per-slug membership test in §4, not the digest).
- **Coverage:** 100% branch coverage stands (AGENTS.md); the new branches
  (reuse/skip/refuse/tolerate) are all exercised by the P2–P4 tests above.
</content>
</invoke>
