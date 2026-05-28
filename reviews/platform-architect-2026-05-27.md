# Architecture Review: gitbulk

**Date:** 2026-05-27
**Effort level:** medium
**Reviewer role:** Platform Architect (adversarial, fresh-context)

---

## Calibration note (read first)

gitbulk is **not** an Origin platform microservice. It is a single-user,
cron-launched CLI that:

- has no HTTP surface, no Kafka topics, no DB, no Kubernetes deployment,
  no multi-tenancy, no inter-service IPC;
- operates on the user's local clones (`~/code/<repo>`) and on GitHub via
  `gh`;
- delegates auth to the user's `gh` token and `ssh-agent` and stores no
  secrets;
- produces file artifacts (`~/.cache/gitbulk/...`) consumed by the same
  user.

The bulk of the platform-architect checklist (RFC 9421, OpenAPI, URL
conventions `/v{major}/{svc}/noun`, error-code format `e.domain.code`,
Kafka topic naming, cell encryption, audience model, `/.private`
endpoints, AU-AID, l10n IDs via `l10n-svc`, native-service DB ownership,
HTTP 202 + `Operation` polling, circuit breakers between services,
graceful-degradation modes between services) **does not apply** to this
codebase. I checked each and they are non-applicable rather than absent.

What I **did** look for, and what this report covers, is platform fit at
the boundaries where gitbulk does touch the ecosystem:

- the contract between gitbulk and the platform repositories it acts on
  (via `gh` and `git`);
- the contract between gitbulk and its own future selves (run-state
  artifacts as a de-facto API the user and any reader of `~/.cache/`
  depend on);
- the contract between gitbulk and sibling personal-tooling
  (`multiprompt.py`, `agentprep`, `bin/gitbulk-cron`);
- resilience and failure behavior appropriate to a tool that mutates
  real PRs unattended;
- divergence and convergence with conventions visible in the
  surrounding Daniel-tooling ecosystem.

---

## Evidence Inventory

Files read (all paths absolute):

- `/home/daniel/code/gitbulk/README.md`
- `/home/daniel/code/gitbulk/AGENTS.md`
- `/home/daniel/code/gitbulk/pyproject.toml`
- `/home/daniel/code/gitbulk/docs/architecture.md`
- `/home/daniel/code/gitbulk/docs/methodology.md`
- `/home/daniel/code/gitbulk/.github/workflows/ci.yml`
- `/home/daniel/code/gitbulk/.github/workflows/copilot-review-gate.yml`
- `/home/daniel/code/gitbulk/.github/copilot-instructions.md`
- `/home/daniel/code/gitbulk/.github/instructions/backend-python.instructions.md` (top of file)
- `/home/daniel/code/gitbulk/.githooks/pre-commit`
- `/home/daniel/code/gitbulk/bin/gitbulk-cron`
- `/home/daniel/code/gitbulk/config/gitbulk.yaml.example`
- `/home/daniel/code/gitbulk/config/repos.txt.example`
- `/home/daniel/code/gitbulk/prompts/triage.md`,
  `/home/daniel/code/gitbulk/prompts/migrate-cd-n-deviations.md`
- All Python in `/home/daniel/code/gitbulk/src/gitbulk/`:
  `cli.py`, `__main__.py`, `dashboard.py`, `locks.py`, `paths.py`,
  `runstate.py`, `sentinel.py`,
  `config/policy.py`, `config/repos.py`,
  `util/businessdays.py`,
  `invariants/__init__.py`, `invariants/base.py`,
  `invariants/registry.py`, `invariants/runner.py`
- Listing of `/home/daniel/code/gitbulk/tests/` (test bodies sampled only
  where needed to confirm injection contracts)

Files deliberately **not** read (independence requirement per
methodology §10):

- `this.i`
- `docs/design-notes.md`

This means some findings below necessarily say *"unjustified from the code
surface"* where the rationale may exist in `this.i` and I cannot see it.
Per the review brief, that is the correct treatment — a finding rather
than reading the forbidden source.

