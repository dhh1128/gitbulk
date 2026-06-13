# gitbulk intent file
# Component: gitbulk (personal nightly PR-triage tool)
# Format: intent code — nodes are named trees of key: value pairs.
#   goal:       — purposive outcome.
#   decision:   — an architectural choice made and locked.
#   constraint: — a non-negotiable boundary condition.
#   tension:    — an open question or deferred decision; do not resolve silently.
#   deviation:  — an approved exemption from a project standard (see
#                 docs/methodology.md §6).
#   Each node carries id: (opaque base32, 6–12 chars, [a-z2-7]) and
#   why: (rebuttal-surface rationale; see methodology.md §2).
# Seed: pre-populated 2026-05-27 from the Phase 0 scaffold conversation,
#       the resolved items in docs/design-notes.md §2–§10 and §11, and
#       the deferred items captured as tension: nodes below.
#       Per methodology §5, decisions made during implementation are added
#       to this file BEFORE the code commit that implements them.

Gitbulk Triage Tool = goal:
  id: q3kfzm7n
  why: >
    Triage open pull requests across roughly 150 repositories nightly without
    human attention, in a way that can run from cron and cannot damage
    in-progress local work. The set is too large to inspect by hand each day;
    by-hand triage degrades to "ignore most of it" within a week. gitbulk
    converts that backlog into a structured report, optional automated
    progressions (merge, rebase-onto-default, close-stale), and a sentinel
    that surfaces the small subset that actually needs human attention.

    Beyond PRs, the fleet itself is the object of maintenance: orphaned
    worktrees, undeleted post-merge remote branches, stale local refs, and
    repos that need work no PR yet exists for are all within gitbulk's
    scope (see node xq4npk7r). "Triage" is the leading verb but "maintain"
    is the full picture; the file-based artifacts and unattended-cron
    discipline cover both.

  children:

    # ─── CONSTRAINTS (non-negotiable) ────────────────────────────────────────

    Local Git Safety Contract = constraint:
      id: 7mxr4pql
      why: >
        gitbulk must never modify the working tree, index, or HEAD of any
        clone under ~/code/. The user is actively editing those clones; a
        rogue checkout, pull, reset, or stash would silently destroy
        in-progress work that the user has no way to detect before next
        login. Operations that require a checkout MUST use git worktree add
        into a disposable path (see node mw6kp2nq). Read-only git -C
        invocations (rev-parse, status --porcelain, config --get, log) are
        fine. This is the most important rule in the project; every mutating
        code path is suspect until proven to honor it.

    Network Via gh CLI Only = constraint:
      id: hp4nck2v
      why: >
        All GitHub network access goes through the gh CLI, never raw HTTPS
        or PyGithub. The user has gh authenticated globally; layering a
        second credential path (token file, keyring, env var) would create
        a place where credentials can leak or drift out of sync with gh's
        rotation. gh also provides GraphQL access for free and has built-in
        secondary-rate-limit handling. Cost: gh's argv interface is the
        contract we depend on, which is less stable than a library API;
        we accept that tradeoff for the credential-management simplification.

    SSH Git Authentication = constraint:
      id: ks52rg4w
      why: >
        Git fetch/push operations use SSH, not HTTPS. The user's ssh-agent
        is already configured across all clones; layering an HTTPS-token
        path in gitbulk would duplicate the credential surface that node
        hp4nck2v simplified away, and produce a tool that breaks when the
        user rotates their token. Cost: gitbulk cannot operate on repos
        that only allow HTTPS clone — accepted, since all target repos
        (provenant-dev plus the user's own OSS work) support ssh.

    Python 3.10 Minimum Version = constraint:
      id: 6jz4n2pq
      why: >
        Minimum Python is 3.10, enforced at startup in cli.py. AGENTS.md
        mandates this, and modern type-hint syntax (X | None, list[T]
        without imports, match statements) reads more cleanly than the
        3.9-compatible equivalents. Cost: gitbulk will not run on Debian
        stable until it catches up; accepted since the user controls their
        own runtime.

    # ─── METHODOLOGY & DISCIPLINE ────────────────────────────────────────────

    Methodology Adoption = decision:
      id: nh4kp2rq
      why: >
        gitbulk follows the development methodology defined in
        docs/methodology.md (copied from origin-platform): structured intent
        in this.i, the speculative interview before each phase of
        implementation, this.i commits ordered before the code commits they
        justify, named adversarial reviewer roles at gate boundaries,
        deviation: nodes for any standard exceptions, and TDD discipline per
        AGENTS.md. This is sensitive automation against ~150 real repos;
        the "speculative interview forces decisions to be explicit"
        discipline is exactly the safety net this tool's blast radius
        requires. Cost: more upfront process per phase than ad-hoc dev would
        need; the user has explicitly bought into that cost.

    Hundred Percent Branch Coverage Of Src = decision:
      id: cn4pk7zq
      why: >
        Coverage standard is 100% branch coverage on src/gitbulk/. The
        framing in AGENTS.md — "a bug in gitbulk can damage real work in
        real repos" — applies most acutely to the local-git safety contract
        (node 7mxr4pql), where an untested fallback branch could be the one
        that writes to the main clone instead of a worktree. Any gap
        requires an approved deviation: node; a gap without one is a defect.
        CI enforces this gate. Cost: writing tests for every branch,
        including obvious-looking defensive code; accepted because this
        tool's defensive code is exactly where the working-tree-safety rule
        lives.

    # ─── SCOPE & OWNERSHIP ───────────────────────────────────────────────────

    Personal Tool Single User = decision:
      id: nfk2zpr3
      why: >
        gitbulk is built for one user (the maintainer), not a team. No
        multi-tenancy, no auth model, no access controls, no shared-state
        coordination beyond what the user runs concurrently themselves.
        Treating it as personal infra removes a large class of design
        surface a generalized tool would require (config namespacing,
        per-user run dirs, role-based subcommand gating). If a second user
        ever appears, that is the moment to revisit the simplification;
        until then this assumption is load-bearing.

    Local Repos Are First Class Citizens = decision:
      id: xq4npk7r
      why: >
        Local clones, their branches, their worktrees, and the cruft that
        accumulates around them are managed objects in their own right —
        not just bearers of pull requests. The fleet gitbulk maintains is
        (repos × PRs), not just PRs. Concretely in scope: orphaned
        worktrees from crashed dispatches, undeleted post-merge remote
        branches, stale local branches that match merged/closed PRs, and
        repos that need work no PR yet exists for (e.g., "this repo has
        no CI", "this README is stale"). The local-git safety contract
        (node 7mxr4pql) still applies in full: gitbulk inspects and
        reasons about clones but never writes to a working tree the user
        is editing. Rationale for promoting repos to first-class status:
        cruft compounds silently — "I'll clean up after the merge" never
        actually happens at fleet scale, so gitbulk must do it
        automatically the way it triages PRs. Rejected alternative: keep
        gitbulk PR-only and have a separate tool for repo-level work.
        Rejected because the candidate set, locking model, invariant
        framework, run state, and notification layer are all the same
        between the two concerns; splitting would duplicate infrastructure
        for no semantic gain.

    Personal Account Owns The Public Repo = decision:
      id: 6xp4kq2n
      why: >
        The repo lives under dhh1128/gitbulk (public), not under
        provenant-dev/. gitbulk is personal infrastructure (see node
        nfk2zpr3): it triages PRs across the user's full repo set, most
        of which happen to be under provenant-dev but many of which are
        not. Housing the tool under the user's personal account avoids
        implying that provenant-dev maintains it, sponsors it, or stands
        behind it; this is the maintainer's nightly cron, not an
        org-platform service. Public visibility is incidental — gitbulk
        has no secrets — and being public makes the methodology-driven
        development process (this.i commits, speculative interviews,
        adversarial reviews) legible to anyone who wants to learn from it.
      approved-by: daniel, 2026-05-27

    Cron Driven Unattended Primary Mode = decision:
      id: 4kp7nb2x
      why: >
        Primary execution mode is unattended cron. Every UX decision
        downstream of this — file-based notifications (node tp4kq2nr),
        ATTENTION sentinel, exit-code signaling, dry-run defaults
        (node 2vqp4nk6) — follows from "no human is watching when this
        runs." An interactive mode is supported (run a subcommand by hand
        any time), but the interactive path must not require human
        attention to be safe; it must produce the same artifacts the cron
        path does.

    Live Cron-Tick Shakedown Verifies Unattended-Mode Changes = decision:
      id: shkd5crn
      why: >
        Any change that touches the unattended path is NOT considered
        verified by the unit suite alone. The unit tests run in-process with
        a rich environment and cannot reproduce the failure modes that only
        appear when cron itself invokes the wrapper: a missing MTA silently
        discarding output, gh credentials unreachable from cron's scrubbed
        environment, a PATH that lacks ~/.local/bin or git/gh, config-root
        defaulting to the wrong place, or the cron daemon not running at all.
        The standing acceptance test for the cron path is therefore a LIVE
        one-shot cron tick: install a crontab line pinned to a specific
        minute a minute or two in the future (minute+hour+day-of-month+month,
        so it fires exactly once and does not recur), run a READ-ONLY
        subcommand (report) first, watch it fire via the system journal,
        then study every artifact it produces (cron log, run dir, exit code,
        the last-*.log symlink per node clip7nm4, the ATTENTION sentinel per
        node tp4kq2nr) and remove the one-shot. Mutating subcommands graduate
        to this only after the read-only tick is clean, and dry-run before
        --apply (node 2vqp4nk6). Scope of "the cron path": bin/gitbulk-cron,
        the exit-code/symlink contract, the sentinel, config-root resolution
        under a scrubbed environment, and how any subcommand behaves run
        headless. This shakedown was first run 2026-05-29 (see tension
        opd3ny5k #3) and proved exactly the headless-only properties unit
        tests cannot: config-root defaulted correctly with no --config-root
        flag, gh auth worked from a scrubbed env, and MAILTO was silently
        dropped for lack of an MTA. Future agents SHOULD proactively propose
        this shakedown whenever a change lands on the cron path or before a
        first real cron deployment; the AGENTS.md hard rule points here for
        the why so the suggestion is spontaneous and we do not redesign the
        verification each time. Cost: a few minutes of wall-clock and a
        transient crontab entry — cheap relative to discovering a
        headless-only break overnight against ~150 real repos.
      approved-by: daniel, 2026-05-29

    # ─── CLI ARCHITECTURE ────────────────────────────────────────────────────

    Subcommand CLI Architecture = decision:
      id: 7w4mxr5z
      why: >
        argparse subcommands (report, summarize, dispatch, merge,
        rebase-onto-default, close-stale, show, ack, invariants), not a
        single monolithic command with flags. Each subcommand maps to a
        single mental model — "what did you ask gitbulk to do?" — and
        each can declare its own lock requirements, invariant chain, and
        exit-code semantics independently. Rejected: a single `gitbulk run`
        that consumes a config-defined pipeline, which would have made
        cron entries shorter but lost the one-purpose-per-cron-entry
        isolation that prevents a failing merge from disabling the nightly
        report.

    Mutating Subcommands Default Dry Run = decision:
      id: 2vqp4nk6
      why: >
        Every mutating subcommand (merge, rebase-onto-default, close-stale,
        dispatch) defaults to --dry-run and requires --apply to actually
        act. A misconfigured cron entry must not silently merge PRs the
        user hasn't reviewed. Cost: first-time use of any mutating
        subcommand requires --apply on every invocation forever; accepted
        as the audit signal it is.

    Merge Method Default And Per-Repo Override = decision:
      id: gji4dyze
      why: >
        Default merge method is ``merge`` (a true merge commit), passed
        through to ``gh pr merge --merge``. Per-repo override via
        ``repos.<slug>.merge_method: merge|squash|rebase`` honors the
        cases where a specific repo's convention differs. Branch
        cleanup defaults to ON (``gh pr merge --delete-branch``);
        GitHub server-side refuses to delete a branch still pointed to
        by another open PR, so that's the safety net there.

        Why merge (not squash) as default: the user's review history is
        valuable. Squash collapses each PR to one commit, losing the
        intermediate commits that explain HOW a change was developed
        (incremental refactors, "ah, that broke a test, here's why"
        commits). Merge commits preserve that ladder and make
        ``git bisect`` more useful. Squash makes sense when a project
        treats PRs as opaque units; the user's projects don't.

        Why per-repo override matters: some repos (e.g. ones with
        outside contributors whose commit hygiene varies) genuinely
        want squash. Others (history-curated mainlines that mandate
        linear history) want rebase. A single default can't serve
        all; the override exists for the minority of repos that need
        something other than the default.

        Phase 5 originally hardcoded squash with delete-branch=true,
        deferring per-repo override (recorded in gaps.md as a known
        gap). Decided 2026-05-28: undeferred, with the default
        flipped to merge per the user's stated preference. The
        Phase-5 implementation comment is now the wrong default; the
        per-repo override IS implemented.

        Not in scope here (deferred to gitbulk gc, tension jw3kpn4q):
        local worktree / branch cleanup after merge. AGENTS.md forbids
        touching the main clone, so the only legitimate post-merge
        local cleanup target is disposable worktrees under
        ~/.cache/gitbulk/worktrees/; that work happens with the gc
        subcommand, not the merge subcommand.
      approved-by: daniel, 2026-05-28

    One Merge Per Repo Per Run Guardrail = decision:
      id: kdgmyj7o
      why: >
        ``gitbulk merge --apply`` will merge AT MOST one PR per repo per
        run. If multiple gate-passing PRs exist in the same repo, the
        lowest-numbered one (oldest, most likely to have been ready
        longest) is merged; the rest are recorded as "deferred to next
        run" in summary.md and state.yaml. Dry-run mirrors the same
        partitioning so what the user sees in DRY-RUN is what APPLY
        will actually do.

        Why: merging PR A in a repo has domino effects on its siblings
        targeting the same base — chiefly (a) mergeable_state goes
        DIRTY on conflicting siblings, and (b) if branch protection has
        "Dismiss stale pull request approvals when new commits are
        pushed" enabled, sibling approvals get dismissed silently when
        the base advances. Acting on N PRs in a single run means
        operating against state we KNOW is about to change.

        Discovered the hard way 2026-05-28: PR #93 was merged by hand
        into provenant-dev/origin-shim-svc; sibling PR #95 immediately
        went DIRTY. gitbulk's pr.mergeable_state_clean correctly
        Skipped it, but only because we hadn't already tried both in a
        single run. Without the guardrail, a multi-PR cron tick would
        attempt #95 against fresh stale data.

        Rejected: "refetch state after each merge." Doubles GraphQL
        calls, adds an async race window (GitHub recomputes
        mergeable_state asynchronously; the refetch could return
        UNKNOWN), and cron's per-tick cadence means a deferred PR only
        waits one cron interval to be picked up. Simple beats
        elaborate.
      approved-by: daniel, 2026-05-28

    Watchdog Ack On First Clean Observation = decision:
      id: yhwagcvw
      why: >
        Once the post-merge watchdog observes a merge commit in a
        "clean and complete" state — every check_run has status =
        ``completed`` AND every conclusion is in {success, skipped,
        neutral} — gitbulk persists an ack record at
        ``~/.cache/gitbulk/watchdog-acked.yaml`` and skips that
        (slug, sha) on all subsequent ``gitbulk report`` runs.

        Why: the prior design re-fetched check_runs for every merge
        on every nightly report, repeatedly re-confirming the same
        green state. That's noise without value — once green stays
        green, GitHub doesn't asynchronously turn it red.

        The "all completed" gate matters. Without it, a 5-minute-post-
        merge report could ack a commit whose test workflow has
        finished but whose cd workflow hasn't started yet, missing the
        delayed cd failure entirely. ``_is_ackable`` returns False as
        long as anything is still ``in_progress``/``queued``, so the
        watchdog keeps watching until every workflow has reported.

        Conservative on unknown conclusions: a completed check with
        ``conclusion=None`` or a future-GitHub-value we don't recognize
        does NOT count as passing. Better to keep watching than to ack
        uncertainty.

        Tradeoff accepted: a delayed/scheduled check_run (a weekly
        Dependabot scan that fires hours later on the same SHA) could
        appear post-ack and fail. The watchdog would not catch it
        because the ack is permanent. That's not really a "this merge
        broke CD" failure — it's a separate concern that GitHub's own
        notifications cover. Rejected alternative: re-fetch even
        ack'd commits to catch this case → re-introduces the original
        noise problem the ack was designed to solve.

        Retention: ack entries are pruned at write time if older than
        7 days, purely housekeeping since the 24h scan window in
        ``_check_recent_merges`` already filters older merges from
        even being candidates.
      approved-by: daniel, 2026-05-28

    Post-Merge CD Watchdog = decision:
      id: aazqlwc3
      why: >
        After each successful merge, gitbulk captures the resulting
        merge commit SHA (via a follow-up ``gh pr view --json
        mergeCommit`` call) and records it in state.yaml. On every
        subsequent ``gitbulk report`` run, the report scans run-state
        from the last 24 hours, collects (slug, merge_sha) pairs (cap
        50), fetches check-runs for each via
        ``gh api repos/<slug>/commits/<sha>/check-runs``, and surfaces
        any failing conclusions ({failure, cancelled, timed_out,
        action_required, stale}) in a "Recent merges" section at the
        top of the report. ATTENTION is raised on any failure.

        Why: gitbulk's view of a merge ends the moment ``gh pr merge``
        returns. CD workflows (cd.yml, deploy.yml, package-publish)
        often run AFTER the merge and can fail invisibly to gitbulk.
        Discovered 2026-05-28: a manual merge of #93 in
        provenant-dev/origin-shim-svc broke cd.yml; gitbulk had no
        record. Two-call cost (one extra ``gh pr view`` per merged PR,
        one ``gh api check-runs`` per recent merge per report run) is
        small.

        Rejected: a dedicated ``gitbulk verify-merges`` subcommand
        polling more aggressively (every 5 minutes for 30 minutes
        post-merge). Higher cost, requires cron orchestration, and
        report already runs nightly which is the natural cadence for
        catching previous-day breakage.

        Rejected for v1: acknowledgement-of-seen-failures mechanism.
        Every report run shows current state of recent merges; if a
        failure stays red, every report run re-flags it as ATTENTION.
        If that becomes noisy in practice, add an ``ack`` flow then.

        Failure to fetch check-runs (gh error, transient or otherwise)
        is recorded as WARNING but does NOT force ATTENTION — we don't
        actually know the check state, and a downstream gh outage
        shouldn't escalate every report run.
      approved-by: daniel, 2026-05-28

    Two File Configuration = decision:
      id: ws2pn4kr
      why: >
        Configuration lives in two files at ~/.config/gitbulk/: repos.txt
        (plain owner/repo per line, # comments, blanks ignored) and
        gitbulk.yaml (policy and classification). Rejected richer formats
        (YAML for repos, inline tags) because repos.txt is the file the
        user edits most often and minimizing its friction matters more
        than format consistency. Cost: per-repo policy and repo membership
        live in different files, so adding a repo with custom policy means
        editing both; accepted as the optimized common case.

    # ─── POLICY EXPRESSION ───────────────────────────────────────────────────

    Invariants Framework = decision:
      id: c4jzm5pn
      why: >
        Operations on repos and PRs are expressed as chains of named
        invariants (Pass/Skip/Fail functions) registered in a central
        registry. Subcommand X's behavior is fully defined by which
        invariants it includes and in what order — there is no other
        place where policy hides. This makes policy auditable
        (`gitbulk invariants` lists everything), suppressions explicit
        (`--skip-check NAME` logs a WARNING into run state), and the
        deny-by-default stance enforceable: a new operation that touches
        repos or PRs must compose existing invariants or add new ones,
        not bypass the chain. Cost: some indirection compared to inline
        checks; accepted for the auditability it buys.

    Cmdline Wins Over Config For Overrides = decision:
      id: r4nzp7kq
      why: >
        When cmdline and config disagree about an invariant's status,
        cmdline always wins. Asymmetric audit: cmdline RELAXING
        (--skip-check on something the config required) trips exit
        code 4 plus a WARNING in invariants.log; cmdline TIGHTENING
        (--require on something the config skipped) logs INFO only.
        Rationale: tightening is always safe, relaxing is the auditable
        event. Rejected: most-restrictive-wins, which would have removed
        the user's ability to actively loosen a single run when they know
        what they are doing.

    Manifest Stamps The Acting GitHub Identity = decision:
      id: actrstmp7q
      why: >
        Every run manifest carries an `actor` field naming the GitHub login
        gitbulk acted as. The audit trail's purpose is to attribute every
        mutating action (merge/close/force-push) to an identity; without the
        actor a reader of manifest.yaml can reconstruct WHAT was done but not
        WHO it was done as — a gap that matters precisely when a cron host is
        misconfigured to authenticate as the wrong account. The login is
        stamped from inside the `gh.authenticated` UNIVERSAL invariant
        (catalog.py), which is the first and only point where the operator's
        identity is both fetched (`gh api user`) AND verified non-empty; it
        was previously fetched there and discarded. This covers all six
        gh-touching subcommands (report, summarize, dispatch, merge,
        rebase-pr, close-stale) at one site rather than threading an actor
        argument through nine RunState.begin call sites, and avoids a second
        redundant `gh api user` round-trip. The invariant therefore has a
        deliberate write side effect (record_actor -> manifest.yaml) beyond a
        pure Pass/Skip/Fail check; accepted because the verified identity is
        an audit fact that only exists at that moment. begin() seeds
        `actor: null` so the key is always present (audit consumers get a
        stable schema; null means "no verified identity was recorded" — e.g.
        a run that failed auth, or a non-gh subcommand like prune-*). This is
        an ADDITIVE manifest field, not a breaking shape change, so
        SCHEMA_VERSION is not bumped. Local-only subcommands that never run
        the universal chain (prune-branches/-worktrees, recover-branch) leave
        actor null for now; extending coverage to them is a separate change.

    Untrusted Refs And SHAs Are Validated At The gh Boundary = decision:
      id: gtargv7n
      why: >
        base_ref / head_ref / head_sha originate from gh's GraphQL/REST JSON
        and are interpolated into git subprocess argv (rebase.py fetch/rebase/
        force-push) and into REST API paths (gh.fetch_check_runs:
        `repos/<slug>/commits/<sha>/check-runs`). A ref that begins with `-`
        is parsed by git as an OPTION rather than a positional — e.g.
        `--upload-pack=<cmd>` on a fetch is remote-code-execution under cron;
        a sha containing `/` or `?` redirects the REST path. Two layers of
        defense, fail-closed:
          (1) PRIMARY — validate at ingest. `gitbulk.util.gitref` exposes
          `is_safe_ref` (non-empty, no leading `-`, no whitespace/ASCII
          control) and `is_valid_sha` (`^[0-9a-f]{7,40}$`).
          `_pr_info_from_graphql_node` calls `ensure_safe_ref` on
          base_ref/head_ref and `ensure_valid_sha` on head_sha; a violation
          raises GHError and PROPAGATES (the run aborts loudly naming the bad
          value). This is acceptable because GitHub/git's own refname rules
          forbid leading-dash and whitespace refs, so legitimate data never
          trips it — a value that does is an attack or corruption and must
          not reach git. `fetch_check_runs` independently `ensure_valid_sha`s
          its sha argument before building the REST path (folds SEC-F2).
          (2) DEFENCE-IN-DEPTH — `--` terminator. rebase.py inserts `--`
          before the positional ref in every `git fetch origin -- <base_ref>`
          and `git rebase -- origin/<base_ref>`, so even an unvalidated
          dash-leading ref is forced to be read as a refspec. (Verified git
          accepts `--` there, 2026-06-08.) The force-push refspecs
          (`--force-with-lease=<ref>:<sha>`, `HEAD:<ref>`) are already
          option-safe by their fixed prefixes once the ref/sha are validated.
        Rejected: a per-PR skip on a malformed ref inside my_open_prs — it
        would let a single crafted PR silently vanish from triage; a hard
        abort is the safer security posture for a tool that force-pushes.

    Fork / Cross-Repo PRs Are Fork-Aware In rebase-pr And merge = decision:
      id: frkrep5q
      why: >
        A PR opened from a fork has its head branch on the FORK, not on
        `origin` (the base repo gitbulk has a clone of). Two unconditional
        operations were wrong for such PRs: (a) rebase-pr's
        `force_push_with_lease(worktree, head_ref, head_sha)` pushes
        `HEAD:<head_ref>` to `origin` — for a fork PR that targets a branch
        on the BASE repo that has nothing to do with the PR, either failing
        or clobbering an unrelated ref; (b) merge passed
        `delete_branch=True` unconditionally, asking GitHub to delete a
        branch that lives on someone's fork (gitbulk's standing policy is to
        never delete fork branches — see ClosedPRRef / prune). Fix: PRInfo
        gains `is_cross_repository` (from GraphQL `isCrossRepository`, the
        authoritative fork signal) and `head_repo_slug` (from
        `headRepository.nameWithOwner`, null when the fork is deleted), both
        default-valued so fixtures stay ergonomic. rebase-pr adds a PER_PR
        invariant `pr.head_on_origin` that SKIPS cross-repo PRs (this-PR-
        doesn't-qualify, not a structural Fail). merge passes
        `delete_branch=not pr.is_cross_repository`, so fork PRs still merge
        but their fork branch is left intact. Local-only prune commands
        already had head_repo_slug via the REST path and are unaffected.

    # ─── PR CLASSIFICATION & MERGE-READINESS ─────────────────────────────────

    Unknown Accounts Default Non Human = decision:
      id: pj5kn2zw
      why: >
        For humans-vs-bots classification, unknown logins default to
        non-human. The set of bots grows over time (new CI tools, new
        review bots), so the default needs to favor the failure mode that
        is correctable rather than the one that silently merges
        unreviewed code. If a real human appears under an unknown login,
        gitbulk skips their input and the user adds them to
        humans.always_human or org_members — discoverable. If a new bot
        defaulted to human, it might silently mark a PR "approved" by a
        bot, which is invisible until something breaks downstream.

    Ready To Merge Stricter Than GitHub = decision:
      id: zk3r4nqp
      why: >
        gitbulk's "ready to merge" is stricter than GitHub's
        mergeable_state == clean. A PR is ready iff (a) GitHub says clean
        (CI green, required checks passed, mergeable), AND (b) all review
        threads — including bot threads — are resolved, AND (c) it has
        been continuously in this state for the configured age threshold.
        ready_since is the start of the most recent continuous ready
        window. Bot threads counted in (b) per user preference 2026-05-27:
        a forgotten Dependabot nag or unaddressed maintainer comment
        should block auto-merge; the cost of merging with an unresolved
        concern is much higher than the cost of pausing until it is
        resolved.

    Three Business Days From Continuously Ready = decision:
      id: bg4pqn7m
      why: >
        Default age threshold for auto-merge is 3 business days (M–F,
        local timezone, no holiday awareness) since ready_since
        (node zk3r4nqp). Three days lets async review cycles complete
        without letting trivial PRs sit indefinitely. Business days
        rather than calendar days because weekend hours are not review
        hours; "the conversation has been quiet for 3 days" means
        something different over a Tue–Fri span than over a Fri–Mon span.
        Per-repo overrides remain the primary tuning knob. Rejected:
        2 days (too aggressive), 5 days (too conservative for a personal
        tool), measuring from PR creation (lets actively-edited week-old
        PRs slip through).

    Approval Bypasses Age Gate = decision:
      id: kjyfc4m5
      why: >
        An explicit approval short-circuits the 3-business-day window
        from bg4pqn7m and the timeline anchor from zk3r4nqp. The 3-day
        wait exists to let humans react to a PR; an approval IS the
        positive human reaction, so further waiting is pointless.
        Concretely: (a) ``compute_ready_since`` treats an ``approved``
        timeline event as a breaker-closer for ``changes_requested``
        but NEVER advances the anchor — approvals are what we're
        waiting for, not a fresh-start event; (b) ``pr.age_threshold``
        short-circuits to Pass when ``review_decision == "APPROVED"``,
        making the age gate effectively vacuous under strict policy
        (where ``pr.approved_per_policy`` already required APPROVED)
        and a "soft escape hatch" under ci-only (the 3-day wait still
        applies to green-CI PRs without human review, but evaporates
        the moment a human says yes). Decided 2026-05-28 after the user
        observed that my initial timeline-aware implementation was
        restarting the clock on approval, which produced the exact
        wrong behavior — making the user wait 3 more days after the
        signal they were waiting for. Rejected alternative: treat
        approval as a normal restorer (would re-anchor at approval
        timestamp, breaking the user's "merge immediately on approval"
        mental model).
      approved-by: daniel, 2026-05-28

    Maintainer Auto-Approve On Merge = decision:
      id: aprmn5kq
      why: >
        Problem surfaced by a 2026-05-30 smoke test against a green
        Dependabot PR (provenant-dev/origin-sip-policy-lib#15): under the
        default ``strict`` merge_policy, ``pr.approved_per_policy`` requires
        an APPROVED review, so a perfectly green bot PR NEVER auto-merges —
        a maintainer must click "approve" by hand, forever. The user asked
        for a command-line switch that supplies that approval programmatically
        when they are a maintainer and everything else already passes.

        Decision: a ``--approve`` flag on ``merge``. On the ``--apply`` path,
        for a candidate PR whose ONLY merge-chain blockers are
        ``pr.approved_per_policy`` (strict-needs-APPROVED) and the cascading
        ``pr.age_threshold``, gitbulk posts an approving review AS THE USER
        (``gh pr review --approve``) and then merges. Because that approval is
        real, the existing age bypass (node kjyfc4m5) collapses the
        3-business-day cooling-off — the user chose "approve ⇒ merge now"
        (2026-05-30) over "approve but still wait", since an explicit approval
        is exactly the human signal the wait exists for.

        Scope is deliberately narrow — auto-approval is the strongest single
        action gitbulk takes, so it must be hard to fire by accident:
          (a) AUTHOR GATE. Auto-approve only PRs whose author is a configured
              bot (``policy.bots`` — Dependabot et al.), the safe default the
              user named ("without [a filter], only dependabot"). A repeatable
              ``--approve-author LOGIN`` flag explicitly whitelists specific
              NON-bot logins; absent it, non-bot PRs are never auto-approved
              (no silent rubber-stamping of human work the maintainer hasn't
              read). This is the "cmdline filter arg that clarifies scope" the
              user asked for.
          (b) NOT SELF. Never auto-approve a PR the viewer authored — GitHub
              rejects self-approval (422) and it would be meaningless. (Bots
              are never the viewer, so this only matters for --approve-author.)
          (c) MAINTAINER. The viewer must hold write/maintain/admin permission
              on the repo (the "if I'm a maintainer" condition), checked via
              the repo permissions API (a new gh client method).
          (d) SOLE GATE + STRICT. Every other invariant must already PASS, and
              the repo's effective merge_policy must be ``strict`` — NEVER
              auto-approve a ``never`` repo (whose approved_per_policy also
              Skips, but for the opposite reason).
        Requires ``--apply``; in dry-run it posts NOTHING and only reports what
        it WOULD approve+merge. Each auto-approval is recorded prominently in
        run state and invariants.log — it collapses the human review step on
        the user's behalf, so it must be legible after the fact.

        Why a per-invocation FLAG rather than a merge_policy value: auto-approval
        is a maintainer's act of trust at a moment in time ("I choose to
        rubber-stamp this class of PR on this run"), not standing config. A flag
        keeps it off by default and out of unattended cron unless the cron line
        opts in explicitly. Rejected: (1) an ``auto-approve`` merge_policy (too
        easy to leave on); (2) approving every fetched PR (would rubber-stamp
        unread human PRs — hence bots-only default + explicit per-login widen);
        (3) re-fetching review_decision after approving (needless round-trip — a
        successful approve deterministically makes approved_per_policy and
        age_threshold Pass, so the PR is promoted to eligible in-process). New
        gh client methods required: approve a PR, and read the viewer's repo
        permission. Composes with ``--author`` (fetch scope) and the
        one-merge-per-repo guardrail (node 2vqp4nk6 family).
      approved-by: daniel, 2026-05-30

    Unresolved Thread Burden Configurable Default Author = decision:
      id: hj3nq5kp
      why: >
        Whose responsibility is it to mark a review thread resolved?
        Default: the PR author (i.e. the user, since gitbulk operates on
        their PRs). Configurable per-repo via
        defaults.unresolved_burden: me|other|either. When the user is in
        CODEOWNERS for a repo and acting as maintainer rather than
        contributor, "other" or "either" lets them flip the burden onto
        the human reviewer. Default stays "me" because that is the safer
        failure mode — the user is more likely to forget to resolve their
        own threads than to forget the repo's burden setting.

    # ─── CLOSE-STALE BEHAVIOR ───────────────────────────────────────────────

    Any Activity Resets Stale Clock = decision:
      id: 2aefqte7
      why: >
        For close-stale, the "is this PR inactive enough to consider"
        check uses GitHub's ``updatedAt`` field, which advances on ANY
        timeline event — push, comment, review, label change, base
        change — by any actor (human or bot). This is the simpler of
        the two semantics considered. Rejected "human activity only"
        (would require classifying every actor and walking timeline
        nodes for each PR, expensive); rejected "push only" (would
        close PRs that are alive in active human discussion).
        Tradeoff accepted: a PR with a chatty Dependabot might never
        go stale even though no human is doing anything; if this hurts
        the user can set bot logins to humans.exceptions to push them
        out of being treated as activity-relevant — but in practice
        the cost of an under-closed PR (it sits there) is much less
        than the cost of an over-closed PR (it loses work-in-progress).

        Side effect that drives a separate decision (e4yuzip6): when
        gitbulk posts its own stale-warning comment, that also bumps
        updatedAt, so the warning would re-extend the stale window by
        itself unless the close-stale logic compensates. See the
        marker-comment pattern.
      approved-by: daniel, 2026-05-28

    Stale Warning Marker Comment = decision:
      id: e4yuzip6
      why: >
        Persistent state for "we already warned this PR" is encoded in
        the PR's own comments via an HTML-comment marker
        (``<!-- gitbulk: stale-warning v1 -->``). On each close-stale
        run, fetch the PR's last 50 comments via GraphQL, look for the
        marker, and use the comment's createdAt as the
        warning-timestamp anchor for the cooloff check.

        Rejected: a separate local cache file
        (~/.cache/gitbulk/stale-warnings.yaml) tracking which PRs were
        warned and when. Reasons: (1) drifts when the user moves
        machines (cron host vs laptop); (2) drifts when GitHub
        notifications carry the warning to the PR author but the cache
        record disappears; (3) the warning comment itself is
        load-bearing — if it's deleted from GitHub, the cooloff
        SHOULD restart, which the marker-comment pattern naturally
        encodes (no marker = no prior warning = start over).

        Side effect (2aefqte7): the marker comment bumps updatedAt.
        ``pr.inactive`` uses ``stale_cooloff_days`` (not
        ``stale_age_days``) as its threshold so warned-in-cooloff PRs
        still pass the gate; the handler enforces ``stale_age_days``
        only for the WARN decision specifically. See
        ``commands/close_stale.py:_decide_action`` for the matrix.

        Marker versioning: ``v1`` lets a future warning-text revision
        be detected (handler can match both v1 and v2 during a
        transition, or refuse v0 if format changes incompatibly).
      approved-by: daniel, 2026-05-28

    Stale Policy Per-Repo Knob = decision:
      id: esizf2qp
      why: >
        ``defaults.stale_policy`` (and per-repo
        ``repos.<slug>.stale_policy``) takes one of three values:

          - ``warn-and-close`` (default): post stale-warning comment,
            wait ``stale_cooloff_days``, close. The standard path.
          - ``warn-only``: post the warning but never close. Useful
            while tuning thresholds on a new repo before committing to
            close behavior, or for repos where the user wants the
            heads-up but doesn't trust autoclose.
          - ``never``: skip close-stale entirely for this repo.

        Parallel to the existing ``merge_policy: strict | ci-only |
        never`` shape — same naming pattern, same per-repo override
        mechanism, same "never" as the off-switch. Rejected reusing
        ``merge_policy: never`` for both: a repo might want manual
        merges only AND auto-close stale PRs, or vice versa. They are
        unrelated decisions, so they get unrelated knobs.

        Per-repo opt-out is the load-bearing case (one repo I don't
        want autoclosed without bothering with thresholds). The
        warn-only middle case is cheap to support and useful during
        rollout.
      approved-by: daniel, 2026-05-28

    Keep Branch On Stale Close = decision:
      id: 45njfyds
      why: >
        ``gh.close_pr`` defaults to ``delete_branch=False``, in
        contrast to ``gh.merge_pr`` which defaults to ``True``.
        Reason: a stale-closed PR is often abandoned-but-recoverable
        — the author may want to revisit, fix the rebase, and reopen.
        Deleting the branch would force a re-push from local. GitHub
        branch storage is essentially free; the reversibility wins.

        Per the close-stale design interview, the alternative ("delete
        the branch too") and the per-repo-configurable middle ground
        were both considered and rejected for v1. If the asymmetry
        with merge_pr becomes irritating, add a ``stale_delete_branch``
        boolean knob then.
      approved-by: daniel, 2026-05-28

    # ─── PRUNE COMMANDS (fleet cleanup, node xq4npk7r realized) ──────────────

    Prune Branches Subcommand = decision:
      id: prnbr4kq
      why: >
        ``gitbulk prune-branches`` deletes remote branches whose ONLY pull
        requests are merged-or-closed, so the post-merge cruft xq4npk7r
        names ("undeleted post-merge remote branches") stops accumulating
        at fleet scale. It is the remote half of the deferred jw3kpn4q
        cleanup work, promoted to a first-class command rather than a
        catch-all ``gc``: the candidate set (remote branches) and its
        guardrails are distinct enough from the local worktree half
        (node prnwt5nq) that one combined command would muddle two safety
        models. Like merge it needs no clone — it works purely through gh
        — so needs_clone is False and the local-git safety contract is not
        even in play for this command.

        Deletion goes through the GitHub ref API (node prdel4rq), never
        ``git push --delete``. A branch is deleted only when ALL hold,
        biasing to skip-with-reason on any ambiguity or gh error
        (design-notes §11 principle): (a) it is NOT the repo's current
        default branch; (b) it is NOT protected by a branch-protection
        rule; (c) it is NOT the head of any OPEN pr (a branch can carry
        several PRs — one merged does not free it); (d) it is NOT the base
        of any OPEN pr (the stacked-PR dependency — deleting it orphans the
        dependent PR; this is the "other things depend on it" case the user
        called out); (e) it loses no commits (node prdls2nq); (f) the head
        repository of the merged/closed PR IS the upstream, never a fork —
        gitbulk never pushes to a fork it does not own; (g) the PR is older
        than the grace period (node prgrc3kp). Open-PR head/base indexing
        uses an ALL-AUTHORS open-PR fetch, not my_open_prs (author:@me):
        someone else's open PR can depend on the branch just as easily.
      approved-by: daniel, 2026-06-03
      children:

        Prune Branches Incremental Plan = decision:
          id: prnpl3kq
          why: >
            prune-branches on the user's ~150-repo fleet took 15-30 min per
            run because its scan is sequential and per-branch: the dominant
            cost is one ``closed_prs_for_head`` REST call (gh.py) for every
            branch that clears the cheap guards — 1000-2000 serial
            subprocess+network calls. Worse, the expensive ANALYSIS and the
            cheap ACTION were fused into one non-reusable run: every
            invocation (dry-run OR --apply) minted a new run dir and
            repointed ``latest-prune-branches``, so a narrow ``--apply
            --repo X`` re-scanned only X and shadowed the broad dry-run
            report in ``show`` — forcing the user to re-run the 30-min scan
            to see what remained.

            Resolution (design doc docs/design/prune-branches-incremental.md,
            approved 2026-06-04): separate the analysis from the action by
            persisting a reusable PLAN in the run's existing state.yaml
            (schema 1 -> 2, schv4nrm). A dry-run writes the full plan (every
            delete-candidate as ``disposition: pending`` plus per-repo
            ``analyzed_at`` and a top-level ``scope_slugs``). ``--apply``
            reuses plan entries that are fresh and in scope, AUTO-RE-SCANS
            anything missing or stale (so a stale/absent plan makes --apply
            slow — accepted, chosen over refusing), deletes, then CARRIES
            THE FULL PLAN FORWARD into its own new run with only its scope's
            branches' dispositions updated. So multiple subset applies
            ACCUMULATE in one report and ``show`` always shows the whole
            plan with running dispositions. Subsetting is by repo/fleet
            filter only (no per-branch selection UI). The state.yaml run-dir
            home was chosen over a dedicated cache file: it reuses the
            existing artifact, locking (rsclk7nq), and GC with no new
            schema to version independently; retain_runs (default 30) keeps
            plans around plenty long.
          approved-by: daniel, 2026-06-04
          children:

            Prune Scope By Resolved Slug = decision:
              id: prnsc7nr
              why: >
                prune-branches filters are repo-level only (orgs/repo_globs,
                filters.py constrains_repos), so a run's "scope" is concretely
                the sorted SET OF SLUGS surviving select_repos — recorded as
                the plan's ``scope_slugs``. Reuse is therefore a per-repo
                resolved-slug membership + freshness test, NOT FilterSpec
                algebra: immune to glob-spelling differences (``*/origin-*``
                and ``provenant-dev/origin-*`` resolve to the same slugs).
                The "reuse iff the apply narrowed the focus, else re-scan
                what's missing" rule the user asked for falls out for free:
                a narrower apply hits only present+fresh slugs (full reuse,
                zero analysis network); a broader apply finds slugs absent
                from the plan and live-scans exactly those. No global subset
                gate is needed.
              approved-by: daniel, 2026-06-04

            Prune Branch SHA Is The Change Key = decision:
              id: prnsh5kp
              why: >
                state.yaml already records each branch's tip ``sha``, and
                list_branches returns current shas in one cheap paginated
                call, so no separate "last changed" date is needed — the tip
                SHA IS the exact change detector. When a repo is re-analyzed,
                a branch that clears the cheap guards AND whose sha equals its
                cached entry reuses the cached decision/pr/reason and SKIPS
                the expensive closed_prs_for_head + branch_ahead_by. Safe
                because the only verdict-changing directions are covered: the
                "used again" direction (a new open PR head/base) is caught by
                the cheap guards, which always re-fetch my_open_prs fresh;
                the "now deletable" direction (merged PR ages past prgrc3kp)
                only makes a delete MORE valid. The one verdict NOT cached as
                final is a grace-pending skip (prgrc3kp not yet met) — it is
                re-classified each run (cheap; the PR is already known) rather
                than reused, so it flips to delete exactly when grace elapses.
                Freshness is a per-repo ``analyzed_at`` vs ``--max-age``
                (policy default prune_plan_max_age_minutes = 720 / 12h);
                ``--force-scan`` ignores the plan. Both dry-run and apply
                honor the threshold.
              approved-by: daniel, 2026-06-04

            Prune Parallel Fetch = decision:
              id: prnpf8nq
              why: >
                The scan is read-only (deletes happen later, individually,
                under repo_lock per rsclk7nq res #7), so fan-out is safe
                against the locking model. Reuses the existing
                ThreadPoolExecutor fan-out (exec.py) — ordered result slots,
                progress callback, SIGINT handling — in two passes that keep
                the pool saturated: Pass A over repos (list_branches +
                my_open_prs + cheap guards + sha-cache reuse, yielding each
                repo's needs-classification branches), Pass B FLATTENED over
                all those branches (closed_prs_for_head + branch_ahead_by) so
                the dominant cost runs at full width regardless of how
                branches distribute across repos. Knob ``--concurrency N``
                (policy default prune_scan_concurrency = 12). The one real
                external risk is GitHub REST SECONDARY rate limits
                (concurrent-request + points/min); _run's retry/backoff
                already covers 5xx, and this work ADDS 403/429 + Retry-After
                handling so a wide pool degrades gracefully. Expected cold
                scan ~25min -> ~2-3min; warm/incremental -> low seconds.
                Cron-path-adjacent: warrants a live one-shot fleet shakedown
                (shkd5crn) before the default concurrency is trusted.
              approved-by: daniel, 2026-06-04

            Prune Safe Re-Validation On Apply = decision:
              id: prnrv6kq
              why: >
                Reusing an hours-old plan on --apply reintroduces a TOCTOU
                the always-fresh scan avoided, so each delete is re-validated
                against the governing facts IMMEDIATELY before acting,
                upholding prdls2nq. delete_branch_ref deletes by ref NAME not
                sha (the ref API has no compare-and-swap), so apply re-GETs
                the tip (new cheap branch_ref_sha) right before the DELETE and
                classifies the drift: branch already gone (someone else
                pruned it) is SAFE -> tolerate as ``already-gone`` success
                (idempotent); tip SHA moved (a push after the merge) is
                UNSAFE -> ``refused`` ("would lose work"); branch is now an
                open-PR head/base (reopened/new PR) is UNSAFE -> ``refused``
                ("used again"). Candidates are few by construction so the
                per-candidate GET is cheap; the residual sub-second GET->DELETE
                window is the inherent limit of the ref-delete API and no
                worse than today's final window. ``refused`` is
                attention-worthy (exit ladder -> EXIT_ATTENTION_NEEDED) because
                reality diverged from the plan unsafely; ``already-gone`` is
                quiet. This re-validation is the safety gate that any
                plan-reuse on apply (prnpl3kq) depends on and MUST land before
                or with reuse — never after.
              approved-by: daniel, 2026-06-04

            Prune Plan Reuse Versus Fresh Truth = tension:
              id: prntn9kp
              why: >
                The prune commands were designed (prnbr4kq, prdls2nq) on an
                always-fresh scan: every guardrail saw live state at decision
                time. Plan reuse (prnpl3kq) trades that freshness for speed,
                which is in direct tension with the data-loss guard's "never
                delete a ref whose deletion loses commits." The tension is
                RESOLVED, not merely accepted, by prnrv6kq: analysis may be
                reused (it only ever gets safer or is re-validated), but the
                DESTRUCTIVE act re-checks the two unsafe drift directions live
                before every delete. The residual is a sub-second TOCTOU
                inherent to the ref-delete API, identical to the pre-existing
                command. Binding consequence: no code path may delete a branch
                from a cached plan WITHOUT the prnrv6kq pre-delete re-GET.
              approved-by: daniel, 2026-06-04

    Prune Worktrees Subcommand = decision:
      id: prnwt5nq
      why: >
        ``gitbulk prune-worktrees`` removes LINKED git worktrees whose
        checked-out branch's only PRs are merged-or-closed — the local half
        of xq4npk7r's "orphaned worktrees" cleanup. Scope decision (chosen
        2026-06-03 over "only gitbulk-created worktrees under worktree_root"):
        discover ALL linked worktrees via ``git worktree list --porcelain``
        across every configured clone, so worktrees the user created by hand
        for PR work are pruned too, not only gitbulk's own dispatch/rebase
        leftovers. The narrower option was rejected because the user's stated
        goal is "delete local worktrees I made for closed/merged PRs," which
        the gitbulk-only scope would miss entirely.

        A worktree is removed only when ALL hold (skip-with-reason
        otherwise): (a) it is a LINKED worktree, NEVER the clone's primary
        working tree — the local-git safety contract (7mxr4pql) forbids
        touching the tree the user edits; path-verified the same way
        create_worktree verifies its target; (b) its working tree is clean
        — ``git status --porcelain`` empty; (c) it has no untracked files
        (treated as uncommitted; ``--include-untracked`` overrides); (d) it
        is NOT locked (``git worktree lock`` is an explicit "keep this");
        (e) it has no unique unmerged commits (node prdls2nq); (f) it is not
        detached / mid-rebase / mid-merge / in git-conflict state (reuse
        is_worktree_in_conflict) — states we cannot reason about safely;
        (g) its branch has a closed/merged PR association past the grace
        period (prgrc3kp). Removal uses ``git worktree remove`` WITHOUT
        --force (force would defeat guards b–c) then ``git worktree prune``;
        never rm -rf. After removing the worktree, the now-orphaned LOCAL
        branch is deleted too IFF it is fully merged (no unique commits),
        per the 2026-06-03 decision — symmetric with guard (e); a branch
        with unique commits is kept.
      approved-by: daniel, 2026-06-03
      children:

        Prune Worktrees Parallel Scan = decision:
          id: prnwpf9k
          why: >
            prune-worktrees had the same sequential-scan shape that made
            prune-branches slow (prnpf8nq), but a DIFFERENT dominant cost.
            Linked worktrees are few (most of the ~150 clones have zero), so
            the per-worktree ``closed_prs_for_head`` that dominated
            prune-branches is small here in aggregate. The actual hot spot
            was the PER-REPO ``my_open_prs([slug])`` call issued ONE REPO AT
            A TIME inside the scan loop — ~150 serial search round-trips —
            plus a serial ``git worktree list`` per clone.

            Two fixes, both mirroring existing patterns: (1) HOIST
            my_open_prs to a SINGLE batched call over all in-scope slugs
            before the loop (the gh client already chunks repo: qualifiers,
            50/search, gh.py _OPEN_PRS_REPO_CHUNK) — collapsing ~150 searches
            into ~3; (2) PARALLELIZE the scan with the same parallel_map
            fan-out prnpf8nq uses, in two passes: Pass A over repos
            (list_worktrees + list_local_branches, each under
            repo_lock(slug,"shared") per rsclk7nq res #6 — fcntl.flock opens
            a fresh fd per call so shared reads across worker threads are
            safe), Pass B FLATTENED over every linked worktree AND every
            worktree-less local branch (closed_prs_for_head + the local
            status/in-progress/unpushed reads, run lock-free exactly as the
            original per-worktree classification did). Knob ``--concurrency
            N`` reuses policy prune_scan_concurrency (12), shared with
            prune-branches. NOT ported from prune-branches: the reusable
            PLAN / freshness / SHA-cache machinery (prnpl3kq/prnsh5kp) —
            a worktree's verdict turns on VOLATILE local working-tree state
            (uncommitted edits, untracked files, mid-rebase) that no tip SHA
            captures, so a cached "clean" verdict would be unsafe to reuse;
            those checks are cheap and MUST be fresh. The prnrv6kq pre-delete
            re-validation is likewise unnecessary: removal already uses
            ``git worktree remove`` (no --force) and ``git branch -d``
            (merged-only), so git itself re-checks the governing facts at
            apply time. Apply stays SEQUENTIAL (local + fast; avoids parallel
            git mutation on one clone).
          approved-by: daniel, 2026-06-06

        Prune Worktrees Local Branch Sweep = decision:
          id: prnwlb7q
          why: >
            Gap the user identified 2026-06-06: a local branch created in a
            clone, pushed, merged, and switched away from accumulates forever
            — prune-worktrees only deleted a branch that was ATTACHED to a
            worktree it removed (the wtrm6kpq orphan-branch step), and
            prune-branches deletes only REMOTE refs. Neither swept a
            worktree-less local branch. Resolution: extend prune-worktrees so
            that, after the worktree pass, it also classifies every LOCAL
            branch (``git for-each-ref refs/heads``) that is NOT checked out
            in any worktree, applying the SAME PR/grace/unpushed guardrails as
            the worktree branch (open-PR head -> keep; needs a merged/closed
            upstream PR past prgrc3kp; no unpushed commits per prdls2nq) and
            deleting via ``git branch -d`` (merged-only — git refuses an
            unmerged branch AND the current branch, so the local-git safety
            contract holds; a kept branch is a valid outcome, never an error).

            PROTECTION IS REMOTE-DRIVEN, NEVER NAME-BASED (corrected
            2026-06-06 after a live dry-run proposed deleting local ``main``
            and ``dev`` in 17 repos): a first attempt guarded a hardcoded
            {main,master,dev} set, which is both unsafe (an integration branch
            can have any name) and wrong (a local ``main`` need not track
            origin/main). The guard instead resolves each local branch's
            UPSTREAM (``%(upstream:remoteref)``) and keeps the branch when that
            upstream is the repo's DEFAULT branch (cached from the prefetch) OR
            is PROTECTED on GitHub (the ``protected`` flag from list_branches,
            one call per candidate repo, mirroring prune-branches guard a/b in
            prnbr4kq). This guard applies to BOTH worktree branches and free
            branches via the shared classifier. If the remote protection can't
            be fetched for a repo, every candidate there is REFUSED (bias to
            safe). A branch with no upstream is not protected by this rule but
            still faces the PR/grace/data-loss gates.
            Branches checked out in a worktree are EXCLUDED from this pass
            because they are handled by the worktree pass (or kept with that
            worktree); this avoids double-processing and never touches the
            primary clone's current branch. DEFAULT-ON (still --apply-gated,
            still merged-only) per the user's choice — maximally useful for
            the unattended cron path — with ``--no-prune-local-branches`` as
            the opt-out for a worktrees-only run. Report rows carry a ``kind``
            ("worktree" | "branch") so the summary distinguishes a worktree
            removal from a bare local-branch deletion.
          approved-by: daniel, 2026-06-06

        Prune Force-Delete When All Commits Are On A Remote = decision:
          id: prnfd8kq
          why: >
            Gap the user observed 2026-06-06 on a real run (gitbulk 0.7.3): a
            dry-run promised "50 local branches would be removed" but --apply
            "deleted 47 of 50 local branches; 0 failed" — 3 vanished silently,
            counted neither as deletions nor as failures. Root cause: the
            CLASSIFY gate and the APPLY gate used different, non-equivalent
            tests for "safe to delete". A branch becomes a delete candidate
            (PR-merged path in _classify_branch_by_pr, and State-2a in
            _classify_no_pr) only when branch_unpushed_commit_count == 0, i.e.
            EVERY commit is reachable from SOME remote-tracking ref
            (``git rev-list --count <branch> --not --remotes`` == 0) — that IS
            the prdls2nq data-loss guard. But apply deleted via
            ``git branch -d`` (delete_merged_local_branch), and ``-d`` is a
            DIFFERENT test: git allows it only when the branch is an ancestor
            of its configured @{upstream} OR of HEAD. The two diverge exactly
            when the work landed on a remote ref OTHER than the branch's own
            upstream — squash/rebase merge, an auto-deleted remote head, or a
            stale LOCAL default branch that hasn't pulled the merge — so ``-d``
            refused branches whose deletion loses nothing. delete_merged_local_
            branch treats a refusal as a benign "kept", so the loss was
            invisible in the headline.

            Resolution (two parts):
            (1) HONEST REPORTING — a delete candidate whose branch the apply
            step does NOT delete is now counted in a distinct "kept: git
            refused" bucket and surfaced in the summary line, so the headline
            "deleted N of M" never silently drops the difference.
            (2) MATCH THE GATES — a candidate decided on the "no unpushed
            commits" basis (PR-merged path and State-2a) is flagged
            ``all_commits_remote`` and applied via a new
            delete_branch_all_commits_remote helper, which RE-verifies
            branch_unpushed_commit_count == 0 at delete time (defense in depth
            against a push racing in since classification, mirroring how the
            State-2b path re-verifies containment) and then force-deletes with
            ``git branch -D``. This does NOT weaken prdls2nq: the authoritative
            data-loss guard remains "every commit is on a remote", re-checked
            at apply time; ``-d`` was only ever a redundant — and, as found,
            wrongly-shaped — secondary check. State-1 (empty worktree contained
            in its local default) does NOT prove unpushed==0 and so KEEPS
            ``git branch -d`` (a genuine "merged into base" test); any residual
            ``-d`` refusal there now shows up via the part-(1) honest count.
            A genuine git error during the apply-time re-check raises
            WorktreeError and is recorded as a real failure, never a silent
            keep. Supersedes the prnpf8nq note that ``git branch -d`` lets git
            "re-check the governing facts at apply time" — for the all-commits-
            remote candidates that re-check is now ours.
          approved-by: daniel, 2026-06-06

        Prune Never Harvests Orphan Branches = decision:
          id: prnorph7
          why: >
            Gap the user identified 2026-06-13: prune-worktrees recommended (and
            with --apply would harvest) ORPHAN branches — branches deliberately
            detached from the default branch with NO commit in common. The
            motivating case is the ``tick`` ledger branch
            (https://github.com/dhh1128/tick), an orphan branch checked out in a
            ``.tick`` linked worktree, which the ``tick`` CLI pushes to
            ``origin/tick``. Trace: an orphan has no merge base with the default
            branch, so ``branch_ahead_behind`` reports ahead>0 (State-1 cannot
            fire) and ``branch_contained_in`` is false (State-2b cannot fire) —
            but EVERY commit is on ``origin/tick`` so
            ``branch_unpushed_commit_count == 0``, which together with reflog
            staleness makes State-2a (prdls2nq) classify it deletable. No git
            data is lost (the commits survive on the remote), but removing the
            ``.tick`` worktree and force-deleting the local ``tick`` branch
            destroys a working setup the user maintains on purpose and must
            re-create. The same path harvests an orphan ``gh-pages`` site branch.

            Resolution — TWO additive, bias-to-keep layers (a branch can only
            ever be KEPT by them, never deleted; neither weakens prdls2nq or any
            other guard):
            (1) STRUCTURAL — a new read-only helper
            ``worktree.branch_shares_history`` runs ``git merge-base
            refs/heads/<default> <branch>``: exit 0 → shares history (normal
            logic); exit 1 (git's "no merge base") → UNRELATED/orphan → keep;
            any other exit → can't verify → keep (mirrors "could not verify
            remote branch protection"). Wired into _classify_branch_by_pr right
            after the open-PR-head gate and BEFORE the closed-PR network lookup,
            so an orphan short-circuits the gh call. It runs only when the
            default branch is known; the precision is high because a normal
            feature branch ALWAYS descends from the default branch's history, so
            only deliberately-orphaned special-purpose branches lack a merge
            base (near-zero false positives). One cheap local merge-base per
            surviving candidate, negligible next to the per-branch
            closed_prs_for_head network call already made.
            (2) NAME-BASED — ``gh-pages`` and ``tick`` JOIN the
            universally-sacred SACRED_BRANCH_NAMES set in _common (chosen
            2026-06-13 by the user over a separate config key, since the existing
            sacred mechanism already exists and is shared with prune-branches, so
            the names are protected from REMOTE deletion too). This is the
            fallback for the two gaps the structural layer leaves: a NON-orphan
            ``gh-pages`` that was branched off the default (shares history), and
            the case where the default branch can't be resolved so merge-base
            cannot run. No opt-out flag: harvesting an unrelated-history branch
            is never desired; the user removes such a branch by hand if ever
            needed.
          approved-by: daniel, 2026-06-13

    Prune Grace Period = decision:
      id: prgrc3kp
      why: >
        Both prune commands ignore a branch/worktree whose PR was
        merged-or-closed more recently than ``prune_min_age_days`` (default
        7, per-repo overridable like every other Defaults knob). Rationale:
        a just-merged branch is the one most likely to still be wanted — a
        hotfix off it, a deploy that references it, a revert in flight — and
        the whole point of running these without --dry-run (the user's
        stated intent) is high confidence, which a cool-off buys cheaply.
        7 chosen over 0/1: a week comfortably outpasses same-day churn and
        any reasonable CD/rollback window while still clearing the backlog.
        The grace period is measured from the PR's mergedAt (merged) or
        closedAt (closed), not the branch's last-commit date, because PR
        lifecycle — not commit recency — is what "associated with a
        closed/merged PR" means.

        Both prune commands also accept ``--min-age-days DAYS`` (2026-06-13):
        a per-run override meaning "instead of the default N". It rewrites
        ``defaults.prune_min_age_days`` only (via
        ``_common.apply_prune_min_age_override``), so the whole classifier
        chain and the run's config snapshot see the effective grace with no
        extra plumbing. It deliberately does NOT win over an explicit per-repo
        ``prune_min_age_days`` override: a configured per-repo grace is a
        stronger, usually-SAFETY statement of intent (a longer cool-off) and an
        ad-hoc CLI flag must not silently shorten it. DAYS is a non-negative int
        (0 removes the grace); a negative is a clean argparse usage error.
      approved-by: daniel, 2026-06-03

    Prune Data-Loss Guard = decision:
      id: prdls2nq
      why: >
        The unifying safety principle across both prune commands: never
        delete a ref or worktree whose deletion would lose commits that
        exist nowhere else. Concretely, a remote branch is prunable only if
        its tip is EITHER the exact head SHA GitHub recorded for the merged
        PR (nothing was pushed after the merge) OR reachable from the
        repo's default branch (fully merged). A local worktree's branch is
        prunable only if every commit on it is reachable from the merged PR
        head or from the remote/default branch. This is what makes acting on
        CLOSED-but-unmerged PRs safe (the 2026-06-03 "merged + closed, data-
        loss guarded" choice): an abandoned closed branch is pruned only
        when it turns out to hold no unique work, otherwise it is kept with
        a reason. It also handles squash/rebase merges, where the PR head
        SHA is NOT an ancestor of default: the recorded-head-SHA arm
        accepts them via GitHub's authoritative "merged" signal rather than
        an ancestry test that would (wrongly) report data loss. On any
        inconclusive check (gh/git error, unknown SHA) the guard fails
        closed → skip.
      approved-by: daniel, 2026-06-03

    Prune Deletes Remote Branch Via Ref API = decision:
      id: prdel4rq
      why: >
        prune-branches deletes a remote branch with
        ``gh api -X DELETE repos/{slug}/git/refs/heads/{branch}``, not
        ``git push origin --delete``. Three reasons: (1) it keeps the
        command clone-free (like merge), so a missing/dirty local clone
        never blocks remote cleanup; (2) ``git push --delete`` is exactly
        the destructive push mode the .agent-bin shim blocks during AI
        development, whereas the ref API is the documented, scriptable path
        for unattended deletion; (3) it is one network call with a clear
        success/refusal signal. Verified non-deprecated against the gh CLI
        at wiring time per AGENTS.md. GitHub still permits restoring a
        deleted branch of a merged PR from its UI, and gitbulk records the
        deleted SHA in run state first, so the operation is recoverable.
      approved-by: daniel, 2026-06-03

    Worktree Removal Extends The Safety Contract = decision:
      id: wtrm6kpq
      why: >
        The local-git safety contract (7mxr4pql) enumerates only read-only
        ``git -C <clone>`` subcommands as permitted. prune-worktrees needs
        one more mutating local operation: ``git worktree remove`` of a
        LINKED worktree (and the subsequent local-branch delete for a
        fully-merged branch). This is consistent with the contract's intent
        — it never touches the working tree, index, HEAD, or current branch
        of the PRIMARY clone the user edits; it removes a SEPARATE linked
        worktree and prunes administrative metadata. The contract text (and
        AGENTS.md) is extended to bless exactly this, gated on the same
        path-verification create_worktree uses (target must resolve to a
        real linked worktree of the clone, never the main worktree path).
        Rejected: leaving prune-worktrees to shell out to rm -rf outside the
        contract — that would bypass git's own dirty/lock refusals and the
        worktree admin cleanup, i.e. strictly less safe.
      approved-by: daniel, 2026-06-03

    # ─── WORKTREE & LOCAL CLONE HANDLING ─────────────────────────────────────

    Worktree Root Under XDG Cache = decision:
      id: mw6kp2nq
      why: >
        Default worktree root is
        ~/.cache/gitbulk/worktrees/<runid>/<owner>__<repo>/.
        Configurable via gitbulk.yaml worktree_root. Rejected /tmp/gitbulk/
        because a crashed dispatch loses its worktree before the user can
        examine it the morning after; rejected per-repo
        .git/gitbulk-worktrees/ because it pollutes user-owned clone dirs
        and forces gitbulk gc to walk every clone. ~/.cache/ honors XDG,
        puts all gitbulk disposable state in one place, and survives
        reboot for crash forensics.

    Missing Local Clone Skips With Warning = decision:
      id: 5xqp2nkr
      why: >
        The local.exists invariant is in the chain only for subcommands
        that need a clone (rebase-onto-default, merge, dispatch); report
        and summarize do not run the check at all because they operate
        purely on gh data. When a repo configured in repos.txt has no
        clone under ~/code/, the affected subcommand skips that repo,
        logs a WARNING, and the run contributes to exit code 3.
        Rejected: auto-clone (slow first run, grows disk silently) and
        fail-the-run (one missing repo would tank the other 149). General
        principle from design-notes §11: bias toward skip-with-reason-
        logged over do-something-risky.

    Rebase Conflicts Persist The Worktree = decision:
      id: vp7n2krq
      why: >
        When rebase-onto-default produces a conflict, the worktree is
        left in-place with conflict markers and a CONFLICT.md is written
        into the run directory containing the absolute worktree path and
        suggested fix-up commands. gitbulk gc (Phase 5/6) GCs worktrees
        older than N days unless they are in git-status conflict state.
        Rejected: tear down the worktree on conflict, which would force
        the user to recreate it manually before resolving — extra
        friction at exactly the moment when the user needs the
        lowest-friction path forward.

    Dispatch Surfaces Agent Verdict And Salvages Escalations = decision:
      id: dspesc4q
      why: >
        Two gaps surfaced by the first live run of the resolve-conflicts
        dispatch prompt (2026-06-01; all 5 conflicting PRs escalated
        cleanly). Both are about the run's durable artifacts telling the
        true story without spelunking per-target logs.

        GAP 1 — outcome visibility. A dispatched agent's real result
        (e.g. the resolve-conflicts prompt's final ``RESOLVED:`` /
        ``ESCALATED:`` line) lived ONLY in
        ``<run>/dispatch-logs/<key>.stdout.log``. summary.md/state.yaml
        showed only the PROCESS status ("completed (exit 0)"), so an
        escalation read as a success. Fix: ``_parse_agent_outcome`` lifts
        the last ``RESOLVED:``/``ESCALATED:`` line (tolerating backtick
        wrapping) from the agent's stdout; the verdict + normalized line
        land in state.yaml (``outcome`` / ``outcome_detail``) and replace
        the bare process status per-PR in summary.md, with a
        ``Resolved: N  Escalated: M`` tally. A missing/garbled line
        degrades to "unknown" (the process status), never a crash. This
        is a deliberate convention: the agent's contract is to END with
        exactly one such status line.

        GAP 2 — escalation note salvage. The resolve-conflicts prompt
        escalates by running ``git rebase --abort`` then writing
        ``ESCALATION.md`` in the worktree. But abort leaves the worktree
        NOT in a git-conflict state, so the vp7n2krq teardown rule
        ("preserve only if in git-status conflict") removed it — taking
        ``ESCALATION.md`` with it. Fix: ``_salvage_escalation`` copies any
        worktree ``ESCALATION.md`` into ``<run>/escalations/<key>.md``
        BEFORE the teardown/preserve decision, so the reason survives
        regardless. Chosen over "always preserve the worktree on
        escalation": a cleanly-aborted worktree holds no mid-rebase state
        worth keeping, and preserving it would just accrue disk; the
        durable run dir is the right home for the note. vp7n2krq's
        preserve-on-git-conflict behavior is unchanged. Best-effort: a
        failed salvage returns None rather than aborting the finalizer for
        the other PRs.

        Found-and-fixed same day; backlog note in global memory
        ``project-dispatch-escalation-gaps``.

    # ─── ON-DISK CONVENTIONS ─────────────────────────────────────────────────

    CLI Wiring Phase 1C = decision:
      id: clip7nm4
      why: >
        cli.py grows real handlers for two subcommands that no longer
        live in the "not yet implemented" pool:

          `gitbulk invariants` — reads
          gitbulk.invariants.all_invariants() and prints one line per
          registered invariant: "name  [kind]  applies-to: <subs>".
          When the registry is empty (Phase 1C state — concrete
          invariants land in Phase 2+), prints a brief explanatory
          message and returns 0.

          `gitbulk ack` — calls sentinel.clear_attention() and reports
          "cleared" or "no sentinel was set"; returns 0 either way.

        Exit-code → ATTENTION wiring in main():
          - On exit 2 (EXIT_ATTENTION_NEEDED) or 3 (EXIT_INVARIANT_SKIPPED):
            if no sentinel is already present, write a fallback sentinel
            with subcommand + "?" runid. Phase 2+ handlers will pre-empt
            this by writing their own richer sentinel with the real
            runid before returning.
          - Exit 4 (EXIT_OVERRIDES_APPLIED) does NOT trigger ATTENTION
            per design-notes §8 — it's an audit signal logged in
            invariants.log, not user-visible attention-needed state.
          - Subcommand handlers that return 0 do NOT clear ATTENTION;
            clearing is explicitly the user's gesture via `gitbulk ack`,
            because a 0-exit on one subcommand does not necessarily
            mean every concern from a previous subcommand has been
            resolved.
            REFINED by node aklr5pq3 (2026-06-03): same-subcommand 0-exit
            supersession and view-the-flagged-run clearing are now safe
            implicit triggers; the cross-subcommand concern stated here is
            preserved (those cases still need `ack`).

        Subcommand stubs for report/summarize/dispatch/merge/
        rebase-onto-default/close-stale/show continue to return 99
        until their respective phases (2-5) implement them; main()'s
        wiring does not change for those.
      approved-by: daniel, 2026-05-27

    Invariants Framework Implementation = decision:
      id: ivp4wq7n
      why: >
        invariants/ implements the policy-as-named-chains model from
        node c4jzm5pn. Three internal files:

          invariants/base.py
            - Result types: three frozen dataclasses Pass, Skip(reason),
              Fail(reason). Type alias Result = Pass | Skip | Fail.
              Consumer call sites use isinstance dispatch (no match
              statement requirement; works on 3.10+).
            - InvariantKind enum: UNIVERSAL, PER_REPO, PER_PR. Per-
              subcommand applicability is a separate ClassVar
              (frozenset[str]) — kind says WHAT scope, subcommands
              says WHICH subcommands.
            - InvariantContext frozen dataclass: policy, runstate,
              repo (optional), pr (optional, typed Any until Phase
              2's PRInfo), gh (optional, Any until Phase 2's GHClient).
              Universal invariants leave repo/pr/gh as None; per-repo
              invariants assert ctx.repo is not None inside check().
            - Invariant ABC with ClassVar name/kind/subcommands and
              abstract check(ctx) -> Result.

          invariants/registry.py
            - Module-level dict _REGISTRY: dict[str, type[Invariant]].
            - @register decorator: raises ValueError on duplicate name.
            - get(name), all_invariants() (returns shallow copy),
              for_subcommand(sub) -> list filtered by subcommands set.
            - _clear() test-only helper. Tests use a fixture to
              snapshot+restore around each test so registrations don't
              leak.

          invariants/runner.py
            - ChainResult frozen dataclass:
                passed: bool
                fail_reason: str | None
                skips: tuple[tuple[str, str], ...]   # (name, reason)
            - run_chain(invariants, ctx, *, skip_set, target) iterates,
              recording each outcome to ctx.runstate.record_invariant.
              Stops on first Fail. Per the user choice this session:
              an invariant that RAISES is caught and converted to Fail
              (the run aborts), with the traceback summary captured in
              ctx.runstate.record_error so the audit trail is debuggable.
              A non-Result return raises TypeError (programmer bug).

        Override semantics from node r4nzp7kq (cmdline-wins) are
        NOT enforced inside run_chain itself; the caller (CLI
        handler, Phase 2) computes the effective skip_set from
        config + cmdline before calling. This keeps run_chain a
        pure executor and makes the override-audit point a single
        location in the CLI layer instead of leaking through here.

        Invariant kinds drive WHICH chain a CLI handler builds and
        in what order (universal once, then per-repo, then per-PR).
        Phase 1C delivers the framework only; concrete invariants
        from the design-notes catalog land in Phase 2 onward.
      approved-by: daniel, 2026-05-27

    Dashboard Composition = decision:
      id: dwq3kpn4
      why: >
        dashboard.py rewrites ~/.cache/gitbulk/dashboard.md to be
        the single-screen view of recent gitbulk state per node
        tp4kq2nr.

        API:
            rewrite_dashboard() -> Path
        Returns the path written, for ergonomic chaining.

        Discovery: scans paths.runs_dir() for symlinks named
        `latest-<subcommand>` (these exist only after a successful
        runstate.complete() per kp7nw4mq.e). For each, reads
        manifest.yaml (timestamps, exit_code) and the first ~15
        lines of summary.md. A subcommand with no latest-* symlink
        gets a "no runs yet" placeholder.

        Markdown structure: one H2 section per subcommand; first
        line per section is metadata
        ("Run: <runid>  Exit: <code>  Completed: <iso>"), followed
        by an excerpt of summary.md fenced or as a blockquote.

        Incomplete runs (manifest.completed_at missing) are marked
        with an explicit "**[INCOMPLETE]**" tag so the user sees
        crashed cron runs immediately. This is the layer-3 visibility
        property that lets ATTENTION sentinel and dashboard.md
        together explain "what's gitbulk doing right now?" without
        opening any run directories.

        Atomic write via tmp + os.replace, same pattern as runstate.

        Excerpt length: hardcoded to ~15 lines; truncation marker
        "... (truncated; see <run-dir>/summary.md)" appended if
        longer. Phase 6 polish can make this configurable.
      approved-by: daniel, 2026-05-27

    ATTENTION Sentinel API = decision:
      id: snk7p4qm
      why: >
        sentinel.py manages ~/.cache/gitbulk/ATTENTION per node
        tp4kq2nr (the file-based notification layer).

        API:
            set_attention(exit_code, subcommand, runid, summary) -> None
            clear_attention() -> bool   # True if a sentinel was removed
            has_attention() -> bool
            read_attention() -> str | None

        File contents: a single line of the form
            "{exit_code} {subcommand} {runid} {summary}"
        e.g. "2 report 20260527T194501Z 4 PRs need attention". The
        format was chosen so `cat ~/.cache/gitbulk/ATTENTION` shows
        WHY the prompt glyph is active without needing to run
        `gitbulk show`.

        clear_attention's bool return lets the `ack` subcommand
        report "cleared" vs "was already clear" without raising.

        No locking: the sentinel is written by exactly one subcommand
        process at a time (guaranteed by the global lock acquired
        upstream). read_attention silently returns None if the file
        is missing — `has_attention` is the explicit existence test.
        UPDATE (rsclk7nq, 2026-06-03): the global lock is retired, so this
        "exactly one writer" guarantee no longer holds for free. set/clear
        now run under sentinel_lock() and set_attention is made atomic
        (tmp+os.replace) so external readers never see a torn line.

    Implicit ATTENTION clearing = decision:
      id: aklr5pq3
      why: >
        Refines clip7nm4's "only `gitbulk ack` clears" stance. clip7nm4
        rejected clearing on a 0-exit because "a 0-exit on one subcommand
        does not necessarily mean every concern from a previous subcommand
        has been resolved." That objection is real but narrower than the
        blanket rule it produced: it only argues against CROSS-subcommand
        clearing. Within the same subcommand, and when the operator
        demonstrably views the flagged run, an implicit clear is safe and
        removes friction (the alternative — a stale yellow glyph that
        outlives its cause until a separate `ack` — trains the operator to
        ignore the glyph).

        Three implicit-clear triggers are added; `ack` remains the
        unconditional hammer (clears any sentinel, including a corrupt or
        legacy-format one):

          1. `gitbulk show <sub>` (any artifact, including --path) clears
             the sentinel IFF it was set by the run being viewed: the
             sentinel's subcommand == <sub> AND its runid == the runid of
             the resolved `latest-<sub>` run. A "?" fallback runid (written
             by _maybe_set_attention when a handler did not record its own)
             never matches — those still require `ack`. Viewing a DIFFERENT
             subcommand's run never clears (preserves clip7nm4's concern).

          2. `gitbulk show` with no arg (the dashboard) clears WHATEVER
             parseable sentinel is present. The dashboard aggregates every
             subcommand's latest-run summary, so it is the broad "I looked"
             gesture; weaker evidence than (1) but the user accepted it.
             An unparseable/legacy sentinel is left for `ack` (the note
             needs a parseable payload to describe what was cleared).

          3. A clean (exit 0) run of an attention-PRODUCING subcommand
             (report, summarize, dispatch, merge, rebase-pr, close-stale —
             marked by Subcommand.sets_attention) supersedes a sentinel the
             SAME subcommand set earlier: the condition that raised the
             alert has resolved. Cross-subcommand sentinels are left intact
             (a clean `report` must not dismiss a `dispatch` failure). Only
             exit 0 supersedes — exit 1/2/3 means the run itself did not
             complete cleanly, so it cannot claim the prior concern is gone.

        sentinel.py gains clear_if_matches(subcommand, runid),
        clear_if_superseded(subcommand), and clear_and_describe(); each
        returns the cleared payload (or None) so callers can emit a
        one-line note. Notes go to STDERR in `show` so they never corrupt
        an artifact piped from stdout (e.g. `gitbulk show report --state |
        yq`). Clearing keeps `show` mutating=False: per clip7nm4 the
        sentinel is local-cache state, and `ack` itself is mutating=False
        despite deleting it; "mutating" in gitbulk means touches GitHub.
        `show` already holds the shared global lock, which is what guards
        the read+clear against a concurrent exclusive run swapping the
        symlink.
        UPDATE (rsclk7nq, 2026-06-03): with global_lock retired, `show`
        guards the symlink read via run_state_lock(sub, SH) and the
        sentinel read+clear via sentinel_lock().
      approved-by: daniel, 2026-06-03
      supersedes-aspect-of: clip7nm4

    Policy Config Loader Schema = decision:
      id: ck5pwr2n
      why: >
        config/policy.py parses ~/.config/gitbulk/gitbulk.yaml into a
        frozen Policy dataclass tree:
          Policy(defaults: Defaults, humans: HumansConfig,
                 bots: tuple[str, ...],
                 repos: dict[str, RepoOverride],
                 worktree_root: Path)

        Plus a helper:
          policy_for(policy, slug) -> Defaults
        returning the effective defaults for a repo after applying any
        per-repo override.

        Conventions:

        (a) Validation library: dataclasses + hand-rolled type and
        enum guards. No pydantic. Rationale: PyYAML is already the
        only runtime dep; adding pydantic (10+ MB) for a ~30-line
        schema is over-engineering for a personal tool. The
        validation helpers (_ensure_int, _ensure_str with allowed
        values, etc.) are reused across the parser.

        (b) Unknown keys at any level (top-level, defaults, humans,
        per-repo override) raise ConfigError. Typos like
        min_buisness_days are usually mistakes; loud failure beats
        silent acceptance. Forward-compat unused-but-recognized keys
        ("notifications") are added to the allow-list explicitly.

        (c) Per-repo override semantics:
          - Scalar fields use the per-repo value if non-None, else
            inherit from defaults.
          - List fields (skip_checks, extra_checks) APPEND to
            defaults rather than replace. defaults.skip_checks +
            repos.X.skip_checks = effective skip_checks for X.
            Rationale: "add this exception just for X" is the
            common case; wholesale replacement would silently drop
            project-wide exceptions when a per-repo entry exists.

        (d) Missing file or empty file returns Policy() with all
        documented defaults from this.i. A brand-new user with no
        gitbulk.yaml gets a working tool.

        (e) Defaults pinned by this.i (not invented here):
          merge_policy default "strict"        - design-notes §2
          min_business_days = 3                - bg4pqn7m
          unresolved_burden = "me"             - hj3nq5kp
          bot_threads_block = True             - zk3r4nqp
          worktree_root default                - mw6kp2nq

        (f) worktree_root is expanded for ~ (user-friendly) and
        defaults to paths.default_worktree_root(). Per-run usage
        of the worktree root happens in Phase 4 (dispatch); for
        Phase 1 the value is just held in the Policy object.

        (g) Removed fields from the Phase-0 example: default_branch_only
        (it is an invariant property, not a tunable — pr.base_is_default
        is always enforced), dispatch_concurrency (Phase 4 concern;
        the in-tree execution kernel owns concurrency per execk7nm),
        min_age_days (renamed to min_business_days per bg4pqn7m).
        config/gitbulk.yaml.example is updated in the same commit
        as the loader so the documented schema is never out of sync.
      approved-by: daniel, 2026-05-27

    Repos Config Loader API = decision:
      id: rj4pwn7k
      why: >
        config/repos.py parses ~/.config/gitbulk/repos.txt into a list
        of RepoEntry records used by every subcommand.

        API: load_repos(path: Path | None = None,
                        code_root: Path | None = None) -> list[RepoEntry]
        RepoEntry: frozen dataclass with slug, owner, name,
                   local_path, source_line.

        Conventions:

        (a) Format: one "owner/repo" per line, optionally followed by
        a "#" comment; blank lines and pure-comment lines ignored.
        Decision rationale lives in ws2pn4kr (two-file config).

        (b) Malformed slug raises ConfigError with file path + line
        number. Loud failure beats silent skip because a typo in
        repos.txt should never silently exclude one of 150 repos —
        the user's mental model says "everything in repos.txt is
        managed by gitbulk" and a silent drop violates that
        invariant.

        (c) Duplicate slugs: keep the first occurrence, log a
        WARNING via logging.getLogger("gitbulk.config"). Not an
        error because the most common cause is editorial — copying
        a line and forgetting to delete the original; gitbulk should
        not block the nightly cron over it.

        (d) Local clone path resolves to `code_root / basename(repo)`
        — e.g., dhh1128/gitbulk → ~/code/gitbulk. code_root defaults
        to Path.home()/"code"; the loader takes an explicit override
        so the future --code-root CLI flag has a clean injection
        point. Note: only `basename(repo)` is used; the owner is
        intentionally discarded for the local path because the
        user's clones are organized as flat siblings under ~/code/,
        not nested per-owner.

        (e) RepoEntry.source_line is preserved so error messages
        downstream ("invariant X failed for owner/repo (configured
        in repos.txt:42)") can cite the original config location.

        Excluded for now: per-line inline tags or YAML-extended
        repos.txt formats — explicitly rejected in ws2pn4kr in
        favor of repos.txt simplicity.
      approved-by: daniel, 2026-05-27

    Run State Module Schema And API = decision:
      id: kp7nw4mq
      why: >
        runstate.py manages the per-run audit trail. A RunState
        object owns one directory at
        runs_dir()/<timestamp>-<subcommand>/ and is the single place
        every subcommand records its decisions.

        Public API is class-based — a run is a long-lived stateful
        thing, not a function:

            RunState.begin(subcommand, argv, config_snapshot)
            rs.record_invariant(name, target, result, reason)
            rs.record_error(message, *, level, context)
            rs.record_repo_state(slug, payload)
            rs.set_repos(repos)
            rs.record_extra(key, value)
            rs.flush_state()           # node 7gpd: deferred state.yaml write
            rs.record_timings(mapping) # node 5agg: per-phase wall-clock
            rs.write_summary(markdown)
            rs.complete(exit_code)
            rs.run_dir (property)

        Schema decisions:

        (a) manifest.yaml carries the full inline config snapshot —
        the parsed gitbulk.yaml contents plus the contents of
        repos.txt — not just a hash. Rationale: gitbulk.yaml lives at
        ~/.config/gitbulk/ which is NOT in git; a hash without the
        snapshot becomes irrecoverable on the next config edit. Cost:
        10–50 KB per run for a 150-repo config; at ~100 runs/year
        that is ~5 MB/year. Reproducibility of any past decision is
        the load-bearing benefit.

        (b) invariants.log and errors.log are JSON Lines (one JSON
        object per line). Each event carries a UTC timestamp plus
        event-specific fields. Rationale: machine-parseable for
        `gitbulk show`, ad-hoc `jq` queries, and any future
        dashboard. Not directly cat-friendly; users use `gitbulk
        show` or `jq` to render. Accepted because the audit trail's
        primary consumer is tooling, not eyeballing.

        (c) state.yaml is rewritten atomically (write to .tmp,
        rename over). Crash safety: begin() writes an empty
        {repos:{}} immediately, and the file is rewritten in full at
        finalization, so a crashed run still leaves a parseable file.
        Append-style YAML would be smaller but harder to validate
        after a partial write.
        UPDATE (7gpd / PERF-F1, 2026-06-08): the original design
        rewrote the WHOLE state dict on EVERY record_repo_state /
        record_extra / set_repos call. At 150-205 repos that is N
        full-file dumps of a growing dict — O(n^2) work plus N
        fsync-class writes per run. record_repo_state/record_extra/
        set_repos now only accumulate in memory and mark the snapshot
        dirty; the single on-disk write happens in flush_state()
        (called from complete(), or explicitly at a phase boundary by
        a caller that wants an intermediate checkpoint). This is O(n).
        Tension with the original per-call durability: RESOLVED by
        observing that the mutating-action audit (merges, force-pushes,
        branch deletions) is appended LIVE to errors.log/invariants.log,
        not state.yaml — state.yaml is only the post-action per-repo
        summary, which today's callers already write in a tight loop
        immediately before complete(). Deferring that summary's write to
        a single flush therefore trades negligible crash-resilience
        (the begin() empty write keeps the file parseable; a crash
        mid-run loses only the in-memory summary, recoverable from the
        live logs) for eliminating the O(n^2) amplification.

        (d) manifest.yaml is rewritten on complete() to add
        completed_at and exit_code. A missing completed_at field
        flags an incomplete/crashed run to `gitbulk show`. Atomic
        via tmp+rename.

        (e) latest-<subcommand> symlink is updated only at
        complete(). A run that crashed never becomes "latest"; the
        previous successful run remains the latest until a new run
        completes. Atomicity: create latest-<sub>.tmp pointing at
        the new run, then rename over the existing latest-<sub>.
        Symlink target is RELATIVE (just the run dir's basename) so
        the cache dir is relocatable without breaking symlinks.

        (f) Append-only logs use buffered file.write() with no
        locking. Events from a single subcommand process are written
        serially by that process. Concurrent gitbulk runs each have
        their own run directory, so no cross-process log contention
        exists.

        (g) record_invariant's `result` argument is one of "PASS",
        "SKIP", or "FAIL" (uppercase strings); these mirror the
        chain-runner Pass/Skip/Fail outcomes from node c4jzm5pn but
        are serialized as strings to keep YAML/JSON simple.
        record_invariant validates and raises ValueError on other
        values.

        (h) RunState does NOT take any lock on the run directory.
        Subcommands acquire global_lock() in their CLI handler
        before calling RunState.begin(). runstate.py and locks.py
        have no circular dep; locks.py is the lower layer.
        UPDATE (rsclk7nq, 2026-06-03): global_lock is retired. RunState
        still takes no lock and runstate.py still does not import locks.py;
        but handlers now acquire resource-scoped locks around specific
        sections — RunState.complete() runs under run_state_lock(sub, EX).
      approved-by: daniel, 2026-05-27

    Locks Module API And Semantics = decision:
      id: hk5pq3nm
      why: >
        locks.py implements the two-lock concurrency model from node
        lj5pqn4kr using fcntl.flock POSIX advisory locks. Public API
        is two context managers:

            global_lock(mode, *, timeout=None, subcommand=None)
            repo_lock(slug,  *, timeout=None, subcommand=None)

        Conventions:

        (a) Library: fcntl.flock from the Python stdlib. No external
        dep. gitbulk targets Linux (constraint 6jz4n2pq plus the
        cron-on-Linux deployment model); Windows is not a goal. Cost:
        no native Windows support; accepted explicitly.

        (b) Mode is a string enum ("shared" | "exclusive") at the API
        surface, mapping to LOCK_SH / LOCK_EX inside the
        implementation. Reads as prose at call sites and is
        extensible if upgrade patterns appear later.
        repo_lock takes no mode — always exclusive (per lj5pqn4kr).

        (c) Acquisition blocks forever by default AT THE LIBRARY
        LEVEL; an optional timeout kwarg (float seconds) lets
        callers fail fast with a LockTimeoutError. Timeout
        implementation: poll with LOCK_NB + sleep(min(0.1,
        remaining)); raises LockTimeoutError including the holder's
        metadata when the deadline elapses.

        **However, the library default is NOT the right default
        for CLI subcommands.** Every CLI subcommand handler MUST
        pass an explicit bounded timeout (per Phase 2 CLI Lock
        Timeout Policy, node tmlk5pq3). Rationale for the layered
        split: in-process tests and ad-hoc scripts may legitimately
        want indefinite waits; the CLI is the layer that knows the
        cron-overlap failure mode, so the CLI owns the bounded-
        timeout responsibility. The platform-architect adversarial
        review (2026-05-27) identified that without explicit CLI
        timeouts a stuck process holding the global exclusive lock
        silently parks successive cron invocations — the new
        process never reaches the ATTENTION-setting code path, so
        the user discovers the situation only when the third or
        fifth nightly run has failed to produce expected artifacts.
        The two-layer split (permissive library default, mandatory
        CLI bound) prevents this without restricting non-CLI use.

        (d) Lock files persist empty after release rather than being
        deleted. Their purpose is to be a stable fcntl target;
        deletion would create an unlink/create/lock race on next
        acquire.

        (e) The holder writes JSON metadata to the lock file on
        acquire:
            {"pid": int,
             "started_at": ISO-8601 UTC,
             "subcommand": str | None}
        Rationale: `cat ~/.cache/gitbulk/run.lock` shows who holds
        the lock at a glance, which is the practical debugging win
        when a stuck process keeps the next cron run waiting. A
        future `gitbulk locks` subcommand (Phase 6) can scan
        locks_dir/ and report systematically. Reset of metadata on
        release: lock file is left in place but its contents reflect
        the most-recent holder; on release the fd is just closed,
        which implicitly releases the flock per POSIX semantics.

        (f) Reentrancy is not supported: a process re-acquiring the
        same lock has undefined behavior (documented, not asserted).
        gitbulk's subcommand-per-process architecture never
        re-acquires.

        (g) Lock-event logging via logging.getLogger("gitbulk.locks");
        cli.py later wires handlers to route into the run dir's
        invariants.log. locks.py does NOT depend on runstate
        directly (which would be circular — runstate uses locks).

        (h) Tests use mocked fcntl.flock for argument verification
        (LOCK_SH / LOCK_EX / LOCK_NB), plus a small set of
        subprocess-spawned tests for genuine inter-process
        contention, since fcntl.flock is per-process and threads in
        the same process all see the lock as held.
      approved-by: daniel, 2026-05-27

    Resource Scoped Locking = decision:
      id: rsclk7nq
      why: >
        Supersedes the two-lock model of lj5pqn4kr and extends the locks
        API of hk5pq3nm. Resolves tension rlkrcn3p. Full spec and
        per-command critical-section map: docs/design/resource-scoped-
        locking.md.

        PRINCIPLE: lock the RESOURCE, not the operation. A lock protects a
        specific piece of shared state; its scope is exactly that state, no
        wider. Contention follows data, not command identity. The old
        single global_lock (held for an entire run) conflated three jobs —
        serializing mutators, guarding per-subcommand run-state for readers,
        and masking writer-vs-writer cache/sentinel races — which is why a
        read-only `show prune-worktrees` blocked on a `prune-branches` run
        that touched disjoint state (rlkrcn3p resolution).

        SHARED RESOURCES AND THEIR LOCKS (all built on hk5pq3nm's
        _file_lock; lock files under locks_dir()):

          run_state_lock(sub, mode)  runstate-<sub>.lock   EX writers / SH show
            Guards `latest-<sub>` symlink swap + gc.prune_runs(<sub>) vs
            readers (show <sub>, dashboard). Keyed by SUBCOMMAND, so
            show of subcommand X never contends with a run of subcommand Y.

          repo_lock(slug, mode)      <slug>.lock           EX mutate / SH read
            The previously-dead repo_lock, now ACTIVATED. Unifies resources
            #6/#7/#8: the local clone (refs, .git/worktrees, index) AND
            remote repo mutations (merge/close/branch-delete/head-push) for
            one repo. Governing rule: any git invocation against clone
            <slug> holds it (SH for read-only git, EX for mutating git);
            any remote mutation to <slug> holds it EX. NOTE: hk5pq3nm.b
            said repo_lock "takes no mode — always exclusive"; that is now
            relaxed to shared|exclusive so clone-preflight READS
            (local.exists/remote_matches in rebase-pr/dispatch/prune-
            worktrees) can take it shared.

          default_branches_lock()    default-branches.lock EX
            Around prime_default_branches' load->merge->save. Mandatory:
            the merge drops/keeps entries, so a concurrent run can resurrect
            a deleted branch or lose a fetch (a real lost update, not benign).

          org_lock(org)              org-<org>.lock        EX
            Around ensure_org_members_fresh's refresh->save. Wired (not
            skipped) to honor security-hawk F4 (shawk7nq): the refresh must
            not race on the cache file. Implemented with DOUBLE-CHECKED
            locking inside the helper — the warm fast path returns BEFORE
            taking the lock, so steady-state runs never contend; only an
            actual refresh locks (and re-checks freshness in case a peer just
            refreshed). The lost update was already benign post-Phase-0
            (atomic write); the lock additionally dedups redundant fetches.

          sentinel_lock()            attention.lock        EX
            Around set/clear of the ATTENTION sentinel (check-then-act).
            Replaces the "no locking, guaranteed by the global lock" claim
            in the sentinel node and in aklr5pq3.

          dashboard_lock()           dashboard.lock        EX
            Around dashboard.md render. Low stakes.

          watchdog_ack_lock()        watchdog-acked.lock   EX
            Resource #9: cache_dir()/watchdog-acked.yaml, written by
            watchdog_ack.record_ack (load->modify->save) and read by
            report's recent-merges watchdog. Phase 2 lock guards the
            load-modify-save lost-update window; Phase 0 already made the
            write itself atomic.

        REFACTOR SHAPE: delete the outer `with global_lock(...)` in every
        handler; wrap the short critical sections INSIDE _run_under_lock,
        each acquired and released before the next. The multi-minute
        gh-fetch / preflight phases run under NO lock. This both removes the
        coarse blocking and shrinks every hold to milliseconds.

        WHERE THE LOCKS LIVE: org_lock, default_branches_lock, and
        watchdog_ack_lock are acquired INSIDE their shared helpers
        (ensure_org_members_fresh, prime_default_branches, record_ack) rather
        than at each call site — DRY and unforgettable for new subcommands.
        default_branches_lock wraps only the file read-modify-write (it
        RE-READS under the lock so a concurrent prime is merged, not lost);
        the GraphQL prefetch runs OUTSIDE it so commands never serialize on
        each other's network fetch. repo_lock, run_state_lock, and
        sentinel_lock are acquired at the handler/_finish call sites (they
        need the per-command timeout/label and the per-repo slug).

        DEADLOCK SAFETY: the structure is PREDOMINANTLY FLAT — at most one
        lock is held at a time in almost all paths (org/default_branches are
        primed before the per-repo loop; repo_lock is the only in-loop lock
        and is released each iteration). The ONE sanctioned nesting is
        `show <sub>`'s sentinel clear, which holds sentinel_lock while still
        holding run_state_lock(sub, SH) — i.e. run_state -> sentinel, the
        canonical order below; nothing acquires those two in reverse (the
        mutators take run_state at complete() and sentinel at set_attention
        SEPARATELY, never nested), so no cycle can form. Acquisition order
        for any nesting, documented at the lock definitions:
          org -> default_branches -> repo(slug) -> run_state(sub)
              -> sentinel -> dashboard

        AUDITED + ENFORCED (2026-06-04, after a "prove it's deadlock-free"
        review): a source audit confirmed org/default_branches/watchdog locks
        are taken early (before any repo/run_state/sentinel lock), _finish
        takes sentinel and run_state sequentially (not nested), and the lone
        simultaneous hold is show's run_state(SH)->sentinel(EX) in canonical
        order with no reverse pair. Two regression guards keep it true:
        test_all_production_lock_acquisitions_pass_a_timeout (token-scans
        src/gitbulk; fails if any `with <lock>(...)` omits timeout=) and
        test_show_nested_sentinel_clear_is_bounded_not_a_hang (drives the one
        nested path under contention -> bounded LockTimeoutError, not a hang).

        WAIT-FOREVER vs TIMEOUT: every production acquisition passes a bounded
        timeout (tmlk5pq3: 300s read / 1800s mutate; 60s file-only cache
        locks; 300s org refresh). On contention the poll loop raises
        LockTimeoutError -> handler catches -> exit 1 + holder metadata on
        stderr, no ATTENTION. The keyed constructors default to None
        (block-forever) per hk5pq3nm.c, so safety lives at the call sites and
        the token-scan guard makes a forgotten timeout= a test failure. Worst
        observable case is therefore a bounded timeout + clean exit 1, never a
        hang.

        kp7nw4mq.h UPDATE: that node said "subcommands acquire global_lock()
        in their CLI handler before RunState.begin()". Under rsclk7nq there
        is no single global_lock; RunState.begin() still takes no lock, but
        the handler now acquires the SPECIFIC resource locks around the
        specific sections (RunState.complete() under run_state_lock(sub,EX)).
        runstate.py still does not import locks.py (no cycle).

        PHASE 0 HARDENING (independent of the lock model; ships first
        because finer locks expose more concurrency that these races would
        otherwise corrupt — see docs design doc §7):
          (1) Unique tmp names in every atomic writer (tempfile.mkstemp in
              the target dir, as update.py already does) — the fixed
              "<name>.tmp" suffix means two concurrent writers of the same
              file collide and one os.replace hits ENOENT. This refines
              kp7nw4mq.c/.d/.e (which specified the tmp+rename pattern but
              with a fixed, collision-prone tmp name).
          (2) Atomic set_attention AND watchdog_ack.record_ack (tmp+
              os.replace) — both did a bare write_text, so an external
              reader (tmux status, report's watchdog) could see a torn file.
          (3) runid uniquifier — RunState.begin does mkdir(exist_ok=False)
              on a second-resolution runid (3pw7qkn2), so two same-sub runs
              in the same second crash. A lock cannot fix this (both compute
              the same runid); add a pid/counter suffix or retry on
              FileExistsError.

        DELIBERATE CONSEQUENCE (user-approved 2026-06-03): two `merge
        --apply` runs may overlap on DIFFERENT repos. repo_lock(slug)
        guarantees they never touch the SAME repo concurrently; the old
        "one mutating gitbulk at a time" global guarantee is intentionally
        dropped. tmlk5pq3's bounded-timeout policy and LockTimeoutError
        handling carry over unchanged to every keyed lock.

        LOCK-STATUS UX (2026-06-04): locks._acquire calls a pluggable,
        default-silent reporter (set_status_reporter) while BLOCKED, so an
        interactive user running two commands sees one waiting on the other.
        cli installs util/lockstatus.TtyLockStatusReporter; library/tests stay
        silent (no behavior change). Wait-only (uncontended acquires render
        nothing); live stderr line with a COUNTDOWN to timeout; folds into an
        active Progress bar (progress.active_progress + set_wait_suffix) so a
        repo_lock wait mid-apply shares the bar's line. Auto-on when stderr is
        a TTY; GITBULK_LOCK_STATUS=off disables; only engages when a bounded
        timeout is set (timeout=None keeps the original blocking flock, no
        status). Full design: docs/design/resource-scoped-locking.md §11.

        ROLLOUT (all landed): Phase 0 (hardening) -> Phase 1 (show/summarize
        off global_lock onto run_state_lock — fixed the reported symptom) ->
        Phase 2 (repo_lock + cache/org/sentinel locks across report + the six
        mutators; global_lock function REMOVED from locks.py). The
        global_lock_file() path helper is kept only as a holder placeholder in
        a few tests.
      approved-by: daniel, 2026-06-03
      supersedes: lj5pqn4kr
      extends: hk5pq3nm

    Business Day Arithmetic API = decision:
      id: gmw3npk7
      why: >
        util/businessdays.py provides two pure functions used by the
        merge-readiness clock from node bg4pqn7m:
        is_business_day(dt) -> bool and
        add_business_days(start, n) -> datetime. Conventions:

        (a) Weekdays only: Monday through Friday are business days; no
        holiday awareness. Matches the policy lock in bg4pqn7m.

        (b) add_business_days preserves time-of-day. Friday 17:00 plus
        1 business day equals Monday 17:00. Reason: ready_since is a
        wall-clock moment, not a calendar day — "3 business days
        later" most naturally means "the same wall-clock moment 3
        business days later." Rejected "round to midnight at the end
        of day N" because it would let a Friday-17:00-ready PR become
        merge-eligible at Wednesday 00:00, six hours short of three
        full business days.

        (c) If start is itself a weekend, the count begins from the
        next business day. add_business_days(Saturday 17:00, 1) is
        Monday 17:00. Reason: a PR that becomes ready over the weekend
        shouldn't accrue weekend "credit" toward its age threshold.

        (d) n=0 is identity (returns start unchanged even if weekend);
        n<0 raises ValueError. gitbulk never needs to subtract business
        days; rejecting the case loudly is safer than silently
        defining backward semantics.

        (e) The function does not normalize timezone. Callers pass
        tz-aware datetimes and the local-TZ semantics from
        bg4pqn7m are the caller's responsibility — this module is
        TZ-agnostic arithmetic on whatever tzinfo the caller supplies.

        Excluded from v1: business_days_between() count function. No
        caller needs it yet; added when one does (YAGNI).
      approved-by: daniel, 2026-05-27

    Paths Module Conventions = decision:
      id: 3pw7qkn2
      why: >
        paths.py is the single source of truth for every file and
        directory gitbulk reads or writes; reading paths.py tells you
        gitbulk's entire on-disk footprint. Four conventions are
        load-bearing:

        (a) XDG-only path resolution: $XDG_CONFIG_HOME and
        $XDG_CACHE_HOME are honored as the override mechanism, falling
        back to ~/.config and ~/.cache respectively. No gitbulk-specific
        override env var. Rationale: XDG is the documented platform
        standard; tests monkeypatch the XDG vars to tmp_path and thereby
        exercise the same code path production users will. Rejected
        GITBULK_ROOT because it would split the world into "production
        XDG behavior" vs "test-only override behavior" — that's exactly
        the divergence that lets test-only paths rot.

        (b) Compact ISO 8601 UTC for run-directory timestamps:
        YYYYMMDDTHHMMSSZ (e.g., 20260527T194501Z). Sortable by `ls`,
        filesystem-friendly (no colons), timezone-unambiguous. Rejected
        epoch (unreadable when grepping run dirs) and hyphenated ISO
        (longer with no readability gain since runs are not read aloud).
        Always UTC so cross-timezone log comparison is straightforward.

        (c) Slug normalization for filesystem use: owner/repo becomes
        owner__repo (double underscore). Already established in this.i
        node mw6kp2nq for worktrees; reused for locks/ and findings/ so
        a single convention covers every place gitbulk encodes a slug
        into a path. Malformed slugs (anything not matching exactly
        owner/repo) raise ValueError at the boundary rather than
        silently producing odd filenames.

        (d) No memoization in path helpers: each call reads env vars
        and constructs the Path fresh. The cost (a few microseconds
        per call) is irrelevant; the benefit is that test fixtures
        setting and unsetting XDG vars work without cache-clearing
        rituals.
      approved-by: daniel, 2026-05-27

    # ─── NETWORK BEHAVIOR ────────────────────────────────────────────────────

    Serial GraphQL Coalescing No Rate Limiter = decision:
      id: gd4kp7nz
      why: >
        v1 hits gh serially per repo, coalescing related queries via
        GraphQL where the data shape allows. Estimated load: 150 repos
        × 1–2 GraphQL calls = ~300 calls per run, well under the 5000/hour
        primary limit. Secondary rate limits are concurrency-sensitive,
        so staying serial sidesteps them naturally without an explicit
        limiter. A token-bucket rate limiter is YAGNI for v1; add only
        if 429s are observed in practice. Cost: report wall-clock time
        is ~minutes not ~seconds; accepted for a nightly cron job.

    # ─── CONCURRENCY & NOTIFICATION ──────────────────────────────────────────

    Global Plus Per Repo Lock = decision:
      id: lj5pqn4kr
      why: >
        Two locks: a global advisory lock at ~/.cache/gitbulk/run.lock
        (fcntl.flock, shared for read-only subcommands, exclusive for
        mutating ones), and per-repo exclusive locks at
        ~/.cache/gitbulk/locks/<owner>__<repo>.lock for the duration of
        any mutating op. This lets two report runs overlap, a merge wait
        for any in-flight report, and a merge on repo A run concurrently
        with a report on repo B. Rejected: single global exclusive lock
        (would serialize everything, including independent reads), and
        no locking (concurrent mutating ops could produce inconsistent
        run state and racing worktree creation).
      superseded-by: rsclk7nq    # 2026-06-03 resource-scoped model; global_lock retired, repo_lock activated

    Four Layer File Based Notification = decision:
      id: tp4kq2nr
      why: >
        v1 ships layers 1–4 — run artifacts in ~/.cache/gitbulk/runs/,
        exit codes (0/1/2/3/4/99), a dashboard.md rewritten each run,
        and an ATTENTION sentinel file — all file-based. External
        adapters (ntfy.sh, slack, desktop notifications) are deferred
        because each one adds credentials/services that complicate first
        deploy; v1 should be installable and runnable with zero account
        setup beyond the gh CLI the user already has. Shell-prompt or
        tmux-statusline integration consumes the ATTENTION sentinel for
        live visibility without an external service.

        Channel split (implemented in bin/gitbulk-cron, conformance verified
        by node shkd5crn 2026-05-29): MAILTO is the STRUCTURAL-FAILURE channel
        only. The wrapper echoes its status line to stdout — which is what
        cron mails — exclusively on exit 1 + unexpected codes; clean (0),
        attention/skips (2|3), audit (4), and not-implemented (99) stay silent
        on stdout (the status is still written to the log). This keeps routine
        attention, which fires ~nightly for a large fleet, on the sentinel/
        daily-glyph channel and reserves email for things that are actually
        broken (consistent with tmlk5pq3's "stuck lock is a structural issue
        surfaced via cron's failure channel, not the daily attention glyph").

    Semantic Terminal Color And Glyphs = decision:
      id: clr7sgqm
      why: >
        gitbulk's summary and error lines are colorized with a semantic
        outcome marker (green ✓ clean, yellow ⚠ attention, red ✗/red text
        error) so a human scanning a ~150-repo run can spot the one thing
        that needs attention. Added 2026-05-30; lives in
        src/gitbulk/util/style.py with the per-command wiring at each
        summary/error print site.

        Design rules locked here:

          - SEMANTIC, NOT DECORATIVE. Every style maps to a meaning, and
            the outcome category is derived from the run's exit code via
            outcome_for_exit_code() — the SAME classification that drives
            the ATTENTION sentinel and exit-code channel (node tp4kq2nr).
            Color never introduces information; disabling it only ever
            hides emphasis. The exit-code→category map duplicates the
            EXIT_* integers (which each command module already copies) to
            keep style.py free of a cli import that would cycle; the drift
            guard is test_style.test_exit_code_map_matches_cli_constants.

          - TWO INDEPENDENT GATES. Color (ANSI) and Unicode (glyph vs.
            ASCII fallback [ok]/[!]/[x]) are resolved separately, per
            stream, because their failure modes are independent (a dumb
            terminal or a redirected file may take UTF-8 but not ANSI, and
            vice-versa).

          - EMPHASIS GLYPHS ARE COLOR-GATED. The ✓/⚠/✗ marker appears only
            when color is on, so with color off a summary line is
            byte-identical to the pre-color era and any downstream parser
            of gitbulk's stdout keeps working. This preserves the cron
            channel-split in tp4kq2nr: the mailed stdout status line stays
            clean.

          - ENV-VARS ONLY, NO --color FLAG. Precedence (highest first):
            NO_COLOR present (any value) → off; FORCE_COLOR/CLICOLOR_FORCE
            → on; TERM=dumb → off; stream.isatty(). NO_COLOR deliberately
            outranks FORCE_COLOR: it is an accessibility/environment
            opt-out and is treated as the strongest signal, while
            FORCE_COLOR's job is only to beat the isatty check when piping
            (e.g. into `less -R`). A --color flag was rejected to avoid
            threading a global option through every argparse subparser for
            no capability the env vars don't already provide.

        Scope of the first cut (this commit): the per-command run summary
        lines and the error/lock-timeout/ConfigError stderr paths. The
        diagnostic logger stays plain (it is mailed by cron — ANSI would be
        noise). Glyphs in dense listings and column alignment in
        `show`/`invariants` are a deliberate later layer that reuses the
        same two-gate Style without rework.

    # ─── PHASE 1D FOLLOWUPS (adversarial review 2026-05-27) ─────────────────

    Phase 2 CLI Lock Timeout Policy = decision:
      id: tmlk5pq3
      why: >
        Phase 2's CLI subcommand handlers MUST pass an explicit
        bounded timeout to every call into global_lock / repo_lock
        (per the split codified in node hk5pq3nm.c). Concrete
        defaults:

          Read-only subcommands (report, summarize, show, ack,
          invariants):                                 timeout = 300s
          Mutating subcommands (merge, rebase-onto-default,
          close-stale, dispatch):                      timeout = 1800s

        Rationale for these numbers:
          - 300s (5 min) for read-only: generous enough that a
            concurrent long-running report doesn't trip a quick
            `gitbulk show`; short enough that a hung process surfaces
            within one cron cycle.
          - 1800s (30 min) for mutating: long enough for legitimate
            dispatch / rebase runs across many repos; short enough
            that stuck mutators are noticed within one nightly cron
            cadence.

        LockTimeoutError handling:
          - Caught at the subcommand entry point.
          - Surfaced as exit code 1 (structural failure).
          - Holder metadata (pid, started_at, subcommand, alive
            status — see node hk5pq3nm.e + the pid-liveness fix in
            Phase 1D) written to errors.log and to stderr so cron's
            MAILTO captures it.
          - ATTENTION sentinel is NOT set on timeout. Reasoning:
            attention is for "PRs need a human"; a stuck lock is a
            structural issue surfaced via cron's failure channel, not
            via the daily attention glyph.

        These numbers and the no-ATTENTION-on-timeout rule are
        revisitable as Phase 2 lands and real timing data accumulates.
      approved-by: daniel, 2026-05-27

    Cache Artifact Schema Versioning = decision:
      id: schv4nrm
      why: >
        Every file gitbulk writes into ~/.cache/gitbulk/ carries an
        explicit schema_version field. Established by the platform-
        architect adversarial review (2026-05-27) which flagged the
        cache directory as a de-facto cross-version API: future
        readers (gitbulk show, dashboard re-rendering, the user's
        tmux integration, external notifier adapters) need to know
        what shape they're looking at.

        Conventions:
          - YAML files (manifest.yaml, state.yaml) carry
            `schema_version: <int>` at the top level. Initial value
            = 1 for every file.
          - JSONL events (invariants.log, errors.log) carry
            `"v": <int>` as the first key of each event.
          - ATTENTION sentinel migrates from the whitespace-
            delimited 4-field format defined in node snk7p4qm to a
            ONE-LINE JSON OBJECT with the same fields plus "v": 1.
            Clean break, not a silent corruption — any external
            consumer parsing the old whitespace format will fail
            loudly, which is the intended migration signal. This
            supersedes the format conventions in snk7p4qm; that
            node remains the API surface, only the wire format
            changes.
          - Reader strictness: gitbulk reads only artifacts whose
            schema_version is in {N-1, N} where N is the current
            version. Older versions: refuse with a clear message,
            log to errors.log, continue. Forward-compatible (the
            current gitbulk reads both v_curr and v_curr-1 during
            transitions); backward-safe (old gitbulk seeing v_new
            fails loudly).
          - Initial state (Phase 1D): all schemas are at v=1.
            Future bumps document the breaking change in this.i as
            their own decision nodes.
      approved-by: daniel, 2026-05-27

    Subcommand Invariant Chain Field = decision:
      id: scinv4qm
      why: >
        Extends ``Subcommand`` (node smodlpr3) with a new ClassVar-
        style field declaring which invariants run for that
        subcommand, in order. Set in src/gitbulk/subcommands.py
        per-Subcommand at construction time; consumed by the CLI
        handler when building the chain for a run.

            invariant_chain: tuple[str, ...]   # registry names

        Resolves the "how does each subcommand know which
        invariants to compose" question that was open after
        ivp4wq7n. Three alternatives rejected:

          (a) Invariants self-declare via subcommands ClassVar on
              each invariant class. This works but means the chain
              ORDER for a subcommand is implicit — the registry's
              insertion order. Forcing ORDER to be declared at the
              subcommand keeps "what runs and in what sequence" in
              one place per subcommand.
          (b) Hardcode chains in the CLI handler. Splits the
              "what runs for X" knowledge between the registry
              and the handler. Rejected.
          (c) Dynamic discovery (run all registered invariants
              with subcommand in their applies-to). Removes the
              explicit ordering — bad for the c4jzm5pn semantics
              where ordering matters (UNIVERSAL → PER_REPO →
              PER_PR).

        Phase 2 fills in invariant_chain on each Subcommand using
        the catalog from ph2inv4n. Phase 5 adds the merge-specific
        invariants and updates the merge Subcommand's chain.

        Empty tuple () is valid (no invariants run; current state
        for ack, invariants).
      approved-by: daniel, 2026-05-28

    Subcommands Module And Dataclass = decision:
      id: smodlpr3
      why: >
        SUBCOMMANDS in cli.py was a list[tuple[str, str]] of
        (name, help). dashboard.py imported it across the CLI/
        rendering boundary, which the platform-architect
        adversarial review (2026-05-27) flagged as a layering
        inversion.

        Resolution: promote SUBCOMMANDS to its own module
        src/gitbulk/subcommands.py exporting a typed frozen
        dataclass:

            @dataclass(frozen=True)
            class Subcommand:
                name: str
                help: str
                mutating: bool                  # 2vqp4nk6 dry-run default applies
                lock_mode: Literal["shared", "exclusive"]
                                                # per lj5pqn4kr
                needs_clone: bool               # per 5xqp2nkr — invariant
                                                # local.exists is in this
                                                # subcommand's chain only if True

            KNOWN: tuple[Subcommand, ...] = (...)

        cli.py and dashboard.py both import from subcommands.py.
        The dataclass becomes the single declarative answer to
        "is this mutating? does it need a clone? what lock mode?"
        — replacing knowledge previously scattered across cli.py,
        AGENTS.md, and docs/architecture.md.

        Per-subcommand initial values:
          report               mutating=F lock=shared    clone=F
          summarize            mutating=F lock=shared    clone=F
          dispatch             mutating=T lock=exclusive clone=T
          merge                mutating=T lock=exclusive clone=F
          rebase-onto-default  mutating=T lock=exclusive clone=T
          close-stale          mutating=T lock=exclusive clone=F
          show                 mutating=F lock=shared    clone=F
          ack                  mutating=F lock=shared    clone=F
          invariants           mutating=F lock=shared    clone=F
      approved-by: daniel, 2026-05-27

    POSIX Only Runtime = constraint:
      id: posqx2nm
      why: >
        gitbulk uses fcntl.flock (POSIX advisory locks) in locks.py
        and POSIX symlink semantics (os.replace on symlinks) in
        runstate.py. Neither is portable to Windows. The tool's
        documented runtime is Linux (and any POSIX-compatible OS
        such as macOS); Windows is explicitly NOT supported.

        This was implicit in the codebase but was not previously
        stated as a constraint — the platform-architect adversarial
        review (2026-05-27) noted the gap. A future contributor
        attempting Windows compatibility would either need to
        abandon fcntl (changing the locking model entirely) or wrap
        with msvcrt.locking, which has different semantics
        (mandatory rather than advisory; per-region rather than
        whole-file).

        Accepted cost: gitbulk doesn't run on Windows. The user's
        development setup is WSL2/Ubuntu, so this is consistent
        with actual use. macOS support is preserved by virtue of
        macOS being POSIX-compliant.
      approved-by: daniel, 2026-05-27

    CI Python Matrix Policy = decision:
      id: cipym4kr
      why: >
        CI runs the test suite on Python 3.12 ONLY (the version pinned
        in .python-version and the user's deployment target).

        Revised 2026-05-28 from a 3.10/3.12/3.13 matrix to single-
        version. Reasoning:

          - The codebase has exactly ONE Python-version-sensitive
            code path: gh._parse_iso8601's `Z`-suffix handling
            workaround for Python <3.11 datetime.fromisoformat. That
            workaround is unconditional and works on every supported
            Python, so the matrix is no longer catching anything.

          - Branch protection requires all matrix entries to be
            green. A single flaky test (host-dependent pid liveness,
            timing-sensitive thread scheduling) blocked merge 3×
            instead of 1×. On 2026-05-28 the matrix turned two real
            test bugs into a 3-way merge blocker.

          - The matrix's original "newer/older Python regression"
            signal value was theoretical; in practice the first
            failures it caught were test-environment quirks, not
            real Python-version regressions.

          - The deployment Python is one version. Testing 3 versions
            for a tool that runs on 1 was paying CI cost for
            non-load-bearing signal.

        If a Python-version-sensitive feature lands later (e.g. tomllib,
        new typing syntax, async TaskGroup-only patterns), revisit by
        adding a SECOND CI job (not a matrix) targeting that specific
        feature's required version range.

        Original 2026-05-27 rationale (matrix kept) and the security-
        hawk F4 mitigation history live in git log for posterity.
      approved-by: daniel, 2026-05-27; revised 2026-05-28

    # ─── TENSIONS (deferred, do not resolve silently) ────────────────────────

    gh Client Implementation = decision:
      id: ghclmp7n
      why: >
        Resolves tension ghc7npqk (which now lives only in git
        history as the pre-decision record). Six forks settled:

        (a) **Protocol class + FakeGHClient.** ``typing.Protocol``
        defines the interface; production ships one concrete impl
        (``ProductionGHClient``) that subprocesses to ``gh``; tests
        inject ``FakeGHClient`` returning canned data. Why: type-
        safe at call sites (mypy / IDE checks against the Protocol),
        clean test seam without monkeypatching ``subprocess``, no
        hidden subprocess execution in tests (AGENTS.md "no network
        in tests"). Cost: two classes per concept; accepted.

        (b) **Per-method typed API.** Methods like
        ``gh.list_open_prs_for_repos(slugs) -> dict[str, list[PRInfo]]``
        return typed structures. Rejected the command-style
        (``gh.run([...args]) -> Response``) because it shifts the
        burden of parsing raw gh output to every invariant, which
        is the exact opposite of "single seam." Rejected per-method
        without coalescing (option from Q2) because it would invite
        N-call-per-repo loops.

        (c) **Coalescing inside the client.** The list-style methods
        accept iterables of slugs and emit one GraphQL query per
        call, transparent to callers. Matches decision gd4kp7nz
        ("serial + GraphQL coalescing"). Callers can NOT accidentally
        produce N round-trips when one would do; the API doesn't
        give them the option.

        (d) **Client owns retry policy; per-call timeout kwarg.**
        The client wraps each gh invocation in a small retry loop
        for transient errors (5xx, network timeout). The retry
        policy is hardcoded conservative — 3 attempts with
        exponential backoff — and not configurable at call sites
        (callers shouldn't need to think about it). Per-call
        ``timeout: float | None = None`` kwarg lets callers extend
        for slow GraphQL queries. Default timeout = 30s.

        (e) **Stateless.** No per-client cache of rate-limit
        headers, auth status, or org members. The org-members cache
        lives separately in the humans/bots classifier (node
        hbcls4pq); the gh client just runs commands.

        (f) **Test seam: FakeGHClient protocol stub.** Tests
        construct a ``FakeGHClient`` configured with the canned
        responses they need, and pass it through ``InvariantContext``
        (which already has a ``gh: Any`` field per ivp4wq7n that we
        now type-narrow). No subprocess in tests; no network; the
        Protocol contract is enforced by mypy on both sides.

        Additional discipline (from user feedback during the Phase
        2 interview): **Every gh subcommand wired into
        ProductionGHClient must be verified non-deprecated** against
        the live `gh` CLI at integration time and re-checked when
        the call is touched. See AGENTS.md "Verify gh against GitHub
        API deprecations" and the feedback memory
        `feedback-gh-cli-deprecation-verification`. The verification
        date is recorded in a comment at each call site.
      approved-by: daniel, 2026-05-28

    PR Data Model = decision:
      id: prdtm4kn
      why: >
        ``PRInfo`` is a frozen dataclass carrying the shape every
        invariant and ``report`` consumer needs. Lives in
        ``src/gitbulk/pr_info.py``. Returned by gh client methods
        and used as a parameter type in per-PR invariant chains.

        Fields (initial Phase 2 set; future fields added via this.i
        decision nodes):

            slug: str                      # "owner/repo"
            number: int                    # PR number
            title: str
            url: str                       # canonical browser URL
            author: str                    # login
            base_ref: str                  # baseRefName
            head_ref: str                  # headRefName
            head_sha: str
            state: Literal["OPEN","CLOSED","MERGED"]
            is_draft: bool
            mergeable_state: str | None    # gh's mergeStateStatus
            created_at: datetime           # UTC
            updated_at: datetime           # UTC
            last_pushed_at: datetime|None  # last head-ref push
            labels: tuple[str, ...]
            review_decision: str | None    # APPROVED/CHANGES_REQUESTED/REVIEW_REQUIRED/None
            checks_status: str | None      # SUCCESS/FAILURE/PENDING/None

        Rationale for "frozen dataclass + hand validation" over
        pydantic: same rule as ck5pwr2n (no pydantic dep for a
        small schema).

        Why ``review_decision`` / ``checks_status`` / ``mergeable_state``
        as ``str | None`` rather than enums: GitHub adds new values
        periodically (the deprecation discipline ghclmp7n applies
        here too). Hard enums would force a code change on every
        new value; ``str | None`` plus a documented set of "known"
        values plus a fallthrough behavior is more forward-
        compatible. The set of known values lives in
        ``pr_info.py`` as ``_KNOWN_REVIEW_DECISIONS`` etc., used
        for validation but not for type narrowing.

        Excluded from initial fields: ``ready_since`` (computed,
        not stored — lives in a separate helper to keep PRInfo
        as raw gh data).

        Phase 5+ additions for the merge gate (this.i nodes
        zk3r4nqp / bg4pqn7m): ``unresolved_thread_count: int``
        (count of currently-open review threads, bots included),
        ``timeline_events: tuple[TimelineEvent, ...]`` (a subset
        of GraphQL timelineItems — ReadyForReview, ConvertToDraft,
        and PullRequestReview with APPROVED/CHANGES_REQUESTED
        state), and ``timeline_capped: bool`` (true if the
        timeline-walk window truncated). These power the
        ``pr.no_unresolved_threads`` invariant and the
        timeline-aware ``compute_ready_since``. Fields default
        to safe-empty so legacy fixtures and ``FakeGHClient``
        usages do not need updating.
      approved-by: daniel, 2026-05-28

    Humans Bots Classifier = decision:
      id: hbcls4pq
      why: >
        ``classify_login(login: str, policy: Policy) -> Classification``
        where ``Classification`` is an ``Enum`` with values ``HUMAN``,
        ``BOT``, ``UNKNOWN``. The classifier is a pure function over
        the login string plus the policy snapshot; no I/O at call
        time. Cached org-members data is loaded once at startup
        into the policy snapshot or alongside it.

        Resolution order (matches design-notes §3 and node
        pj5kn2zw):

          1. login in policy.humans.always_human → HUMAN
          2. login in policy.bots                → BOT
          3. login in cached_org_members AND login not in
             policy.humans.exceptions             → HUMAN
          4. otherwise                            → BOT  (default
             non-human per pj5kn2zw)

        ``UNKNOWN`` is reserved for tooling that needs to
        distinguish "we asked but couldn't decide" (e.g., classifier
        called before the org-members cache loaded). Production
        flow never returns UNKNOWN because preflight invariant
        ``org.members.fresh`` guarantees the cache is loaded before
        any classifier call.

        Org-members cache:

          - File: ``~/.cache/gitbulk/org-members/<org>.yaml``
          - Schema: ``schema_version: 1`` (per schv4nrm), ``fetched_at:
            ISO-8601 UTC``, ``members: tuple[str, ...]``.
          - TTL: ``policy.humans.cache_ttl_hours`` (default 168 = 7
            days, per ormrf7kq; was 24). NOTE: no subcommand relies on
            the invariant's hard-fail anymore — EVERY command
            auto-refreshes a missing/stale cache via
            ``ensure_org_members_fresh`` before the preflight runs (see
            ormrf7kq). The ``org.members.fresh`` invariant is kept as a
            belt-and-suspenders safety net (its Fail message still reads
            "older than TTL; rerun with --refresh-org-members" for any
            path that ever skips the helper).
          - Refresh command (Phase 2): a CLI flag
            ``--refresh-org-members`` on every subcommand forces a fetch
            via ``gh api orgs/<org>/members --paginate``. It is now a
            FORCE override (refetch even when fresh); see ormrf7kq.
          - Empty / null ``policy.humans.org``: the classifier
            falls through step 3 (no org lookup), so unknown
            logins default BOT per the safer-failure-mode rule.

        Test seam: classifier is pure; tests pass canned Policy +
        canned cached members. No mocking required.
      approved-by: daniel, 2026-05-28

    Report Auto-Refreshes Org-Members Cache = decision:
      id: ormrf7kq
      why: >
        EVERY subcommand now refreshes the org-members cache on its own
        whenever the cache is missing OR stale (age >=
        ``humans.cache_ttl_hours``), instead of requiring an explicit
        ``--refresh-org-members`` and otherwise hard-failing the
        ``org.members.fresh`` universal preflight (the original behavior
        described under hbcls4pq). The shared entry point is
        ``org_members_cache.ensure_org_members_fresh(gh, policy, *,
        force)``, called by each handler right after it builds the gh
        client + begins RunState, inside the global lock, before the
        universal preflight runs.

        Trigger: the live cron deployment (shkd5crn) runs ``report``
        Mon–Fri at 03:00 with no flags and nothing else refreshes the
        cache, so the 24h TTL was crossed on every run — guaranteed after
        the weekend gap (Fri→Mon ≈ 72h). The 2026-06-01 run aborted at
        the preflight ("org members cache for 'provenant-dev' is older
        than 24h"), exit 1 → failure email, no triage report. A run that
        cannot sustain its own freshness precondition is a defect, not a
        user error.

        Why ALL commands, not just the unattended report (the question
        that reopened an initial report-only cut): a missing/stale cache
        is strictly worse than a fresh one — refreshing only makes
        classification MORE accurate, never less, so "the cache is stale"
        is never a decision a human needs to make and an explicit flag
        adds friction with zero safety benefit. The "mutating commands
        are higher-stakes" argument cuts the OTHER way: those commands
        most need current org membership, so auto-refreshing PROTECTS
        them. This also matches the analogous default-branch cache, which
        already self-heals stale entries inline in the same mutating
        commands (rj7p4kqn: "every staleness failure mode is 'operate too
        conservatively' — never destructive"). org-members was the
        inconsistent outlier. Bonus: ``--refresh-org-members`` was a DEAD
        flag in merge/close-stale/rebase/dispatch (argparse accepted it,
        handlers never called refresh) — unifying made it live everywhere
        and added it to dispatch, which lacked it.

        Design:
          - ``ensure_org_members_fresh`` is a no-op when humans.org is
            unset (classifier falls through to the safe BOT default) or
            when the on-disk cache is already fresh (no network call).
          - ``--refresh-org-members`` is retained on report/merge/
            close-stale/rebase-pr and ADDED to dispatch, now a working
            FORCE override: refetch even when the cache is fresh.
          - The refresh runs INSIDE the global lock with a RunState
            already begun (preserves security-hawk F4, shawk7nq): the
            network fetch + cache write stay inside the audit envelope.
          - The one legitimate hard-stop is a refresh FAILURE (GitHub
            unreachable/unauthenticated): the helper raises
            ``OrgMembersRefreshError`` (message names the forced
            ``--refresh-org-members failed`` vs automatic
            ``org-members auto-refresh failed`` trigger, chained from the
            GHError), and each handler converts it to
            EXIT_STRUCTURAL_FAILURE + an errors.log entry. A mutating
            command must not classify authors on a guess.
          - The ``org.members.fresh`` invariant is KEPT, unchanged, as a
            belt-and-suspenders safety net: after auto-refresh it always
            passes, but it still guards a future code path that forgets
            to call the helper. Its Fail branches are covered by direct
            invariant unit tests (test_invariants_catalog), not command
            flow.

        TTL change (folded in same commit): default ``cache_ttl_hours``
        24 → 168 (7 days), matching the default-branch cache. With
        universal auto-refresh a longer TTL just means fewer needless
        refetches; the max staleness window degrades only to the
        conservative BOT default, never destructively.

        Considered and rejected: widening only the cron TTL or adding
        ``--refresh-org-members`` to the crontab line (treats the
        symptom, leaves the self-heal gap for any future unattended
        invocation); removing the ``org.members.fresh`` invariant
        (cheap safety net, keep it).
      approved-by: daniel, 2026-06-01

    Security Hawk Findings Disposition = decision:
      id: shawk7nq
      why: >
        End-of-Phase-2 security-hawk adversarial review (2026-05-28)
        produced 5 findings against the "bad software on the user's
        dev box maliciously damages 150 GitHub repos" threat model.
        Report at reviews/security-hawk-2026-05-28.md. Dispositions
        applied in Phase 2D (the next code commit after this node):

        F1 CRITICAL — slug regex accepts `..` segments → path-
        traversal primitive. **ACCEPT.** Tighten _SLUG_PATTERN in
        paths.py to require alphanumeric-leading owner and an
        explicit forbidden-segment list rejecting `.` and `..`.
        Apply the same tightening (with ConfigError) in
        config/repos.py so the two layers stay aligned. Phase 5
        worktree code would have weaponized this; closing now
        eliminates the surface entirely.

        F2 HIGH — ProductionGHClient gh_path="gh" → silent PATH
        hijack. **ACCEPT.** Resolve gh_path via shutil.which() at
        construction; store the absolute path; raise GHError
        immediately if not found. A PATH-prepend attacker cannot
        substitute `gh` after the client is constructed because
        the client carries the absolute path for every subsequent
        invocation.

        F3 HIGH — no os.umask(0o077) → cache files world-readable.
        **ACCEPT.** Set os.umask(0o077) inside cli.main() at
        startup so every file gitbulk creates from then on is
        owner-only. Existing files are unaffected; the security
        improvement applies from the next run forward.

        F4 MEDIUM — refresh_org_members runs BEFORE global_lock
        acquire in report_handler. **ACCEPT.** Move the refresh
        inside the lock so the network call and cache write are
        within the audit envelope.

        F5 MEDIUM — AGENTPREP_NO_AI=1 bypass + no branch
        protection / CODEOWNERS / required reviews. **NOTE FOR
        USER.** Procedural: when the user pushes to a public
        remote, branch protection should be applied (the
        protect-main-branch.sh script is already in
        ~/code/devenv/). 100% branch coverage does not prove
        invariant chain composition is intact; a malicious change
        to subcommands.py could pass all tests. The right
        backstop is human PR review on changes touching
        subcommands.py / catalog.py / cli.py. No code change
        possible from gitbulk side.

        Sub-threshold items (no actions pinned by SHA, no
        secret-scanning hook, gh round-trip slug filtering) are
        deferred and known. (LICENSE: resolved 2026-05-29, node
        vn4kq7pr — Apache-2.0 added.)
      approved-by: orchestrator-claude on user's behalf, 2026-05-28
      stage-status: in-progress

    Phase 2 Invariant Catalog = decision:
      id: ph2inv4n
      why: >
        Concrete invariants landing in Phase 2, as specified by the
        Phase 2 scope ceiling answer (medium: gh wrappers + preflight
        + per-PR baseline + classifier + report). Each invariant
        below gets a Python class implementing the Invariant ABC
        from ivp4wq7n.

        UNIVERSAL preflight (run once per gitbulk run):
          - gh.authenticated     — probes ``gh api user`` (returns
            user JSON iff authed; clean exit-code semantics).
            Verified non-deprecated 2026-05-28.
          - config.parseable     — confirms gitbulk.yaml + repos.txt
            already loaded (i.e. caller passed them; this is a
            sanity invariant).
          - org.members.fresh    — confirms the org-members cache
            for ``policy.humans.org`` exists and is younger than
            ``policy.humans.cache_ttl_hours``.

        PER_REPO preflight (run once per configured repo, only for
        subcommands whose Subcommand.needs_clone is True OR which
        otherwise interact with the local clone):
          - local.exists           — local_path is a git repo per
            node 5xqp2nkr semantics.
          - local.remote_matches   — clone's origin URL points at
            the configured slug.
          - local.default_branch_in_sync — local default branch
            matches GitHub's per branch protection (preliminary;
            full check is in pr.base_is_default).
          - github.reachable       — single gh probe against the
            configured slug succeeds.

        PER_PR baseline (run once per open PR):
          - pr.base_is_default     — PR.base_ref == default branch
            per AGENTS.md hard rule.
          - pr.author_known        — classify_login(PR.author,
            policy) != UNKNOWN. UNKNOWN should be impossible by
            invariant ordering, but the assertion is the protection.

        Order in a chain: UNIVERSAL → PER_REPO → PER_PR. Stop on
        first Fail per c4jzm5pn runner semantics.

        Each invariant class registers via ``@register`` from the
        invariants framework (ivp4wq7n) at import time. The
        gitbulk-invariants entry-point handler from clip7nm4 now
        lists them by name + kind + applies-to once Phase 2 lands.

        Mutating / dispatch / merge-specific invariants are
        deferred to Phase 3+ per the scope ceiling answer.
      approved-by: daniel, 2026-05-28


    Summarize Prompt Design = decision:  # resolves tension kw2pn7qz
      id: smprmpt4n
      why: >
        At Phase 3 entry, with `gitbulk report`'s state.yaml shape now
        concrete (per node prdtm4kn and the per_repo records emitted by
        commands/report.py), the summarize prompt is FROZEN as the
        content of `prompts/triage.md`. Load-bearing choices:

        (a) **Three fixed sections — TOP ATTENTION, BACKBURNER, CLEAN.**
            The user's daily question is "what needs my eyes right now?"
            Three sections answer it in decreasing urgency without
            forcing the model into a long taxonomy. CLEAN is a count
            only (no enumeration) so a quiet day produces a short
            report. The fixed headings are also the parse target for
            the attention-detection heuristic in commands/summarize.py
            (`## TOP ATTENTION` followed by a non-empty body → set the
            ATTENTION sentinel and exit 2). Renaming or reordering the
            headings is a coupled change to that parser.

        (b) **Input shape: structured state.yaml piped on stdin.**
            Chosen over inlining the data in the prompt or feeding a
            pre-rendered summary.md because: (i) state.yaml is the
            only artifact stable enough across phases to commit to,
            (ii) the model handles structured input well, (iii) it
            keeps the prompt body small and reusable. The prompt
            documents the field names it expects, so a future
            state.yaml schema bump (per schv4nrm) is a prompt
            update — caught by the test that asserts the prompt
            references the current PRInfo field names.

        (c) **Priority rules listed explicitly in the prompt.** An
            ordered "first match wins" list (failing checks → changes
            requested → blocked/dirty → review required → ready-to-
            merge). Rejected: leaving prioritization implicit ("you
            decide") — produces inconsistent runs day to day, which
            defeats the "did the report change?" use case. Rejected:
            full scoring formula — over-specifies without a measured
            need yet.

        (d) **Length cap: ~30 lines, terse.** A scannable triage in
            a terminal is the point; longer output is friction at
            6 a.m. The cap is advisory in the prompt, not enforced
            by the handler — the model honors it reliably in
            practice. If empirical drift shows otherwise, add a
            post-hoc truncation in commands/summarize.py and update
            this node.

        (e) **--prompt PATH override.** The handler accepts an
            alternate prompt file at runtime so the user can A/B
            test prompt variants without editing the package. The
            packaged prompt is the default, discovered via the
            ``prompts/`` directory at the repo root (per AGENTS.md
            "Where things live").

        (f) **--model NAME override.** Same A/B story for the model;
            default stays ``claude-sonnet-4-6`` (matches the model
            choice carried forward into the in-tree kernel per
            node execk7nm).

        Out of scope for Phase 3 (revisit when needed):
          - Multi-prompt summarize (per-priority-tier prompts). The
            current single-shot prompt produces good output; adding
            chained prompts triples the API cost without a measured
            improvement.
          - Structured (JSON) output from claude. Markdown is the
            terminal-native format; a future ``gitbulk show
            --summary --json`` would need a separate prompt variant.
      approved-by: daniel, 2026-05-28
      # was: tension kw2pn7qz (Summarize Prompt Design, deferred at Phase 0)

    Dispatch Execution Kernel In-Tree = decision:  # resolves tension mp7kn4qz
      id: execk7nm
      why: >
        At Phase 4 entry, with gitbulk's invariants and ClaudeClient
        Protocol now concrete, the execution kernel is reimplemented
        in-tree at ``src/gitbulk/exec.py`` (option (c) of the original
        tension). Chosen over (a) subprocess-multiprompt-as-is and (b)
        extract-a-shared-package because:

        (1) **Surface area is small.** The kernel needed is ~250 lines:
            ThreadPoolExecutor with bounded concurrency, per-target
            subprocess.Popen with SIGTERM→wait-5s→SIGKILL escalation,
            SIGINT drain (first press lets in-flight finish, second
            press within 10s hard-kills), per-target log capture into
            the run directory. Engineering this in-tree is cheaper than
            negotiating a stable cross-package API.

        (2) **No second consumer in sight.** Option (b) only pays off
            with two real consumers. multiprompt.py serves a different
            use case (free-form prompts across all repos in scratch
            mode, with a rich-TUI display, optional --delay between
            launches, AI filter pre-pass); gitbulk's dispatch use case
            (templated prompts against an explicit PR list inside
            worktrees, headless from cron) overlaps only on the
            execution kernel itself. Until a third tool needs the same
            primitive, packaging adds churn without leverage.

        (3) **Dependency hazard.** Option (a) would force gitbulk to
            shell out to ../origin-platform/scripts/multiprompt.py,
            making gitbulk's cron health depend on a sibling repo's
            checkout state. Option (b) needs multiprompt to first do
            its own packaging work (per the now-also-resolved
            mprmpkg4); that's not gitbulk's call to make.

        (4) **Test simplicity.** A FakeClaudeClient already exists
            (per ghclmp7n). The kernel layers Popen management around
            it for the parallel path. Tests can inject fakes; no
            subprocess is touched in the test suite.

        Kernel surface:

          - ``ExecTarget(key, working_directory, prompt, input_text)``
            — one unit of work; ``key`` becomes the log-file stem
            (e.g., ``owner__repo__pr42.stdout.log``).
          - ``ExecResult(key, status, exit_code, stdout_path,
            stderr_path, started_at, finished_at, duration_seconds)``
            — terminal record per target. ``status`` is one of
            ``completed`` / ``failed`` / ``timed-out`` / ``interrupted``.
          - ``execute_targets(targets, *, claude, log_dir, concurrency,
            timeout_per_target, model, on_progress)`` — runs the
            bounded pool, returns one ``ExecResult`` per input target
            in input order.

        Subprocess-vs-ClaudeClient judgment call: ``execute_targets``
        manages ``subprocess.Popen`` directly for the parallel path,
        rather than calling ``ClaudeClient.run_prompt`` per target.
        Reason: timeout escalation (SIGTERM→SIGKILL) and CTRL+C drain
        both require holding a process handle that the protocol-level
        ``run_prompt`` does not expose. Adding ``cancel()`` to the
        protocol would couple every implementation to the
        cancellation machinery, and the production ``run_prompt`` is
        already a thin ``subprocess.run`` wrapper — so the kernel is
        effectively the parallel sibling of ``run_prompt``, not its
        consumer. Tests still inject a ``ClaudeClient``-shaped fake
        so the seam exists; the production path uses the fake's
        ``claude_path`` accessor (or default ``"claude"``) when
        building argv. Documented at the top of ``exec.py``.

        Resume semantics out of scope for Phase 4: the kernel is
        single-shot. If interrupted, the user re-invokes ``gitbulk
        dispatch`` and accepts that interrupted targets did not
        complete. multiprompt has a resume; gitbulk dispatch does
        not, because the candidate set is already filtered by
        invariants on each run and re-running is cheap. Recorded as
        a deferred enhancement (revisit if real cron-interrupt
        incidents accumulate).
      approved-by: daniel, 2026-05-28
      # was: tension mp7kn4qz (Dispatch Execution Kernel, deferred at Phase 0)

    Pluggable Coding-Agent Seam = decision:
      id: agbknd7q
      why: >
        Formalize the boundary between gitbulk and the coding agent it
        dispatches so backends other than Claude Code (Gemini CLI, GitHub
        Copilot CLI, Cursor agent, or a fully custom tool) can be driven
        through one small, config-driven interface — without weakening, and
        ideally strengthening, the safety posture. Full design + the locked
        forks live in docs/pluggable-agents.md.

        Before this change the invocation was hardcoded in TWO places that
        had drifted into near-duplicates: ``ProductionClaudeClient.run_prompt``
        (used by summarize) and ``exec._claude_argv`` (used by dispatch's
        parallel kernel), both emitting ``claude -p <prompt> --model <m>
        --dangerously-skip-permissions``. Phase 1 unifies them: a single
        ``AgentInvocation`` value (argv + use_stdin + env + timeout) is
        produced by one ``plan()`` method on the backend; ``run_prompt`` is
        reimplemented on top of ``plan()``, and the kernel sources its argv
        from ``claude.plan(...)`` instead of building its own. The
        ``ClaudeClient`` Protocol is retained; ``AgentBackend`` is the
        generalized superset (adds ``plan``), with ``FakeAgentBackend`` /
        ``ProductionAgentBackend`` aliases. A minimal backend exposing only
        ``run_prompt`` still works via the legacy argv fallback in the kernel,
        so user-supplied implementations are not forced to add ``plan``.

        Deliberately behavior-preserving at Phase 1: argv shape, stdin,
        timeout, and env (inherited; ``env=None``) are byte-identical to the
        prior code, proven by the unchanged 1556-test baseline plus new
        ``plan()`` tests. Config-driven profiles/presets (agprof4k), the
        least-privilege push rework (agpriv8n), env scoping (agenv6q), and
        the bwrap sandbox (agsbx3k) build on this seam in later phases. This
        is also the substantive start on threat-model T1 (the dispatch agent
        running with full ambient authority); see agatk5n for the adversarial
        test discipline.

        Subprocess-vs-plan judgment unchanged from execk7nm: the kernel still
        owns its ``subprocess.Popen`` (needed for SIGTERM→SIGKILL and CTRL+C
        drain); ``plan()`` only supplies the argv/env, it does not run.
      approved-by: daniel, 2026-06-04

    Agent Profiles: Presets Plus Custom Template = decision:
      id: agprof4k
      why: >
        How a backend (agbknd7q) is selected and configured. A new optional
        ``agents:`` mapping plus ``default_agent:`` in gitbulk.yaml, and a
        per-repo ``agent:`` override reusing the existing repos.<slug> override
        machinery. Built-in PRESETS (claude, gemini, copilot, cursor) cover the
        common case in one line (``default_agent: gemini``); a custom
        ``command`` template covers anything else. A user ``agents.<name>``
        block deep-merges over the preset of the same name.

        Resolution order (resolve_agent_name): ``--agent`` → per-repo ``agent:``
        → ``default_agent`` → the ``claude`` preset. The ``claude`` default is
        served by the native ProductionClaudeClient (not the generic
        CommandAgentBackend) so the no-config path is byte-identical to
        pre-feature behavior — the whole feature is opt-in and backward
        compatible (proven: 1556→1628 tests, the original 1556 unchanged).

        Per-repo override in dispatch: the parallel kernel takes a single
        ``claude`` backend plus an optional per-target ``backends`` map keyed by
        ExecTarget.key. dispatch builds that map from per-repo overrides,
        caching each distinct backend by resolved name (build-once). summarize
        is single-backend (run-level). Chosen over threading a backend through
        every ExecTarget: the map is a smaller, well-bounded kernel addition and
        leaves ExecTarget unchanged.

    Agent Command Templates Are Argv-Lists, Never Shell = constraint:
      id: agtmpl9k
      why: >
        The security spine of the pluggable layer (threat-model T6 / §3.4-4
        warned that letting config choose the binary is a red flag; we accept
        that surface deliberately and these are the compensating controls):

        (1) ``command`` / ``model_args`` are argv LISTS. A scalar YAML string is
            a hard config error — a string would imply a shell, which gitbulk
            never uses. So attacker-influenceable prompt/worktree text only ever
            lands as a single argv element and cannot break out: there is no
            shell to break out of.
        (2) ``{prompt}`` / ``{model}`` substitute WITHIN one token (whole token
            or substring), so the substitution can never split into extra args.
            Validated: exactly one ``{prompt}`` token for prompt_via=arg, zero
            for stdin.
        (3) ``command[0]`` is pinned via shutil.which at construction (the
            gh/claude F2 fix, generalized), so a later PATH prepend cannot
            substitute the binary. Absolute path trusted as-is; a relative
            path that does not resolve is a config error; a bare name that
            which() cannot find falls back to itself (a missing binary then
            surfaces as a per-target launch failure, not a whole-run abort, and
            an absent binary cannot be PATH-hijacked).

        These are enforced by the test_agent_security.py adversarial suite
        (agatk5n): shell-metachar prompts stay one token, scalar command/env
        refused, which-pinning, relative-path rejection.

    Agent Environment Is An Allowlist = decision:
      id: agenv6q
      why: >
        A subprocess inherits the WHOLE environment, so a backend would
        otherwise get GH_TOKEN, the SSH agent socket, AWS/cloud creds, and every
        API token (threat-model T1). Each profile gets an optional ``env``
        allowlist: only the named vars plus a minimal safe base (PATH, HOME,
        locale, TERM, TMPDIR — deliberately NO credential-bearing vars) reach
        the child. ``env: null`` (the default) inherits the full environment for
        backward compatibility; presets/custom profiles opt into a scoped set.
        The launch plan (agbknd7q) carries the exact ``env`` dict; the kernel
        passes it to Popen only when scoped (so the inherit path and the
        existing test popen-factories are unaffected). With the least-privilege
        push rework (agpriv8n) the resolve-conflicts agent needs no credentials
        at all, so its env can be scrubbed to the bare toolchain.
      approved-by: daniel, 2026-06-04

    Least Privilege: gitbulk Owns Every Networked Git Op = decision:
      id: agpriv8n
      why: >
        The pivotal security change of the pluggable-agent work and the
        substantive fix for threat-model T1 (P0): the dispatched agent must
        never perform a networked, credentialed, or irreversible git operation.
        Before this, prompts/resolve-conflicts.md had the AGENT run
        ``git fetch`` and ``git push --force-with-lease`` — so a prompt-injected
        or buggy/less-trusted backend could push arbitrary refs to any of the
        ~150 fleet repos.

        New division of labor for PR-centric dispatch (execk7nm):
          1. gitbulk creates the worktree (unchanged) and then PRE-FETCHES the
             PR's base into it (rebase.fetch_base) — the networked step, run
             with gitbulk's own creds, BEFORE the agent launches. A fetch
             failure removes the worktree and skips the PR (the agent could not
             rebase offline anyway).
          2. the AGENT rebases onto the already-fetched ``origin/<base>``,
             resolves conflicts, runs ``git rebase --continue`` — purely LOCAL,
             needing no network and no credentials (which is exactly what makes
             the fs+no-net sandbox viable, agsbx3k). It NEVER fetches or pushes;
             the rewritten prompt says so explicitly.
          3. gitbulk INDEPENDENTLY VERIFIES the worktree
             (rebase.verify_resolved_for_push): no conflict markers, no rebase
             in progress, HEAD advanced past the SHA gitbulk first observed.
             Only on READY + a RESOLVED verdict does gitbulk itself call
             force_push_with_lease (lease against the observed SHA). The
             verdict is ADVISORY — a spoofed ``RESOLVED`` that left markers or a
             half-finished rebase yields BLOCKED → gitbulk pushes NOTHING and
             the PR is surfaced for attention (exit 2). NO_CHANGE (HEAD
             unmoved) is benign.

        This establishes the cross-backend invariant: **the agent never touches
        a remote; gitbulk performs every networked mutation.** codeowners.md /
        migrate-*.md already followed "commit locally, gitbulk pushes"; this
        makes it uniform. Blast radius of a hostile/confused agent collapses to
        "garbage in a throwaway worktree," caught by verification before any
        push. Reuses the existing, tested rebase.py machinery
        (force_push_with_lease, the ``_git`` seam) rather than inventing new
        push code. Enforced by tests/test_rebase.py (fetch/verify gate) and
        tests/test_dispatch.py (READY→push, BLOCKED→no-push+attention,
        push-failed→attention, NO_CHANGE→benign, ESCALATED→never verified,
        prefetch-failure→skip). Builds toward agsbx3k (sandbox) and agtok2n
        (scoped tokens).
      approved-by: daniel, 2026-06-04

    Per-Profile Bubblewrap Sandbox = decision:
      id: agsbx3k
      why: >
        Defense-in-depth on top of least-privilege (agpriv8n) and env scoping
        (agenv6q) — NOT the primary control. A non-claude backend can be run in
        an unprivileged ``bwrap`` user namespace (gitbulk.sandbox) so a hostile
        or prompt-injected agent cannot read the operator's credentials or other
        clones, and (fs+no-net) cannot reach the network at all.

        Profile ``sandbox:`` ∈ {none, fs-only, fs+no-net}. ``wrap_argv`` binds
        only a read-only system toolchain (the system dirs that exist) plus the
        worktree (rw, cwd), shadows ``$HOME`` with a tmpfs, unshares
        user/pid/ipc/uts/cgroup, and for ``fs+no-net`` also ``--unshare-net``.
        Credential locations (~/.ssh, ~/.aws, ~/.config/gh) and the other ~149
        clones are simply never mounted. ``--die-with-parent`` preserves the
        execk7nm SIGTERM→SIGKILL timeout semantics.

        ``fs+no-net`` is viable for resolve-conflicts precisely BECAUSE agpriv8n
        removed the agent's need for network/credentials — the two controls
        compose. claude (the trusted native path, ProductionClaudeClient) is
        intentionally not put through the generic sandbox; sandboxing targets
        the less-trusted pluggable backends.

        Availability is capability-probed (``bwrap_available``: bwrap installed
        AND unprivileged userns actually works, since many hosts have bwrap but
        disable userns). REFUSE-IF-UNAVAILABLE is the default
        (``sandbox_fallback: refuse``): if a profile requests a sandbox the host
        can't provide, gitbulk refuses to run rather than silently downgrade to
        unsandboxed (a silent downgrade defeats the purpose). ``warn-run`` opts
        into running unsandboxed with a loud warning. Refusal is raised at
        backend construction: dispatch resolves the run default before creating
        any worktree (refuse → structural abort, cheap) and per-repo overrides
        inside the loop (refuse → skip just that PR). Linux-only; containers /
        firejail were rejected (heavier / setuid attack surface) for a one-box
        cron tool. Enforced by tests/test_sandbox.py + the adversarial
        fs+no-net / refuse tests (agatk5n).

    Scoped-Token Injection Seam = decision:
      id: agtok2n
      why: >
        Even with agpriv8n, some future tasks (e.g. a codeowners-style agent
        that must read remote state) need a token. Rather than hand the agent
        the full ambient ``gh`` auth, the design leaves a seam to inject a
        short-lived, single-repo credential: ``CommandAgentBackend(extra_env=)``
        and ``backend_for(token_env=)`` merge caller-supplied env vars into the
        child on top of the ``env`` allowlist (agenv6q). Blast radius on leak =
        one repo, expires fast. Phase 4 lands the plumbing + tests; the actual
        minting (fine-grained PAT / GitHub App installation token) is follow-on
        — the seam exists so it can be added without reworking the backend.
      approved-by: daniel, 2026-06-04

    Security Review Remediation 2026-06-04 = decision:
      id: agsecr5n
      why: >
        An adversarial security-hawk review (review-panel, security persona,
        reviewedSha c8508a0) of the pluggable-agents work confirmed 5 findings,
        all dispositioned recommend-fix. Their resolutions:

        SEC-F1 (HIGH) — the bwrap sandbox was NON-FUNCTIONAL for dispatch: a
        linked worktree's .git points into the operator clone's
        .git/worktrees/<name> (commondir → objects/refs/config/hooks), which
        wrap_argv never bound and --tmpfs $HOME shadowed; a live bwrap probe
        showed git failing 'not a git repository'. It was tested only by
        argv-shape assertions, never e2e. Fix: sandboxed agents now run in a
        SELF-CONTAINED ``git clone --no-hardlinks`` (own .git, no shared
        objects/hooks/config with the operator clone), origin reset to the real
        remote; gitbulk fetches base + checks out head OUTSIDE the sandbox, the
        agent rebases offline, gitbulk verifies + pushes. core.hooksPath is
        neutralized. Validated by a REAL bwrap e2e test (auto-skips when
        bwrap/userns absent) — the test that would have caught the original
        defect. See agsbx3k (updated) and agecln4k (the isolated-clone model).

        SEC-F2 (HIGH) — least privilege was opt-in: presets defaulted env=None
        (full inherit) + sandbox=none, so default_agent:gemini ran --yolo with
        GH_TOKEN/SSH/AWS. Fix: non-claude presets ship a scoped ``env``
        allowlist by default (agenv6q updated). Env scoping stops env-borne
        leakage only; filesystem isolation needs the sandbox (F1).

        SEC-F3 (HIGH) — no foreign-author gate: the auto-approve agent ran on
        attacker-controllable PR content at head_sha. Fix: dispatch skips PRs
        not authored by the operator unless --allow-foreign-authors, which is
        REFUSED in unattended/cron (no TTY). Closes the open part of
        threat-model T1 / §3.3-fix item 1. (Author-based gate; PRInfo carries
        no fork/head-repo field, and author!=me already covers foreign PRs.)

        SEC-F4 (MED) — sandbox_fallback:warn-run silently downgraded under cron
        with only a logging.warning. Fix: a downgrade now records a durable
        WARNING into run state AND raises ATTENTION (exit 2); the backend
        exposes ``sandbox_downgraded`` for dispatch to surface.

        SEC-F5 (LOW) — the threat model claimed the effective agent argv was
        logged; it wasn't. Fix: exec persists agent_argv (prompt elided) +
        agent_env_keys (NAMES only) per target in <key>.meta.yaml.

        Lesson recorded: argv-shape unit tests gave false confidence in a
        control (the sandbox) that did not actually work; security-relevant
        OS-confinement code needs an e2e test exercising the real binary
        (agtste9k).
      approved-by: daniel, 2026-06-04

    Sandboxed Agents Use A Self-Contained Clone = decision:
      id: agecln4k
      why: >
        The SEC-F1 fix (see agsecr5n). A bwrap sandbox can only bind whole
        directories; a linked ``git worktree`` (worktree.create_worktree) has a
        ``.git`` FILE pointing into the operator clone's
        ``.git/worktrees/<name>`` whose commondir is the clone's
        objects/refs/config/hooks — none of which the sandbox binds, and
        ``--tmpfs $HOME`` shadows the clone outright. So inside the sandbox git
        fails 'not a git repository'. Binding the clone's ``.git`` to fix that
        would re-expose its hooks/ to the auto-approve agent (plant a
        post-checkout hook that fires on the operator's next real git op).

        Therefore a SANDBOXED agent (sandbox != none) gets a self-contained
        repo via gitbulk.isolated_clone.create_isolated_clone:
        ``git clone --no-hardlinks --no-checkout`` of the operator clone (own
        .git, objects copied — not shared, default hooks), origin reset to the
        REAL remote, ``core.hooksPath`` pointed at an empty dir (so nothing in
        hooks/ runs), the PR head fetched from the real remote and checked out
        detached. All networked/credentialed steps run OUTSIDE the sandbox
        (gitbulk's job); the agent then runs bound to that dir ALONE — git works
        and there is no filesystem path to the operator clone, other repos, or
        creds. Teardown is rmtree (no worktree admin entry to unregister; guarded
        to stay under worktree_root). claude / unsandboxed agents keep the
        cheaper linked worktree. fetch_base / verify_resolved_for_push /
        force_push_with_lease operate on either workspace identically, so the
        agpriv8n flow is unchanged. Cost: a clone per sandboxed PR — acceptable
        for a personal tool dispatching a handful of conflicting PRs.

    E2E Tests For Real-Binary Security Controls = decision:
      id: agtste9k
      why: >
        SEC-F1 shipped a sandbox that never worked because it was tested only by
        argv-SHAPE assertions (asserting the bwrap argv looked right) and never
        run end-to-end — false confidence in a load-bearing security control.
        Rule going forward: OS-confinement / real-binary behavior gets an e2e
        test that actually spawns the binary. gitbulk now has a tests/e2e/ tier
        (pytest marker ``e2e``) that runs REAL git + REAL bwrap with NO network
        (a local bare repo is the origin), proving git works inside the sandbox
        over an isolated clone (agecln4k) AND — as a regression control — that
        the old linked-worktree approach fails. The tier is:
          - skipif(not bwrap_available()) so locked-down runners skip cleanly;
          - EXCLUDED from the hermetic 100%-coverage gate (run with
            ``-m "not e2e"``) — coverage of isolated_clone.py / sandbox.py comes
            from hermetic unit tests; e2e is additive behavioral confidence, not
            a coverage source;
          - run in a DEDICATED ci.yml ``e2e`` job (installs bubblewrap, relaxes
            the Ubuntu-24 AppArmor userns restriction, probes bwrap) so its
            environment-specific result never blocks the core gate.
        This honors AGENTS.md 'no network in tests' (local bare origin only)
        while closing the gap that argv-shape testing left.
      approved-by: daniel, 2026-06-04

    Repo-Level Dispatch Opens A PR = decision:
      id: dsprp7kq
      why: >
        Some fleet work is REPO-level and has no existing PR to act on — the
        canonical example (user request 2026-05-30): "every repo should have a
        CODEOWNERS listing every git user with direct-push rights AND a commit
        in the last 60 days; add the file via a PR where missing/stale." This
        is explicitly in scope per xq4npk7r ("Local Repos Are First-Class
        Citizens"; repos that need work no PR yet exists for). But the existing
        ``dispatch`` (node execk7nm) is PR-CENTRIC: it iterates
        ``my_open_prs``, creates one detached-HEAD worktree per PR head, and
        tears it down — it cannot target a repo that has no PR, and has no
        PR-CREATION path.

        Decision: a NEW ``dispatch-repo`` subcommand (NOT a mode bolted onto
        ``dispatch``). Rationale for a separate subcommand over a flag: the
        invariant chain differs (no PER_PR invariants, no PR fetch), the work
        unit differs (a repo + its default branch, not a PR head), and the
        post-exec flow differs (push a branch + open a PR, vs. worktree
        teardown). A ``--mode`` flag would scatter if/else through every step
        of the dispatch handler; a sibling subcommand keeps each clean.

        Pipeline (per selected repo): (1) gitbulk creates a disposable
        worktree on a FRESH branch off ``origin/<default-branch>`` under the
        worktree root (same safety contract as execk7nm — main clone never
        touched; worktree path verified under the root). This requires a
        default-branch-based worktree variant; the current ``create_worktree``
        is PR-head-SHA only. (2) gitbulk runs a headless Claude in that
        worktree with a PLUGGABLE ``--prompt`` (e.g. ``prompts/codeowners.md``).
        The PROMPT owns the content logic — for CODEOWNERS it computes
        (collaborators with push rights, via ``gh api .../collaborators``) ∩
        (committers in the last 60 days, via ``git log --since``), writes/updates
        the file, and commits LOCALLY. It does NOT push. (3) gitbulk inspects
        the worktree: if there are new commits AND ``--apply``, gitbulk pushes
        the feature branch to origin and opens a PR (``gh pr create`` — PERMITTED
        for agents per AGENTS.md; only ``gh pr merge`` / protected-push /
        ``repo delete`` are human-reserved) targeting the default branch. In
        dry-run (default, node 2vqp4nk6) it reports what it WOULD push/open and
        pushes/creates nothing. If the agent made no changes (file already
        correct), no PR.

        Division of responsibility is the safety crux: the OUTWARD, hard-to-
        undo mutations (branch push, PR creation) are gitbulk's, gated by
        ``--apply`` and audited in run state — NOT freeform agent actions. The
        agent only edits + commits inside a disposable worktree. gitbulk gates
        each repo on the viewer actually having push rights, reusing
        ``viewer_repo_permission`` (added with aprmn5kq) as a PER_REPO
        invariant — no point dispatching where we can't open the PR.

        New gh method required: ``create_pr(slug, base, head, title, body)``.
        Repo-set selection reuses dispatch's (repos.txt + ``--org``/``--repo``
        filters). ENUMERATING all repos of an org/user (provenant-dev ∪ dhh1128,
        beyond the curated repos.txt) is a SEPARATE future need (would add
        ``list_org_repos``/``list_user_repos``) and is DEFERRED: the capability
        is first proven on ONE repo (``dhh1128/gitbulk-sandbox`` via ``--repo``)
        before any fleet run. Resume/idempotency: re-running is safe — a repo
        whose file is already correct yields no PR, and an open gitbulk PR
        should be detected to avoid duplicates (the prompt checks for an
        existing equivalent PR/branch).
      approved-by: >
        PENDING — auto-proposed 2026-05-30 during an autonomous run. Daniel
        authorized "build the CODEOWNERS dispatch capability" in the queue, but
        the confer channel was DOWN when the specific design (subcommand name,
        flow) was put up for confirmation, so this design has NOT been reviewed.
        Confirm/adjust on return; the subcommand name and the
        gitbulk-owns-push-and-PR split are the most likely points to revisit.

    Multiprompt Packaging Future = decision:  # resolves tension fw5kq6np
      id: mprmpkg4
      why: >
        Resolved by divergence. With ``execk7nm`` choosing in-tree
        reimplementation, gitbulk no longer depends on multiprompt.py
        in any form; the forcing function that made multiprompt's
        packaging question gitbulk's concern is gone. Whether to
        extract multiprompt into its own repo with proper CI and
        release artifacts remains multiprompt's own question to
        resolve, on multiprompt's own timeline, with no coupling
        back to gitbulk. The shared-kernel path (option (b) of
        ``execk7nm``) was considered and rejected on cost-vs-leverage
        grounds; if a third consumer of the parallel-claude primitive
        appears later, reopen this node and ``execk7nm`` together
        rather than re-deriving the choice in isolation.
      approved-by: daniel, 2026-05-28
      # was: tension fw5kq6np (Multiprompt Packaging Future, deferred at Phase 0)

    Rebase PR Subcommand = decision:
      id: dieug50n
      why: >
        ``rebase-pr`` (renamed from the original ``rebase-onto-default``
        — shorter, and it rebases onto the PR's CURRENT base, which is
        usually but not necessarily the repo default) handles the common
        case where PR A merges and PR B goes BEHIND or DIRTY. Designed
        in a speculative interview 2026-05-29; four decisions:

        1. NAME: rebase-pr, no alias kept (brand-new tool, no muscle
           memory or external docs to preserve).

        2. CONFLICT HANDLING (v1): clean-rebase-only. In a disposable
           worktree, ``git rebase`` onto the fresh base. CLEAN →
           force-push. CONFLICT → DO NOT push; leave the worktree
           mid-rebase with a CONFLICT.md (node vp7n2krq) so the user
           finishes by hand. The grounding run showed the motivating
           PRs are genuinely CONFLICTING (not just BEHIND), so "abort
           and report" wouldn't actually help — but auto-AI-resolution
           then force-push is the single riskiest thing gitbulk could
           do unattended, so v1 stops at preparing the worktree. AI
           conflict resolution via the existing dispatch/exec kernel is
           the natural v2 and is deliberately deferred.

        3. FORCE-PUSH SAFETY: ``--force-with-lease=<head>:<expected_sha>``
           where expected_sha is the PR's last-observed head. An
           intervening push (by anyone) makes the lease fail and that
           PR aborts rather than clobbering. Never plain --force.

        4. TARGETING: fleet-wide, dry-run by default (the 2vqp4nk6
           gate), consistent with merge/close-stale. A future --pr
           filter (and the broader filter-args work) can narrow it.

        Eligibility gate: ``pr.needs_rebase`` (rebase-pr-only invariant)
        Passes only for mergeable_state in {BEHIND, DIRTY}. CLEAN needs
        nothing; BLOCKED is gated on review/checks not base-staleness;
        UNKNOWN/UNSTABLE/HAS_HOOKS don't indicate a stale base. There is
        NO separate pr.author_is_me invariant: my_open_prs already
        searches author:@me, so every PR the handler sees is mine by
        construction (revisit if filter-args ever let rebase-pr target
        other people's PRs — see the open filter-design discussion).

        Mechanics live in two modules to keep the gh client a pure
        network boundary and worktree.py focused on lifecycle:
        ``rebase.py`` (rebase_onto_base / force_push_with_lease, pure
        git subprocess) and the handler in commands/rebase_pr.py, which
        reuses worktree.create_worktree/remove_worktree and the
        is_worktree_in_conflict detector. The main clone is never
        touched; all git work happens in the worktree.
      approved-by: daniel, 2026-05-29

    Fleet Subset Filters = decision:
      id: flt7arg2
      why: >
        Any command can be aimed at a SUBSET of the fleet via filter
        args. Designed in a riff 2026-05-29. The load-bearing design
        choice: filters are a SEPARATE SELECTION LAYER, not invariants.
        Conflating them would be a category error — an invariant Skip
        means "this target needs human attention" and drives the exit-3
        attention signal; a filter exclusion means "the user deliberately
        scoped this target out of THIS run." A repo I globbed away must
        not show up as a skipped/attention item, and a PR I never asked
        about must not inflate the attention count. So filtering happens
        OUTSIDE the invariant machinery, before (repo filters) or after
        (PR filters) the chain runs, and excluded targets are counted
        separately ("Filtered [dims]: N repos, M PRs excluded") rather
        than reported as Skips.

        V1 DIMENSIONS (first slice): org (owner match), repo (fnmatch
        glob on the full owner/name slug), base (PR target branch),
        mergeable_state (raw GitHub enum — no friendlier aliases; the
        user explicitly accepted raw values), author (PR raiser).

        WHERE EACH PRUNES:
          - Repo filters (org, repo glob) prune the repo list BEFORE the
            invariant loop and before any per-repo fetch — so excluded
            repos cost zero API.
          - author is pushed INTO the my_open_prs search query at fetch
            time (author:<x>), not filtered after — narrowing server-side
            keeps the point cost down and is the only way to see OTHER
            people's PRs at all (the default search is author:@me).
          - PR filters (base, mergeable_state) prune AFTER fetch, since
            they read fields only present once the PR is materialized.

        READ-ONLY WIDENS / MUTATING VETOES (the safety asymmetry):
          - report (read-only) honors --author to survey anyone's PRs.
          - merge / close-stale / dispatch resolve author via
            fetch_author(spec) and will act on the resolved author's PRs
            — acceptable because those gates are still invariant-guarded.
          - rebase-pr VETOES --author with a ConfigError: it force-pushes
            the PR's head branch, which only makes sense for your own
            PRs. Honoring --author there would invite force-pushing over
            someone else's branch. This veto is the per-command
            mutating-side check the design promised; see dieug50n's note
            that there is no separate pr.author_is_me invariant precisely
            because my_open_prs is author-scoped by construction.

        CONFIG + CLI: named filter sets live under policy `filters:`
        (each a mapping of dimension→scalar-or-list); --filter NAME loads
        one. CLI flags (--org/--repo/--base/--mergeable-state/--author)
        NARROW: a CLI value on a dimension REPLACES the named set's value
        on that dimension (narrowing, not union — "I typed a flag to
        focus further" is the intuitive verb). Unknown --filter name is a
        ConfigError, not a silent empty selection.

        IMPLEMENTATION: a standalone filters.py (FilterSpec frozen
        dataclass + select_repos/select_prs/apply_pr_filters/
        fetch_author/filter_summary_line/resolve_filter_spec) so the
        selection layer is unit-testable in isolation and the five
        handlers share one code path. No import cycle: policy.py imports
        FilterSpec from filters.py, not vice versa.

        V2 DEFERRED (do NOT silently build these without revisiting the
        read-only-widens / mutating-vetoes rule above):
          1. on-disk location filter (select by where the clone lives —
             needs the local-path resolution from the local-targeting
             tension lct4rgp6 to be settled first).
          2. PR age filter (--older-than / --newer-than) — straightforward
             but wasn't in the first slice.
          3. regex repo matching as an alternative to fnmatch glob (glob
             chosen for v1 as the lower-surprise default).
          4. negation / exclusion (--not-repo, exclude an org) — the v1
             dimensions are all inclusive-match.
          5. single-PR targeting (--pr owner/repo#123) to aim a command
             at one PR — most useful for rebase-pr; interacts with the
             author veto (a single --pr that isn't yours should still be
             refused by rebase-pr).
          6. if other-people's-PRs targeting ever reaches rebase-pr (via
             #5 or a future --author lift), revisit the dieug50n note
             about pr.author_is_me becoming a real invariant.
      approved-by: daniel, 2026-05-29

    Default Branch Rename Handling = tension:
      id: rj7p4kqn
      why: >
        Some target repos may rename their default branch (main → dev,
        or vice versa) while gitbulk has open PRs against the old base.
        The pr.base_is_default invariant catches this and skips the PR,
        but the user then needs a workflow to rebase those PRs onto the
        new default. --allow-non-default-base lets a single run proceed
        but does not fix the underlying PRs. Deferred: deciding whether
        gitbulk grows a "rebase onto current default" helper for the
        rename case, or whether the user re-bases those by hand once.

        Interaction with the default-branch cache (dbcttl7d): a renamed
        default could be served stale from the cache for up to the TTL
        (7 days). This only ever causes gitbulk to be MORE conservative
        — pr.base_is_default compares the PR's base against a cached
        default that might lag reality, so the failure mode is "skip a
        PR that's actually fine," never "act on a wrong base." Acceptable
        given renames are rare and the next refresh self-heals.

    Default Branch Cache = decision:
      id: dbcttl7d
      why: >
        The per-repo invariant chain calls gh.default_branch(slug) for
        every repo (github.reachable, pr.base_is_default,
        local.default_branch_in_sync). Against a 205-repo fleet that was
        ~60s of sequential REST. Two-stage fix:

        STAGE 1 (node-less, shipped first): batch the lookups into one
        chunked GraphQL query (aliased repository() nodes, chunked at
        100 because GitHub 502s past ~150). Populates an in-process dict
        on the gh client; default_branch() reads it, falls back to
        per-slug REST on miss. ~60s → ~15-21s cold. The floor is
        GitHub's ~50ms/repo server cost.

        STAGE 2 (this node): persist resolved branches to
        ~/.cache/gitbulk/default-branches.yaml, keyed by slug with a
        per-entry fetched_at. prime_default_branches() seeds the gh
        in-process cache from fresh file entries (no network) and only
        GraphQL-prefetches stale/missing slugs. Measured 15s cold → 1.3s
        warm (12x) on the 205-repo fleet; the residual 1.3s is the
        prefetch for ~3 deleted/renamed repos that never cache.

        TTL = 7 days. Default branches change closer to never than to
        daily; a week balances cache savings against rename lag. Every
        staleness failure mode is "operate too conservatively" (see
        rj7p4kqn) — never destructive — so a generous TTL is safe.

        Per-entry fetched_at (not one file-level timestamp) so adding a
        repo to repos.txt fetches only that repo, and entries expire
        independently. The file is preserved across runs even when a
        run uses a different repos.txt subset (a different cron entry
        might), and an unresolvable slug (deleted repo) is dropped from
        the cache rather than served as a dead branch forever.

        Cache lives in a separate module (default_branch_cache.py), not
        in the gh client, mirroring org_members_cache: the gh client
        stays a pure network boundary (node ghclmp7n). The client gains
        only seed_default_branches() / cached_default_branches() so the
        cache module can hand it warm data and read back what a prefetch
        resolved.

        No invalidation command in v1 (parallel to org-members having
        --refresh-org-members but default branches being lower-stakes):
        if a rename causes a wrong skip, the user waits out the TTL or
        deletes the cache file. Add --refresh-default-branches if that
        becomes a real annoyance.
      approved-by: daniel, 2026-05-29

    Repo Cleanup Subcommand Scope = tension:
      id: jw3kpn4q
      why: >
        Split into two tracks after the devops adversarial review
        (2026-05-27) flagged that "no GC" is operationally risky the
        moment Phase 2's report subcommand starts creating a run dir
        nightly:

        TRACK A — minimum-viable retention sweep — lands in PHASE 1D
        (before Phase 2 ships). Three pieces, all small:
          1. RunState.complete() prunes runs/<old>-<sub>/ directories
             beyond `defaults.retain_runs` (policy schema; default 30).
             Same-subcommand only; never deletes the "latest" target.
          2. bin/gitbulk-cron prunes ~/.cache/gitbulk/cron/*.log older
             than `defaults.retain_cron_log_days` (default 30).
          3. Worktree-orphan sweep at run start: defined as a
             function but not wired into a CLI handler until Phase 4
             when dispatch actually creates worktrees. TODO comment
             in the code references this tension node.

        TRACK B — full `gitbulk gc` subcommand — remains DEFERRED to
        PHASE 5/6. Original scope below still stands for that track:
        (a) orphaned worktrees under the worktree root (node
        mw6kp2nq) whose creating run terminated and which are not in
        a conflict state per node vp7n2krq, (b) post-merge remote
        branches that gh shows as merged but still present on the
        remote, (c) stale local branches that correspond to
        merged/closed PRs. Open questions for the full subcommand:
        which cleanups default to --apply vs --dry-run (decision
        2vqp4nk6 suggests dry-run for everything mutating, but
        post-merge branch deletion is arguably so safe that --apply
        by default could be justified); whether to integrate with
        `git worktree prune` or do gitbulk's own walk; which
        invariants gate each cleanup (variants of pr.merged_or_closed
        and worktree.belongs_to_us). Track B comes after merge and
        close-stale ship because its correctness depends on the same
        PR-state data those subcommands consume.

    Scan And Findings Artifact Convention = tension:
      id: ck7n4pqr
      why: >
        gitbulk needs to discover work that should happen but hasn't
        — e.g., "this repo has no CI", "this README is six months
        out of date", "this repo should adopt the new auth library".
        Likely mechanism: a `scan` subcommand orchestrates an
        in-tree execute_targets run (per node execk7nm) against repos
        that pass an invariant filter, using a user-supplied prompt;
        each target's log file is the artifact; gitbulk discovers
        those artifacts and presents them via a `findings` subcommand,
        optionally feeding them back into `dispatch` to actually do
        the work. Open questions: (a) artifact format — structured
        YAML, free-form markdown, or markdown with YAML frontmatter;
        (b) artifact location — inside the repo at `.gitbulk/`
        (findings travel with the clone, but writing inside a clone
        is in tension with the local-git safety contract 7mxr4pql and
        would require a deviation: node for that subdirectory) or
        outside at `~/.cache/gitbulk/findings/<owner>__<repo>/`
        (preserves the contract, but findings don't travel); (c)
        finding lifecycle — when does a finding expire, who marks it
        resolved, can a later scan re-raise a finding the user has
        dismissed; (d) whether `scan` is its own subcommand or whether
        `dispatch --scan-only` covers the same need. Resolution
        deferred to a later phase; the execk7nm kernel now exists, so
        this tension is no longer blocked on the multiprompt
        integration decision.

    GitHub API Rate Limiting At Fleet Scale = tension:
      id: urr56mrs
      status: deferred
      why: >
        gitbulk hits the GitHub API on every run, scaling with fleet
        size. As the configured repo count or run frequency grows, or
        as we add API-heavier features, we could eventually bump into
        one of GitHub's rate limits. Recording the shape of the risk
        now so a future maintainer (human or AI) does not have to
        rediscover it.

        MEASURED HEADROOM (2026-05-29, 205-repo fleet, gitbulk report):
          - GraphQL: ~23 points per cold run, ~21 warm, against a
            5,000-points/HOUR budget. ≈217 cold runs/hour before the
            ceiling; hourly cron is ~550 points/DAY. Effectively free.
          - REST core: ~3 requests/run against 5,000/hour.
          - The REST search limit (30/min) does NOT apply: we use
            GraphQL search (node ghclmp7n / the my_open_prs query),
            which draws from the GraphQL point pool instead. This was a
            fortunate side effect of the GraphQL-first design, not a
            deliberate rate-limit hedge — note it so nobody "optimizes"
            my_open_prs back onto REST search and silently reintroduces
            the 30/min ceiling.
        Conclusion at current scale: no danger, by 2-3 orders of
        magnitude. That is why this is deferred, not resolved.

        WHY IT COULD STILL BITE LATER — the primary point/request
        budgets are not the real exposure; GitHub's SECONDARY (abuse-
        detection) limits are. Those trip on BURST PATTERNS regardless
        of how small total consumption is:
          - too many CONCURRENT requests, or
          - too many requests per minute to one endpoint, or
          - (writes) >80 content-creating requests/min, >500/hour.
        gitbulk is safe today only because it is fully SERIAL: a report
        is ~8 sequential requests over ~20s, no burst. The mutating
        paths (merge / close-stale --apply) are serial too and gated
        (one-merge-per-repo-per-run kdgmyj7o; a handful of stale closes
        per run), well under the write limits.

        REOPEN THIS TENSION WHEN any of:
          1. We parallelize API calls. The deferred "parallel chunks"
             speedup for the default-branch prefetch (and any future
             concurrent my_open_prs chunking) would fire simultaneous
             requests and is the single most likely trigger of a
             secondary limit — even though total points stay tiny.
             Parallelism MUST ship with a concurrency cap + throttle,
             not bolted on after.
          2. The fleet grows past ~1,000 repos, or run frequency goes
             sub-5-minute, such that primary GraphQL points become a
             real fraction of 5,000/hour.
          3. We add per-PR API-heavy features (e.g. fetching full
             review threads, file diffs, or CI logs per PR) that
             multiply point cost by PR count.
          4. A run ever actually observes a secondary-limit response
             ("You have exceeded a secondary rate limit").

        WHAT WE MIGHT DO WHEN REOPENED (menu, not a commitment):
          - Honor the ``Retry-After`` header on 403/429 secondary-limit
            responses. The current retry policy (ghclmp7n.d) uses fixed
            exponential backoff capped at ~2s over 3 attempts; a
            secondary limit can ask for 60s, which we would currently
            ignore. This is the cheapest hardening and the most likely
            first step.
          - Add a concurrency semaphore + token-bucket throttle in the
            gh client if/when any call path goes parallel.
          - Log ``rateLimit { cost remaining resetAt }`` (one extra
            GraphQL field) into each run's state so consumption trend
            is observable before it becomes a problem — cheap
            telemetry appropriate for an unattended cron tool.
          - Back off run frequency or shard the fleet across cron slots
            if primary budget ever tightens.

        Not doing any of these now: at ~0.5% of the hourly GraphQL
        budget and fully serial, the cost of the machinery exceeds the
        risk it would mitigate. The trigger conditions above are the
        signal to revisit.

    Local Repo Targeting Flexibility = tension:
      id: lct4rgp6
      why: >
        Reconciled from gaps.md 2026-05-29. gitbulk's targeting model is
        PR-centric and assumes a tidy ~/code/<repo-basename> layout. Three
        related open questions about how repos.txt names and resolves
        local clones. (repos.txt ALREADY accepts slug, full URL, and
        explicit local-path forms as of the 2026-05-28 onboarding fixes;
        these are the REMAINING gaps beyond that.)

        1. LOCAL-ONLY REPOS (no GitHub origin). Every subcommand is
           PR-centric (report→my_open_prs, merge/close-stale→gh API), so a
           clone with no origin has no PRs and yields silent-empty output —
           confusing. Decision needed: (a) document that gitbulk operates
           only on repos with a GitHub origin and validate-and-warn at load
           when a configured target isn't reachable (recommended — current
           silent-empty is the worst option), or (b) expand scope to
           local-only operations (worktree mgmt etc.), a significant pivot.
           Touches the local-first-class decision xq4npk7r.

        2. REPO-DISCOVERY GLOB. Let repos.txt contain a discovery root
           like ``~/code/*`` and enumerate matching dirs, extracting each
           slug from ``git remote get-url origin``. Eliminates list
           maintenance — new clones auto-join. Open: glob syntax (shell
           ``*`` vs explicit ``glob:`` prefix); behavior when a discovered
           dir has no GitHub remote (skip silently vs warn); duplicate
           slugs across dirs (the intent / intent-old case); mixing globs
           with explicit slugs in one file.

        3. FLEXIBLE LOCAL-PATH MAPPING. The computed
           ``<code-root>/<repo-basename>`` path breaks when clones nest in
           category dirs (~/code/work/foo/bar), when basenames collide
           across owners, or when basename ≠ repo name. The explicit-path
           form in repos.txt already covers the manual case; what's
           deferred is making discovery (#2) infer the slug from the
           remote so the path→slug mapping stops being positional.

        These cluster because a path/location FILTER (deferred v2 item #1
        of flt7arg2) can't be specified until how a target maps to an
        on-disk location is settled here. Resolve this before building
        that filter dimension.

    Remaining Invariant Backlog = tension:
      id: ivb5kq3n
      why: >
        Reconciled from gaps.md 2026-05-29. design-notes.md §7 specs more
        invariants than have landed. The load-bearing merge gap
        (pr.no_unresolved_threads) and timeline-aware ready_since SHIPPED
        in the 2026-05-29 merge-gate work, so merge --apply is now safe at
        full strictness; these are the LOWER-priority leftovers. None
        blocks current operation — recorded so they aren't rediscovered.

        STILL MISSING, by chain:
          - Mutating-baseline: local.no_uncommitted_in_pr_branch,
            local.recent_push_quiescence, repo.not_in_deny_list.
            ** repo.not_in_deny_list is a SAFETY gate, not a nicety: a
            per-repo kill-switch to exclude a repo from ALL mutation. The
            review-panel (2026-05-29, MNT-F5) flagged it as something to
            decide on BEFORE running --apply on a schedule against the
            real fleet — a misconfigured target with no opt-out is the
            failure mode. design-notes §7 lists it with no "not landed"
            marker, which misleads a maintainer into thinking it's active.
          - Merge-only: pr.no_blocking_label.
            ** Also SAFETY-relevant (MNT-F5): honor a do-not-merge /
            blocking label so a human can veto an otherwise-eligible PR
            out-of-band. Same "decide before scheduled --apply" caveat.
          - Rebase-only: pr.no_automerge_pending, pr.force_push_allowed
            (pr.author_is_me intentionally absent — see dieug50n/flt7arg2).
          - Close-stale-only: pr.inactive / pr.previously_warned are
            effectively realized by the stale-decision logic (2aefqte7,
            e4yuzip6) rather than as named invariants; revisit only if a
            chain-level expression is wanted.
          - Dispatch-only: repo.agentprep_verified,
            repo.agentprep_initialized, system.resources_available
            (currently handler-validated, not invariants).

        TWO ADJACENT GAPS from the same gaps.md section:
          - No ``--require NAME`` CLI flag. Node r4nzp7kq (cmdline wins
            over config) shipped --skip-check (relaxing, trips exit 4) but
            not the tightening direction. The asymmetric audit only
            exercises one side.
          - humans.exceptions / humans.always_human are honored by the
            classifier (hbcls4pq) but no subcommand surfaces "org member
            you've flagged as bot" / "outsider you treat as human." A
            read-only ``gitbulk humans`` or a report section would close
            the loop.

        Priority: most are pick-up-opportunistically when touching the
        relevant chain. EXCEPTION (review-panel 2026-05-29): the two
        safety gates above (repo.not_in_deny_list, pr.no_blocking_label)
        should be decided — implement, or consciously waive and record
        why — before --apply runs unattended on a cron schedule against
        the full fleet. At today's run-by-hand cadence they are not
        blocking; the trigger is "moving merge/rebase-pr/close-stale
        --apply into cron" (see opd3ny5k item 3).

    License Is Apache 2.0 = decision:
      id: vn4kq7pr
      why: >
        gitbulk is licensed Apache-2.0, resolving backlog item opd3ny5k #4
        (the user had stated Apache-2.0 as the intended license). Chosen
        over MIT/BSD because Apache-2.0 adds an explicit patent grant and a
        clear contribution/NOTICE framework while staying permissive —
        appropriate for a tool others may run and adapt across the fleet and
        beyond, and consistent with the public-by-intent stance of node
        6xp4kq2n. Chosen over copyleft (GPL) because gitbulk is a standalone
        CLI, not a library kept open by reciprocity, and permissive
        licensing maximizes reuse. Mechanics: a verbatim LICENSE file at the
        repo root (canonical Apache-2.0 text, appendix copyright filled with
        "2026 Daniel Hardman"); pyproject declares the SPDX expression
        `Apache-2.0` with license-files = ["LICENSE"] per PEP 639 (which
        requires setuptools>=77, so build-system.requires is bumped to
        match). Per-file source headers and a NOTICE file are intentionally
        omitted for now (single author, low-ceremony personal tool); either
        can be added if external contribution grows.
      approved-by: daniel, 2026-05-29

    Operational Deployment Backlog = tension:
      id: opd3ny5k
      why: >
        Reconciled from gaps.md 2026-05-29. gitbulk WORKS but has never
        been deployed as the unattended cron tool it's designed to be
        (the 4kp7nb2x primary mode). Four operational gaps, none a design
        question so much as undone setup — but tracked here because they
        gate "actually relying on it."

        1. INSTALL / DISTRIBUTION. RESOLVED 2026-05-29 (node dstbr5kq +
           the v0.5.0 release). The original gap: README documented only
           ``pip install -e ".[test]"``, and the editable install at
           ~/.local/bin/gitbulk silently broke if the source clone moved.
           Now gitbulk ships a single-file zipapp via ``gh release
           download … && gitbulk install`` — a STANDALONE binary in
           ~/.local/bin that no longer depends on a source clone staying
           put — plus ``gitbulk update``. PyPI/pipx is deliberately NOT
           used while the repo is private: node bootp4mq routes
           distribution through authenticated ``gh`` instead. Revisit
           pipx only if/when the repo actually goes public.

        2. BRANCH PROTECTION + CODEOWNERS (security-hawk F5, node
           shawk7nq). main has commits and the public repo exists, so
           applying protection via the user's protect-default-branch
           script is the natural next step; CODEOWNERS adds the layer
           that prevents un-reviewed merges to specific paths. The
           "Bypassed rule violations" seen in push output suggest some
           protection is configured but currently bypassable.

        3. REAL CRON DEPLOYMENT. SHAKEDOWN DONE 2026-05-29 (node shkd5crn):
           a live one-shot cron tick ran `gitbulk-cron report` headless and
           the plumbing checked out — the wrapper resolved the ~/.local/bin
           zipapp, config-root defaulted to ~/.config/gitbulk with NO
           --config-root flag, gh auth worked from cron's scrubbed
           environment, all four run artifacts plus the exit-2
           last-attention.log symlink and the refreshed ATTENTION sentinel
           were produced, and the cron daemon is confirmed running under
           systemd. Findings, all addressed: (a) the wrapper's exit code was
           not recorded inside the log file — fixed the same day by an env
           preamble + exit-line tee in bin/gitbulk-cron (with a first
           automated test for the wrapper, tests/test_cron_wrapper.py);
           (b) no MTA on this host — RESOLVED 2026-05-29 by installing msmtp +
           msmtp-mta relaying through Gmail/Workspace (app password,
           ~/.msmtprc), verified end-to-end cron -> sendmail -> msmtp ->
           inbox; a second shakedown caught that ~ is not expanded in
           passwordeval under cron's mail-delivery context (absolute paths
           required); (c) the wrapper echoed its status line to stdout on
           EVERY run, so with an MTA present cron would email on clean/
           attention runs too (and 553-reject the bare local user when MAILTO
           is unset) — fixed to echo to stdout ONLY on exit 1 + unexpected
           codes, bringing the wrapper into conformance with the tp4kq2nr /
           tmlk5pq3 channel split (MAILTO = structural-failure channel;
           ATTENTION sentinel = the daily attention channel). Verified live:
           a report tick (exit 2) with MAILTO set now sends no email and logs
           no 553. STILL DEFERRED: the recurring overnight crontab (report
           nightly, then summarize, then dispatch --apply staged in), gated
           only on creating the dispatch prompt now that the MTA is live.

        4. LICENSE. RESOLVED 2026-05-29 (node vn4kq7pr): Apache-2.0 LICENSE
           file added at the repo root and declared in pyproject (SPDX
           `Apache-2.0`, PEP 639).

        Status: #1 and #4 are done; #3 is shaken down (live one-shot ticks
        2026-05-29, node shkd5crn) with the MTA now live and the failure-only
        MAILTO policy verified — only the recurring overnight install remains,
        gated solely on creating the dispatch prompt. #2 (branch protection /
        CODEOWNERS) remains deferred until the user decides to move gitbulk
        from "I run it by hand" to "it runs itself."

    Per-Repo Lock vs Global Exclusive Lock = tension:
      id: rlkrcn3p
      why: >
        Surfaced by the review-panel 2026-05-29 (finding MNT-F1).
        Binding decision lj5pqn4kr says per-repo locks are held for the
        duration of any mutating op, so "a merge on repo A can run
        concurrently with a report on repo B," and it EXPLICITLY REJECTS a
        single global exclusive lock ("would serialize everything"). But
        the shipped Phase 5 mutators (merge.py:274, rebase_pr.py:205,
        close_stale.py) take ONLY global_lock('exclusive') — the rejected
        design — and locks.py:169 repo_lock() is dead code with no caller.

        This is an UNRECORDED divergence from a binding resolution, which
        AGENTS.md/methodology treats as a defect. Recording it here makes
        the divergence visible (resolving the "unrecorded" part); the
        substantive decision is still OPEN. Two honest exits:

          A. SUPERSEDE lj5pqn4kr: ratify global-exclusive-only as the
             intentional Phase 5 model (simplest; one mutating run blocks
             all reads, which at run-by-hand / nightly-cron cadence on a
             single machine is fine — runs are short and serial anyway),
             write a decision: node that supersedes lj5pqn4kr, and either
             delete repo_lock() or mark it reserved with a comment.
          B. HONOR lj5pqn4kr: wire repo_lock() into the mutators so
             per-repo concurrency actually works. Only worth it if a real
             need for concurrent cross-repo report+mutate emerges.

        Recommendation: A. The original concurrency goal (lj5pqn4kr) was
        speculative; nothing today needs repo-B-reads-during-repo-A-merge,
        and global-exclusive is the safer default for an unattended tool
        (no interleaving of mutating runs at all). But this is the user's
        call — do not silently pick one. Whichever wins, the dead
        repo_lock() code and the locks.py comment must be reconciled with
        the chosen node. See reviews/review-panel-2026-05-29.md (MNT-F1).
      resolution: >
        RESOLVED 2026-06-03, neither A nor B as framed — the user chose a
        THIRD, richer exit prompted by a concrete symptom: `gitbulk show
        prune-worktrees` blocking for the entire multi-minute run of
        `gitbulk prune-branches`, two commands that touch disjoint state.
        That symptom proved the global lock's coarseness was a real cost,
        not a speculative one, so option A (ratify global-exclusive) was
        rejected. Instead: lock the RESOURCE, not the operation. The single
        global lock is decomposed into resource-scoped locks (per-subcommand
        run-state, per-org cache, default-branches, sentinel, dashboard) AND
        repo_lock() is activated per-slug for both clone and remote
        mutations — which honors lj5pqn4kr's intent (per-repo concurrency)
        while going further. global_lock is retired entirely. The deliberate
        consequence the user accepted: two `merge --apply` runs may now
        overlap on DIFFERENT repos (repo_lock serializes same-repo work).
        Full model in decision rsclk7nq and docs/design/resource-scoped-
        locking.md.
      resolved-by: daniel, 2026-06-03

    Fork-Origin PR Handling For Mutating Pushes = tension:
      id: frkpr5kq
      why: >
        Surfaced by the review-panel 2026-05-29 (findings ARC-F1 HIGH,
        ARC-F4 LOW). gitbulk's mutating push paths assume a PR's head
        branch lives on origin, but a PR the user RAISED can still
        originate from a FORK (the standard "fork an org repo, open a PR"
        flow — common across a 150-repo fleet). The --author veto
        (flt7arg2) guarantees the PR is the user's OWN, NOT that its head
        is on origin.

        TWO SURFACES:
          - rebase-pr (ARC-F1, the dangerous one): force_push_with_lease
            (rebase.py:127-156) unconditionally pushes
            "origin HEAD:<head_ref>". For a fork PR the head lives on the
            fork remote, not origin (which is the upstream/base). The
            lease (head_ref:expected_sha) won't match origin's ref state,
            so the push either fails or — worst case — force-pushes onto
            an UNRELATED branch on the upstream. This is precisely the
            "single riskiest thing gitbulk could do unattended" that
            dieug50n flagged, but dieug50n never addressed the fork
            dimension and there is no test for it.
          - merge --delete-branch (ARC-F4, lower stakes): merge.py passes
            delete_branch=True for every eligible PR; for a fork PR that
            targets the fork's head branch, behaving differently (may fail
            or no-op on permissions). Server-side and non-destructive to
            local clones, and GitHub refuses unsafe deletes, so LOW — but
            it is the SAME missing abstraction on a second path.

        ROOT CAUSE: PRInfo carries no head-repository signal and the
        my_open_prs GraphQL query (gh.py:648-700) never requests
        isCrossRepository / headRepositoryOwner. So no handler CAN make a
        fork-aware choice today.

        FIX (decide the shape): add isCrossRepository (+ head-repo owner)
        to the GraphQL query and a head_repo field to PRInfo — fix the
        data model ONCE rather than per-command. Then the open product
        decision: for cross-repo PRs, does rebase-pr (a) Skip them in
        pr.needs_rebase (safest — refuse to touch fork PRs unattended), or
        (b) learn to push to the fork remote (more capable, much riskier)?
        v1 should almost certainly be (a) Skip-and-report; (b) is a future
        enhancement gated on real need. Until the data model carries the
        fork dimension, the conservative stop-gap is to treat ANY rebase-pr
        --apply as suspect on repos where the user uses fork PRs. See
        reviews/review-panel-2026-05-29.md (ARC-F1, ARC-F4).

    # ─── INSTALL & DISTRIBUTION (Phase 6) ────────────────────────────────────

    Install And Distribution Strategy = decision:
      id: dstbr5kq
      why: >
        Adopt a hybrid distribution model ported from agentprep: gitbulk
        stays pip-installable (contributors, pipx users) AND ships a single
        self-contained zipapp executable fetched from a GitHub release.
        This SUPERSEDES the design-notes §10 "out of scope for v1" line that
        deferred a bundled executable — end-user install friction (clone +
        venv + pip) is the thing stopping gitbulk from being usable on a
        fresh machine or by anyone but its author, and the agentprep model
        is already proven and maintained by the same author, so the
        marginal cost is a near-mechanical port. Cost: two distribution
        channels to keep coherent; mitigated by a single version source
        (node vsrc4pn3). The §10 entry is amended to reference this node.
      approved-by: daniel, 2026-05-29
      children:

        Single-File Zipapp Artifact = decision:
          id: zpapb4n7
          why: >
            The end-user artifact is a stdlib `zipapp` (shebang
            /usr/bin/env python3) named `gitbulk`, built by a `bundle`
            subcommand — same mechanism as agentprep's bundle.py. Chosen
            over PyInstaller/shiv/pex because it needs no per-platform build
            matrix and no compiler: it is a portable archive that runs on
            any POSIX box with Python 3.10+, which the user already has
            everywhere. Cost: not a truly static binary (system Python
            required) and the zipapp has no .dist-info, so the version must
            be baked at build time (node vsrc4pn3); both accepted because
            the target audience always has a modern Python.

        PyYAML Vendored Into The Zipapp = decision:
          id: pyvnd6kz
          why: >
            gitbulk's one runtime third-party dependency (PyYAML, used only
            via yaml.safe_load) is vendored into the zipapp by copying the
            installed `yaml` package out of the build environment at bundle
            time and dropping the libyaml C extension (*.so) — the
            pure-Python SafeLoader is sufficient. Chosen over committing a
            static PyYAML source copy (avoids third-party code + license
            churn in the repo, and the vendored version always tracks the
            pinned dependency) and over dropping PyYAML for stdlib
            tomllib/json (which would change the user-authored gitbulk.yaml
            format and touch six modules). Cost: bundling requires PyYAML
            installed in the build env — already true, it is a declared
            dependency, and the release workflow pip-installs before
            bundling.

        Gh-Authenticated Bootstrap And Self-Install = decision:
          id: bootp4mq
          why: >
            Bootstrap is `gh release download --repo dhh1128/gitbulk
            --pattern gitbulk … && ./gitbulk install`; the `install`
            subcommand copies the running binary into ~/.local/bin, marks
            it executable, and prints a shell-specific PATH hint if that dir
            is not on PATH. Reuses `gh` (already a hard runtime dependency,
            node hp4nck2v) so the same authenticated path works whether the
            repo is public or (as now) private — no second credential
            channel, and no anonymous-curl story to document while the
            repo's public release is still pending (node 6xp4kq2n records
            the eventual public intent; "private for now" is only the
            current status). ~/.local/bin is the XDG user-bin convention and
            is exactly what bin/gitbulk-cron's default-PATH branch already
            searches, so cron keeps working with no change. Cost: end users
            must have `gh` installed and authed — already required to use
            gitbulk at all.

        Update Is Notice-Only And Never Mid-Run = decision:
          id: updnc5kr
          why: >
            `gitbulk update` is explicit and the only thing that replaces
            the binary; no command ever auto-replaces it mid-run. A passive
            "newer version available" notice is printed before a normal
            subcommand but ONLY when stderr.isatty() and the check is not
            suppressed (--no-update-check / GITBULK_NO_UPDATE_CHECK=1), and
            bin/gitbulk-cron sets that env var belt-and-suspenders; the
            self-management commands (update/install/bundle) skip the notice
            since it is pointless before them. This is stricter than
            agentprep (which prints the notice even non-interactively)
            BECAUSE gitbulk's primary runtime is a 3 a.m. cron job: a nightly
            notice is log noise, and swapping a running zipapp underneath a
            long cron run risks lazy-import failures. apply_update still
            verifies sha256 and replaces atomically (tempfile -> fsync ->
            os.replace) so an interrupted update can never leave a
            half-written binary. Cost: the user must run `gitbulk update`
            (or add a separate weekly cron line) rather than getting silent
            updates — deliberate, for a tool with this blast radius. The
            TTY gate also keeps the offline-tests rule (no network in tests)
            intact: pytest runs non-TTY, so the check never fires.

        Update Refuses To Clobber A Pip Install = decision:
          id: updtg6qn
          why: >
            Because gitbulk is also pip-installable (node dstbr5kq),
            `update` first determines whether the running artifact is the
            zipapp or a pip/console-script install (the latter resolves
            inside a site-packages tree / is not a zip). If it looks
            pip-installed it refuses to self-replace and tells the user to
            `pip install -U` / `pipx upgrade gitbulk` instead. Chosen over
            agentprep's unconditional "replace sys.argv[0]" because
            overwriting a venv entry-point shim with a downloaded zipapp
            would corrupt that install. Cost: a little detection logic and
            an extra branch to test; cheap insurance against bricking a
            contributor's venv.

        Release Manifest Authenticity Is Sha256-Only = decision:
          id: shano4kp
          why: >
            update.json carries the binary's sha256 and apply_update rejects
            a mismatch; the optional HMAC manifest signature agentprep
            supports (AGENTPREP_UPDATE_SECRET) is intentionally NOT ported.
            For a single-user tool fetched over authenticated gh from the
            author's own repo, the residual threat HMAC addresses — a
            tampered release asset whose transport is nonetheless trusted —
            requires compromising the author's own GitHub repo, at which
            point the attacker could re-sign anyway unless the secret is
            held offline, which is not worth the operational burden here.
            Cost: no defense against a tampered-but-correctly-hashed release
            if GitHub itself is compromised; deferred as a tension
            (schardn7) rather than built speculatively.

        Supply-Chain Hardening Of Releases = tension:
          id: schardn7
          why: >
            OPEN: sha256-only (node shano4kp) trusts GitHub + gh transport.
            If gitbulk ever ships to third parties or the repo gains
            contributors with release rights, revisit: HMAC-signed manifest,
            Sigstore/cosign, or GitHub release attestations. Not resolved
            now because the current threat model (single author,
            private/personal repo) does not justify the key-management cost.
            Do not silently add signing without resolving this node.

        Version Single Source Of Truth = decision:
          id: vsrc4pn3
          why: >
            pyproject.toml is the single source of version truth.
            __init__.py stops hard-coding "0.0.1" and instead derives the
            version from importlib.metadata when pip-installed, falling back
            to a dev sentinel; bundle.py bakes the resolved version into the
            zipapp's __init__.py because a zipapp has no .dist-info for
            importlib.metadata to read at runtime. Chosen over the current
            duplicated literal (which silently drifts between pyproject and
            __init__.py) — this is exactly agentprep's pattern and removes a
            class of "reported version is wrong" bugs. Cost: __version__ is
            no longer a grep-able literal in source; accepted.

        Release Automation Pipeline = decision:
          id: reldst7q
          why: >
            scripts/release.py (clean-tree + in-sync-with-origin/main +
            tests-pass gates -> bump pyproject -> uv lock -> commit
            (pyproject + uv.lock) -> tag -> push tag) triggers
            .github/workflows/release.yml on the tag, which
            pip-installs, builds the bundle, generates update.json
            (latest_version / script_url / sha256), and publishes both as
            release assets. Chosen as a direct port of agentprep's proven
            release path so the author maintains one mental model across
            both tools. release.py is human-run (AGENTS.md reserves pushes
            to main and tags for humans; AI never runs it). The `uv lock`
            step (added 2026-06-04) keeps uv.lock's recorded editable-root
            version tracking pyproject (vsrc4pn3's single source of truth)
            rather than lagging a release behind — `uv lock --check` does NOT
            detect that drift, and plain `uv lock` does not upgrade pinned
            deps, so it only refreshes the project's own version snapshot.
            Workflow actions
            are pinned to node24-runtime versions per the standing
            GitHub-Actions deprecation rule. Cost: couples releasing to
            GitHub Actions availability; acceptable, CI already lives there.

        CI Release-Readiness Hardening = decision:
          id: cidvp4kr
          why: >
            Now that a formal release pipeline exists (node reldst7q), CI is
            extended three ways so a release is validated before a tag, not
            at release time: (1) the test job runs a Python matrix of 3.10
            AND 3.12 — 3.10 is the advertised floor (constraint 6jz4n2pq)
            and was previously never exercised, so a 3.11+ construct could
            ship broken to a 3.10 user; the 100% coverage gate runs on both
            (the only version-conditional code is the startup check, whose
            branches are test-faked, so coverage is identical). (2) A shared
            scripts/build_release_assets.py builds the zipapp + update.json
            and is invoked by BOTH release.yml and a push/PR CI job, so the
            asset-construction logic that otherwise first runs on a tag is
            exercised every push (build + boot + sha256 round-trip), and the
            two paths cannot drift. (3) actionlint lints every workflow so
            YAML/expression/shell errors fail fast rather than on a
            tag-triggered run that is hard to dry-run. Cost: a second matrix
            leg and two extra CI jobs (~1-2 min); accepted as cheap
            insurance for a release path with a damaging blast radius.
            Provenance/attestation remains deferred to tension schardn7.
