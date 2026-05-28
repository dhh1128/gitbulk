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
        multiprompt owns its own concurrency model per mp7kn4qz),
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
        CI workflow at .github/workflows/ci.yml runs the test suite
        across Python 3.10, 3.12, and 3.13. The devops adversarial
        review (2026-05-27) argued this is over-matrixed for a
        single-user CLI deployed to one machine. Counter-rationale
        for keeping the matrix:

          - CI cost is zero on free public-repo runners (which is
            where this lives per node 6xp4kq2n); no resource
            pressure to economize.
          - Test signal value of "new Python release broke
            something" is genuinely useful for a strict-TDD repo
            whose blast radius (real production repos) makes silent
            regressions costly.
          - The matrix is the user's only mechanism to discover
            language-level regressions before they bite cron.

        Mitigation accepted: add .python-version at repo root
        pinning the deployment version (3.12 as of Phase 1D).
        Contributors know which Python is "the real one"; the
        matrix is "additionally, we want to know about
        newer/older Python regressions."

        Revisit if the matrix begins producing flaky failures that
        are not actionable; drop the older/newer tier first.
      approved-by: daniel, 2026-05-27

    # ─── TENSIONS (deferred, do not resolve silently) ────────────────────────

    gh Client Interface Shape = tension:
      id: ghc7npqk
      why: >
        Phase 2 will introduce src/gitbulk/gh.py (or similar) as
        the exclusive channel for GitHub network traffic
        (constraint hp4nck2v). The shape is not yet designed; the
        platform-architect adversarial review (2026-05-27)
        identified that without this design recorded, whichever
        invariant lands first in Phase 2 will set the shape by
        accident.

        Forks the speculative interview must resolve before any
        Phase 2 invariant calls into gh:

          (a) Protocol class with FakeGHClient test double, OR
              concrete class with subprocess injection?
          (b) Per-method API (gh.list_open_prs(slug) →
              list[PRInfo]) OR command-style with a typed result
              (gh.run(["pr", "list", ...]) → Response)?
          (c) Where does GraphQL coalescing live — inside the
              client, or above it as a caching layer? (Decision
              gd4kp7nz says "serial + coalescing"; the layer
              question is still open.)
          (d) Where do timeouts and retries live — inside the
              client, in the invariant, or at the CLI?
          (e) Does the client carry state (rate-limit headers,
              auth-status cache) or is it stateless?
          (f) Test seam: how do invariants get a mock gh in tests
              that respect AGENTS.md's "no network in tests"?

        Resolution timing: speculative interview AT Phase 2 entry,
        with all forks surfaced in one batch (per user preference,
        memory feedback-front-load-questions). The decision node
        replacing this tension will be required before any gh-
        touching code lands.
      approved-by: daniel, 2026-05-27


    Summarize Prompt Design = tension:
      id: kw2pn7qz
      why: >
        The prompt that `gitbulk summarize` sends to claude -p has not
        been designed. The stub at prompts/triage.md captures intent but
        the actual prompt depends on the structured output format that
        `gitbulk report` produces — which is itself unbuilt (Phase 2).
        Deferred to Phase 3 entry: the speculative interview at that
        gate will design the prompt with real report output in hand.
        Resolving earlier would produce a prompt against an imagined
        data shape.

    Dispatch Execution Kernel = tension:
      id: mp7kn4qz
      why: >
        `gitbulk dispatch` needs bounded parallel claude -p execution
        against many worktrees with timeout, CTRL+C drain, and per-target
        log capture. ../origin-platform/scripts/multiprompt.py already
        implements this. Three options to resolve at Phase 4 entry:
        (a) subprocess multiprompt as-is (needs multiprompt feature
        additions for explicit-target-list input and external state-file
        path); (b) extract the execution kernel from multiprompt into a
        small standalone package that both tools consume; (c) reimplement
        the kernel inside gitbulk. User has flagged a leaning toward (b)
        or (c). Resolution deferred until Phase 4 entry, when concrete
        candidate-set shapes from gitbulk's invariants exist to design
        against. Related: tension fw5kq6np.

    Multiprompt Packaging Future = tension:
      id: fw5kq6np
      why: >
        multiprompt.py currently lives as a script inside
        origin-platform/scripts/ with no this.i, no separate CI, and no
        release artifact. gitbulk's dependency on it (if we choose option
        (a) or (b) for tension mp7kn4qz) is risky in that configuration:
        if origin-platform is restructured, multiprompt moves silently.
        Whether to extract multiprompt into its own repo with proper
        docs and CI is multiprompt's question to resolve; gitbulk's
        relationship to it is just the forcing function that makes the
        question visible.

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
        Likely mechanism: a `scan` subcommand orchestrates a multiprompt
        run (per tension mp7kn4qz) against repos that pass an invariant
        filter, using a user-supplied prompt; multiprompt leaves an
        artifact per repo; gitbulk discovers those artifacts and
        presents them via a `findings` subcommand, optionally feeding
        them back into `dispatch` to actually do the work. Open
        questions: (a) artifact format — structured YAML, free-form
        markdown, or markdown with YAML frontmatter; (b) artifact
        location — inside the repo at `.gitbulk/` (findings travel with
        the clone, but writing inside a clone is in tension with the
        local-git safety contract 7mxr4pql and would require a
        deviation: node for that subdirectory) or outside at
        `~/.cache/gitbulk/findings/<owner>__<repo>/` (preserves the
        contract, but findings don't travel); (c) finding lifecycle —
        when does a finding expire, who marks it resolved, can a later
        scan re-raise a finding the user has dismissed; (d) whether
        `scan` is a gitbulk subcommand that drives multiprompt, or
        whether the user runs multiprompt directly and `findings` only
        consumes. Resolution deferred to Phase 4 entry alongside
        tension mp7kn4qz, since both depend on the same multiprompt
        integration decision.