Platform docs: `../origin-platform/docs/origin-platform/` is **not present
on this machine** (`/home/daniel/code/origin-platform/` exists but
contains no `docs/origin-platform/` subtree). I therefore could not cross-
reference platform conventions directly; my reference frame is the role
prompt itself plus the platform context described in AGENTS.md.

---

## Executive Summary

gitbulk's design is genuinely well-shaped for what it is: the local-git
safety contract, invariants framework, dry-run-by-default discipline,
exit-code-driven ATTENTION sentinel, and global-plus-per-repo lock model
are coherent and well-aligned with the cron-unattended-mutation use case.
Almost nothing in the platform-architect checklist *should* apply here,
and the author is not pretending otherwise.

The architecturally interesting findings are at the seams that **do**
exist: the run-state artifacts as a stable contract across versions, the
concurrency model's default of unbounded waits in a cron context, the
boundary the code does not yet have around `gh` (planned for Phase 2),
and a small set of layering and operational-hygiene issues whose cost is
low now but compounds as later phases land. The most important single
finding is **F1: unbounded default lock-acquisition timeout**, because in
a cron-driven mutating tool an indefinite wait can cascade across nightly
runs unnoticed.

---

## Top Findings

Ordered by bang-for-buck.

### F1: Global lock defaults to `timeout=None` (block forever) in a cron-driven mutating tool

- **Severity:** SIGNIFICANT
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/locks.py:122-132` (`global_lock`),
  `src/gitbulk/locks.py:135-149` (`repo_lock`); contract documented at
  `docs/architecture.md:80` ("Locking: `fcntl.flock` advisory") with no
  timeout discussion.
- **Finding:** Both `global_lock` and `repo_lock` accept
  `timeout: float | None = None`, and `None` means *block indefinitely*
  (see `_acquire` at `locks.py:67-80`: when `timeout is None`,
  `fcntl.flock(fd, lock_op)` blocks without `LOCK_NB`). The CLI shell
  (`cli.py`) does not yet acquire locks — that comes in Phase 2 — so the
  call sites that will eventually pick this default do not exist yet.
  But the contract is set: callers who don't pass a timeout get
  unbounded wait.
- **Ecosystem consequence:** Once Phase 2 wires the real subcommands
  through locks, a stuck or zombie mutating run holding the global
  exclusive lock will silently park the *next* cron invocation forever.
  The second invocation will not be visible as a failure (it isn't
  failing — it's blocked in `flock`) and will not produce an ATTENTION
  sentinel; it will just hold a pid waiting on the lockfile of a process
  that may have died ungracefully. The user discovers it the third or
  fifth night when nothing has run. This is exactly the failure mode the
  ATTENTION/sentinel system is designed to surface — but it can't,
  because the new process never reaches the code that would set the
  sentinel.
- **Recommendation:** Make `timeout=None` mean *no timeout, return
  immediately if held* — i.e., reverse the default to "fail fast" — or,
  more conservatively, require callers to pass a timeout explicitly
  (drop the default). The CLI driver (when added in Phase 2) should
  pick a single bounded timeout per subcommand (e.g., 5 minutes for
  read-only `report`, 30 minutes for mutating `merge`) and surface
  `LockTimeoutError` as exit code 1 with the holder metadata already
  captured in the exception. This is also the right place to log
  "previous run still in progress (pid X, started Y)" as the cron-log
  artifact so the user sees the situation without needing to inspect
  `.lock` files by hand. Record the resolution in `this.i` as a
  decision node — the default-value choice is a behavioral invariant
  that meets methodology.md §3 triggers.

### F2: Run-state artifacts are a de-facto cross-version API with no schema-version field

- **Severity:** SIGNIFICANT
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/runstate.py:75-89` (manifest schema),
  `runstate.py:108-115` (invariants.log event shape),
  `runstate.py:124-131` (errors.log event shape),
  `runstate.py:132-138` (state.yaml shape), and `src/gitbulk/sentinel.py:12-15`
  (ATTENTION line format: `"<exit> <subcommand> <runid> <summary>"`).
  Files written under `~/.cache/gitbulk/`.
