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
        rename over) on every record_repo_state call. Crash safety:
        if the process dies mid-run, the most recent successful
        per-repo write is durable on disk. Append-style YAML would
        be smaller but harder to validate after a partial write.

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
          - TTL: ``policy.humans.cache_ttl_hours`` (default 24).
            Stale cache → ``org.members.fresh`` invariant Fails
            with reason "org members cache older than TTL; rerun
            with --refresh-org-members".
          - Refresh command (Phase 2): a CLI flag
            ``--refresh-org-members`` on the universal preflight
            forces a fetch via ``gh api orgs/<org>/members
            --paginate``.
          - Empty / null ``policy.humans.org``: the classifier
            falls through step 3 (no org lookup), so unknown
            logins default BOT per the safer-failure-mode rule.

        Test seam: classifier is pure; tests pass canned Policy +
        canned cached members. No mocking required.
      approved-by: daniel, 2026-05-28

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

        Sub-threshold items (no LICENSE, no actions pinned by SHA,
        no secret-scanning hook, gh round-trip slug filtering) are
        deferred and known.
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