- **Finding:** Five separate file formats are emitted into the user's
  cache directory: `manifest.yaml`, `state.yaml`, `invariants.log`
  (JSONL), `errors.log` (JSONL), `summary.md`, plus the `ATTENTION`
  sentinel and `dashboard.md`. None carries a schema-version field.
  `manifest.yaml` records `gitbulk_version` (line 79) which is useful,
  but the per-file shapes have no explicit version. The `show`
  subcommand (planned), the `ack` subcommand (already wired), the
  dashboard renderer (`dashboard.py`), any future external consumer
  (e.g., a status-bar widget grepping `~/.cache/gitbulk/ATTENTION`),
  and `bin/gitbulk-cron` all consume these formats.
- **Ecosystem consequence:** This is the closest thing gitbulk has to a
  public API. As the tool moves through Phase 2–6 (gh wrappers, scan,
  dispatch, cleanup), these shapes *will* change — new fields,
  renamed keys, different log levels. Any reader that survives across
  gitbulk versions (the user's own scripts, a `tail -f
  ~/.cache/gitbulk/ATTENTION` in tmux, a future external notifier
  adapter) will silently misread old runs or new runs. The
  `dashboard.py` rewriter already reads back `manifest.yaml` from
  arbitrary historical runs and assumes the current key names exist
  (`exit_code`, `completed_at`); a key rename in Phase 3 will silently
  render every old run as `"Exit: ?"`. The ATTENTION file's
  whitespace-delimited 4-field format (`{exit} {subcommand} {runid}
  {summary}`) is especially brittle — a future `summary` that contains
  whitespace at column 4 is fine, but reordering or adding a column is
  a silent breaking change for anything parsing it.
- **Recommendation:** Add a top-level `schema_version: 1` to each
  written file format (manifest, state, invariants.log per-event,
  errors.log per-event), and codify a single rule: gitbulk reads only
  artifacts whose schema_version is one of `{N-1, N}` and refuses
  (with a clear "this run was written by an incompatible version,
  ignoring") otherwise. For `ATTENTION` specifically, switch to a
  one-line JSON object — readers parsing whitespace will break on the
  switchover, which is the point: it's a clean break rather than a
  silent corruption. Record the schema-version conventions as a
  `decision:` node in `this.i` so subsequent phases don't reinvent
  this when they add new files.

### F3: `dashboard.py` imports `SUBCOMMANDS` from `cli.py` — layering inversion / source-of-truth ambiguity

- **Severity:** MINOR (rising to SIGNIFICANT as subcommands grow handlers)
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/dashboard.py:16` (`from gitbulk.cli import
  SUBCOMMANDS`).
- **Finding:** The dashboard rewriter reaches into the CLI entry-point
  module to discover the list of known subcommands. `cli.py` is both
  *the executable's argv-parser* and *the source of truth for the
  registry of subcommands*. The invariants registry (in
  `invariants/registry.py`) is correctly a separate module with a
  `register()` decorator; subcommands deserve the same treatment for
  symmetry, but currently they're a `list[tuple[str, str]]` literal in
  the CLI module.
- **Ecosystem consequence:** Today this is mildly ugly. As Phase 2+
  add real handlers, the CLI module will gain more imports (gh client,
  policy loader, runstate, etc.), and `dashboard.py` will transitively
  pull all of that in any time it is imported — including in tests
  that touch only `dashboard.py`. More importantly: a future user-
  facing tool (e.g., an external notifier or a status-bar widget) that
  wants to enumerate gitbulk's subcommands has no programmatic surface
  except importing the CLI module, which forces argparse construction
  as a side effect of import. This is the kind of small thing that
  hardens into a problem because no single phase makes it actively
  painful — every phase makes it slightly worse.
- **Recommendation:** Promote `SUBCOMMANDS` to its own tiny module
  (`src/gitbulk/subcommands.py` or `src/gitbulk/registry.py`) that
  exports `KNOWN: tuple[Subcommand, ...]` where `Subcommand` is a
  dataclass with `name`, `help`, `kind` (read-only vs mutating), and
  `lock_mode` (shared vs exclusive). `cli.py` and `dashboard.py`
  both import from there. This also kills a duplicate piece of
  knowledge that lives implicitly across `cli.py` (the list),
  `docs/architecture.md` §5 (the lifecycle table), and
  AGENTS.md (the "mutating subcommands" enumeration) — the dataclass
  field becomes the single declarative answer to "is this subcommand
  mutating?"

### F4: `bin/gitbulk-cron` conflates "needs attention" with "failed" in `last-failure.log` symlinking

- **Severity:** MINOR
- **Confidence:** CONFIRMED
- **Location:** `bin/gitbulk-cron:18-23`. Exit-code semantics defined
  in `cli.py:14-19` and `docs/architecture.md:188-197`.
- **Finding:** The wrapper symlinks `last-failure.log → <ts>.log`
  whenever `rc != 0`. The architecture explicitly distinguishes:
  - 1 = structural failure (this *is* a failure)
  - 2 = attention needed (this is a successful run with findings)
  - 3 = invariant skipped (this is a successful run with caveats)
  - 4 = overrides applied (this is an audit signal, not a failure)
  - 99 = subcommand not implemented (scaffold)

  The wrapper collapses all of those into one bucket. Worse, since
  the design expects exit 2 to be common (the daily "you have PRs to
  look at"), `last-failure.log` will be clobbered every successful
  run with findings — defeating its purpose for the *actual* failure
  case (exit 1).
- **Ecosystem consequence:** The wrapper is the user's one-line shim
  between cron and gitbulk. The exit-code design is one of gitbulk's
  most considered features — and the cron wrapper, which is the
  primary consumer of those codes, ignores the distinction. This will
  bite the first time something *actually* breaks (e.g., gh auth
  expires, exit 1) and the user finds `last-failure.log` pointing at
  yesterday's "you have 3 PRs to review" log.
- **Recommendation:** Branch on the exit code: symlink
  `last-failure.log` only for exit 1; symlink
  `last-attention.log` for exit 2/3; symlink
  `last-audit.log` for exit 4; leave the run-success path
  un-symlinked. Alternatively, since the `ATTENTION` sentinel
  already encodes attention-vs-failure, drop the symlink logic
  from the wrapper entirely and tell the user to grep
  `~/.cache/gitbulk/ATTENTION` (single source of truth — fewer
  moving parts). Also consider adding `set -o pipefail` (current
  script has only `set -u`; a failure of `gitbulk` followed by a
  failed redirect would be invisible).

### F5: No discoverable contract or seam yet for the `gh` boundary — Phase 2 will inherit whatever shape lands first

- **Severity:** MINOR (heading toward SIGNIFICANT as Phase 2 lands)
- **Confidence:** LIKELY
- **Location:** Absent by design — see `docs/architecture.md:62-64`
  ("`gh` (constraint `hp4nck2v`) — exclusive channel for GitHub
  network") and the Phase 2 marker on the runstate/dashboard sections.
  No `src/gitbulk/gh.py` or equivalent yet.
- **Finding:** The architecture commits to `gh` being the *exclusive*
  channel for GitHub network traffic. AGENTS.md commits to "no network
  in tests" via subprocess injection. The invariants framework
  already wires a `gh: Any = None  # GHClient | None — defined in
  Phase 2` placeholder (`invariants/base.py:67`). All of this is
  load-bearing, but the actual interface — what methods does `GHClient`
  expose? does it return dicts or typed dataclasses? does it batch
  GraphQL or expose per-call methods? — is not yet sketched in any
  visible artifact. The first invariant to actually call `gh` will,
  *de facto*, pick the shape. Without a recorded interface design,
  whatever Phase 2 invariant lands first becomes the design.
- **Ecosystem consequence:** This is the single biggest piece of
  cross-cutting machinery gitbulk will grow. It defines the boundary
  for all rate-limiting decisions (decision `gd4kp7nz`), the testing
  story (subprocess injection), the GraphQL coalescing design, the
  retry/timeout posture (none yet, by design — but where will it
  go?), and the place where `--allow-non-default-base` and the
  default-branch verification actually call out to GitHub. Letting
  the shape emerge implicitly from the first invariant means the
  shape will be wrong for the second invariant and every subsequent
  one — a cost that compounds quickly as Phase 2 builds out.
- **Recommendation:** Before the first Phase 2 invariant lands, run a
  speculative interview on `gh.py` and record the resulting
  `decision:` node in `this.i`. The interview should answer at least:
  (a) protocol-style or single concrete class? (b) per-method or
  command-style with a typed result? (c) where does the
  rate-limit-aware queueing live — in the client, or above it? (d)
  how is the test seam shaped — `subprocess.run` injection at
  module boundary, or a `GHClient` protocol with a `FakeGHClient`
  test double? (e) does the client carry per-call timeouts, or are
  those configured by the subcommand? (f) does the client carry a
  retry policy, or does the invariant layer? — Each of these is a
  fork that the role-prompt §7 trigger list identifies as needing a
  `this.i` node before code lands. The platform-architect concern
  here is not the answers; it is that the answers be made *explicitly*
  rather than be the residue of "what the first PR happened to do."

---

## Additional Patterns Noted (below top-5 threshold)

- **Apparent node-id reuse in docs.** `docs/architecture.md` calls
  `tp4kq2nr` "decision (exit codes)" at line 188, but `runstate.py:5-7`
  references the same id `tp4kq2nr` as "the 4-layer notification
  model." Either the same `this.i` node covers both topics (in which
  case the doc and the docstring should both say so) or the id is
  miswritten in one of the two places. I cannot verify which without
  reading `this.i`, which is forbidden. Worth a one-minute check.
- **`InvariantContext.pr`/`gh` typed as `Any`** (`invariants/base.py:67-68`)
  with phase markers in comments. This is intentional placeholder
  scaffolding for Phase 2. Acceptable, but note that the moment a
  real type lands these fields stop being optional and the `None`
  default per-Universal-invariant case becomes a small footgun
  (a Universal invariant that accidentally touches `ctx.pr` blows
  up at runtime rather than at type-check time). Consider three
  context types or a discriminated union when Phase 2 lands.
- **`runstate.RunState.begin` rejects same-second concurrent runs**
  (`runstate.py:72`, `mkdir(exist_ok=False)`). Second-resolution runid
  + global exclusive lock means this is essentially impossible for
  mutating subcommands, but two read-only `report` runs starting in
  the same wall-clock second under the shared lock could collide.
  Worth a one-line "if exists, sleep 1s and retry once" or
  microsecond-suffix the runid.
- **`state.yaml` rewritten in full on every `record_repo_state`.**
  (`runstate.py:132-138`). With 150 repos this is fine. If the tool
  is ever pointed at a much larger fleet this becomes O(N²) bytes
  written to disk. Bounded by GitHub rate limit anyway, so probably
  fine. Note for future scale.
- **`paths._normalize_slug` collision potential.** `/` → `__` mapping
  means `owner/foo__bar` and `owner__foo/bar` map to the same
  filesystem name. Astronomically unlikely in practice for GitHub
  repo names; recording for completeness.
- **No formal POSIX-only constraint.** `fcntl.flock` is POSIX-only;
  the tool will not function on Windows. AGENTS.md is silent on this.
  Probably worth a one-line constraint node in `this.i` so a future
  contributor doesn't try to make it Windows-compatible.
- **`paths.py` mixes path helpers, side-effecting directory creation
  (`ensure_directories`), and clock-driven `new_runid`.** Three
  responsibilities in one module. Minor cohesion smell; not worth
  splitting until a second consumer appears.
- **CI uses `actions/checkout@v6` and `actions/setup-python@v6`.**
  Both are node24-runtime. Compliant with the deprecation guidance
  in `~/.claude/CLAUDE.md`. Calling out as positive.
- **The "no network in tests" rule (AGENTS.md) is contract-only — the
  current code has no network surface to test against.** The rule
  becomes load-bearing in Phase 2; the seam where it will be honored
  is exactly the `gh.py` interface flagged in F5. Same concern, viewed
  from the test side.
- **`copilot-review-gate.yml`** is a sensible cross-repo convention
  but is gitbulk-side scaffolding; its design has no platform-fit
  implications for gitbulk itself.

---

## Intentional Divergences Noted

These are not findings. I list them so the author can see I understood
them as intentional and didn't double-count them.

- **`gh` exclusive for GitHub network** (`hp4nck2v`) and **no rate
  limiter, serial + GraphQL coalescing** (`gd4kp7nz`) — both are
  documented in `docs/architecture.md` §3 and §4.
- **Mutating subcommands default to `--dry-run`** (`2vqp4nk6`).
- **100% branch coverage with a `deviation:` node escape hatch**
  (`cn4pk7zq`) — explicitly captured in AGENTS.md.
- **No license yet** — explicitly tracked as "TODO before first remote
  push" in README.md and `docs/architecture.md` §10.
- **No production code beyond scaffold yet** — Phase 1A status is
  explicit in README.md and `docs/architecture.md` §10. All
  "stub returns 99" findings would be false positives at this phase.
- **`agentprep` integration via `.githooks/pre-commit` and `.agent-bin`
  shims** — cross-cutting AI-safety mechanism, not gitbulk-specific.
- **`docs/methodology.md` adopted from the broader Daniel-tooling
  ecosystem** — `this.i` discipline, deviation node convention, etc.

---

## Residual Unknowns

Things I could not verify without reading `this.i` or `docs/design-notes.md`,
both of which the review brief forbade:

- Whether `tp4kq2nr` legitimately covers both exit codes *and* the
  notification model, or is a doc/code id mismatch (F1 in
  "Additional Patterns").
- Whether the `gh` boundary shape (F5) has already been sketched in
  `this.i` as a planned node and only the code is missing, or
  whether it is genuinely unrecorded.
- Whether the schema-versioning concern (F2) has already been raised
  as a tension and deferred, or whether it is genuinely absent from
  the intent tree.
- Whether the timeout default (F1) was a considered choice with a
  recorded `why`, or an unexamined argparse-style "what's the most
  permissive default."

If any of these is already resolved in `this.i`, the corresponding
finding downgrades to "doc-fidelity nit" or disappears entirely.

---

## Decisions Needed

These are open architectural questions where the answer affects more
than the immediate code change and merits the user's explicit
decision before the next phase lands:

1. **Default lock-acquisition timeout.** F1. Pick a number, or pick
   "fail fast (timeout=0)," and put it in `this.i`. Do not let
   `None=block forever` survive into Phase 2's first call site.
2. **Schema versioning convention for cache artifacts.** F2. Decide
   the convention once, apply it to every file gitbulk writes,
   including `ATTENTION`. The cost of retrofitting later is real.
3. **`gh` interface shape.** F5. Run the speculative interview
   before Phase 2's first `gh`-touching invariant lands.
4. **Cron-wrapper exit-code semantics.** F4. Either teach the
   wrapper to honor the 1-vs-2-vs-3-vs-4 distinction, or remove
   the symlink logic in favor of `ATTENTION` as the single source
   of truth.
5. **Subcommand registry as its own module.** F3. Trivial change,
   but the dataclass shape — what fields a subcommand declares
   beyond `name` and `help` — is a design decision that should
   land before more subcommands acquire real handlers.

---

## Severity Summary

- **Critical:** 0
- **Significant:** 2 (F1, F2)
- **Minor:** 3 (F3, F4, F5) — F5 will rise to SIGNIFICANT once Phase 2
  begins picking up the `gh` boundary
- **Additional patterns:** ~9 small items, all below the top-5 threshold

No finding manufactured against non-applicable platform criteria. The
shape of this report is necessarily different from one written against
an Origin microservice; that is the calibration the brief asked for.
