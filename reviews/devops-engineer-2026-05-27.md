# DevOps / CI/CD Review: gitbulk

**Date:** 2026-05-27
**Effort level:** medium
**Reviewer role:** DevOps Engineer (adversarial)

---

## Calibration note

`gitbulk` is a single-user personal CLI tool that runs from cron on a Linux
dev box. It has no HTTP surface, no container image, no Helm chart, no
Kubernetes deployment, no `/actuator/health` probe, no Prometheus metrics,
no PagerDuty rotation. A large fraction of the role-prompt criteria
(multi-stage Dockerfiles, k8s resource limits, probe configuration,
ConfigMap/Secret hygiene, Flyway migrations, rolling-deploy concurrency,
PDBs, on-call rota, ConfigMap-vs-Secret discipline, image push gating)
**do not apply** to this codebase. They are noted once below and not
turned into findings.

What does apply, and what this review focuses on:

- The single GitHub Actions workflow (`.github/workflows/ci.yml`).
- The cron wrapper (`bin/gitbulk-cron`).
- Distribution / install model — how does a user install and update?
- Local-dev ergonomics — Makefile, scripts, contributor doc clarity.
- File-state and locking model from a "can this run safely from cron 24/7"
  perspective: orphan locks, runs-dir growth, ATTENTION sentinel lifecycle,
  worktree disk growth, crash recovery.
- Logging / observability for an unattended tool.
- The `.agent-bin/` shim model — security implications, PATH-safety.
- Branch-protection / release gating.

The author's design-rationale files (`this.i`, `docs/design-notes.md`) were
intentionally **not read**, per the review's independence rule. Where a
finding looks like it might be already-rationalized in `this.i`, that is
noted explicitly so the author can either point me at the node or accept
the finding.

---

## Evidence Inventory

Files read:

- `README.md`, `AGENTS.md`, `docs/architecture.md`
- `pyproject.toml`, `.gitignore`
- `.github/workflows/ci.yml`, `.github/workflows/copilot-review-gate.yml`
- `.github/copilot-instructions.md`,
  `.github/instructions/{infra,backend-python}.instructions.md`
- `bin/gitbulk-cron`
- `.agent-bin/{git,gh,config.json}`, `.githooks/pre-commit`
- `config/gitbulk.yaml.example`
- `src/gitbulk/{cli,paths,locks,runstate,sentinel,dashboard,__init__}.py`
- `src/gitbulk/config/policy.py`

Verified externally:

- `actions/checkout@v6` and `actions/setup-python@v6` are `using: node24`
  on their pinned tag — matches the post-deprecation requirement noted
  in AGENTS.md.

Skipped intentionally (per methodology §10 / review prompt):

- `this.i`
- `docs/design-notes.md`

Not run:

- Pytest was not executed by this review. The CI workflow asserts a 100%
  branch-coverage gate; I did not independently verify it.

---

## Executive Summary

For a Phase-1A personal CLI, the operational hygiene here is unusually
disciplined: actions are pinned to current `node24`-runtime tags, secrets
discipline is explicit (`permissions: contents: read` on CI), the cron
wrapper exists and rotates logs by timestamp, no build artifacts or
secrets are tracked, and the locking model is thought through. The
**biggest operational risks** are not in the CI workflow but in
**unbounded growth and orphan-cleanup on the runtime side**: `~/.cache/gitbulk/`
has no garbage collection of any kind, no retention policy, no stale-lock
recovery, and no `gitbulk gc` subcommand yet implemented. A second-tier
risk is **install reproducibility** — there is no lockfile, the tool
declares only loose lower-bound dependency versions, and the documented
install is "activate venv, `pip install -e`", which means two users (or
the same user six months apart) get different transitive trees. Both are
fixable cheaply; both are the right place to spend bang-for-buck.

---

## Top Findings

Ordered by bang-for-buck (highest operational risk reduction per unit of
fix effort first).

### F1: No garbage collection of `~/.cache/gitbulk/` — unbounded growth in runs/, locks/, worktrees/, and cron logs

- **Severity:** HIGH
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/runstate.py` (writes `runs/<ts>-<sub>/`,
  never deletes); `src/gitbulk/paths.py:73-83`; `bin/gitbulk-cron:11-22`
  (appends a fresh `<ts>-<sub>.log` per run); absent: any `gc.py`,
  `prune_runs()`, or retention function.
- **Finding:** Every `gitbulk` invocation creates a new
  `~/.cache/gitbulk/runs/<timestamp>-<sub>/` directory containing at
  minimum `manifest.yaml`, `state.yaml`, `summary.md`, `errors.log`,
  `invariants.log`. Every `gitbulk-cron` invocation creates a new
  `~/.cache/gitbulk/cron/<ts>-<sub>.log` file. The cron wrapper preserves
  every log forever; `runstate.complete()` only updates the
  `latest-<subcommand>` symlink and does not prune older runs.
  Worktrees (Phase 4+) will land in
  `~/.cache/gitbulk/worktrees/<runid>/...` and the architecture doc
  promises "cleans up afterward" but no GC function exists in the code
  for either runs or worktrees. `docs/architecture.md §10` explicitly
  acknowledges this gap (`gitbulk gc` "tension `jw3kpn4q`" deferred to
  Phase 5/6).
  Running nightly from cron, this is one new directory + one new log
  file per night per subcommand. With ~9 subcommands × 365 days, even
  one year produces ~3,300 directories. A worktree-creating subcommand
  that crashes between `git worktree add` and the `finally` cleanup
  block leaves a worktree (which is a full checkout of the repo) on
  disk indefinitely.
- **Operational consequence:** Disk fills up on a long-running cron
  host. Worktree orphans tie up tens or hundreds of MB per crashed run.
  Forensics is harder when `runs/` has thousands of identical-looking
  directories. Reading `dashboard.md` becomes slower (it stats every
  `latest-<sub>` symlink, but the underlying scan is fine — the human
  cost is grepping the runs tree). This is also a self-foot-gunning
  risk: if `gitbulk` itself starts failing because `~/.cache/gitbulk/`
  is full, the cron wrapper can't even write its failure log.
- **Recommendation:**
  1. Land a minimum-viable retention policy **before** the first
     `--apply` mutating subcommand ships. Trivial form: in
     `RunState.complete()`, after writing the `latest-<sub>` symlink,
     delete `runs/<old>-<sub>/` directories older than the N most
     recent or older than M days (config keys in `gitbulk.yaml`,
     defaults like `retain_runs: 30`).
  2. Add a worktree-orphan sweep at the start of each run: any
     `worktrees/<runid>/` whose `runid` is not the current one and
     whose mtime is > 1 hour old can be `git worktree remove --force`'d
     and `rmtree`'d. Pair with a startup invariant
     `cache.no_orphan_worktrees` that records what it cleaned into
     `invariants.log`.
  3. In `bin/gitbulk-cron`, after the `gitbulk` call, prune cron logs
     older than N days (`find "$LOG_DIR" -name '*.log' -mtime +N -delete`
     gated by a sanity check that `LOG_DIR` ends in `/cron`). Keep
     `last-failure.log` symlink semantics intact.
  4. Expose `gitbulk gc` as a real subcommand (currently absent from
     the `SUBCOMMANDS` list in `cli.py:21-31`) rather than burying GC
     inside every run — this gives the user an explicit way to recover
     after a disaster.

  Per-method-section note: this is the kind of "we deferred it to
  Phase 5/6" item where the author's `this.i` may already capture the
  rationale. I am not permitted to read `this.i`, so I am flagging it
  as a finding and asking the author to either point me at the node or
  accept that the deferral is now risky enough to bring forward.

---

### F2: Lock files never have stale-holder recovery; an SIGKILL'd `gitbulk` leaves no debris in `flock` itself, but the metadata file is misleading

- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/locks.py:82-118`
- **Finding:** `_file_lock()` writes pid/started_at/subcommand JSON to
  the lock file *while holding the lock*, then truncates-and-rewrites
  on each acquisition. On clean exit, `os.close(fd)` releases the
  advisory lock but the JSON metadata stays on disk. On an unclean
  exit (SIGKILL, OOM, crash mid-write), the kernel still releases the
  flock when the fd closes, but the JSON metadata on disk is now
  **stale** — it points at a pid that no longer exists. The
  `LockTimeoutError` message reads "held by pid 12345 since ... running
  merge" even though pid 12345 is long gone. There is no check that
  the pid is still alive, no truncation on release, and no `.lock.tmp
  → .lock` atomic-write discipline like the run-state code uses.
  This is harmless for correctness (the lock itself is honest;
  fcntl/flock semantics are fine) but **harmful for the 2AM operator**
  who reads the LockTimeoutError message, googles pid 12345, and finds
  it doesn't exist — wasting time on a false trail.
- **Operational consequence:** When the user is debugging "why did my
  cron run say it timed out waiting for a lock?" the JSON metadata
  will frequently lie, because the most common cause of contention on
  a single-user machine is *exactly* a previous gitbulk run that
  crashed. The user will spend time trying to find a pid that doesn't
  exist.
- **Recommendation:** In `_read_holder_metadata`, also check whether
  the recorded pid is still alive (`os.kill(pid, 0)` wrapped in
  try/except), and either drop the metadata or annotate it as
  "(pid no longer running)" in the error message. Optionally truncate
  the lock file's metadata to `{}` on clean release so that the next
  reader doesn't see a misleading prior holder at all.

---

### F3: No dependency lockfile; install is not reproducible

- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `pyproject.toml:12-17`, `README.md:30-36`
- **Finding:** Dependencies are declared as `PyYAML>=6.0` and
  `pytest>=7.0` only. There is no `poetry.lock`, no `uv.lock`, no
  `requirements.lock`, no pip-tools hash file. The CI workflow runs
  `pip install -e ".[test]"` without `--require-hashes` (`ci.yml:30-32`),
  meaning every CI run resolves transitive deps fresh. The local-dev
  README does the same. The matrix runs Python 3.10/3.12/3.13, all of
  which will pick up newer PyYAML or pytest minor/patch releases over
  time, with no way to reproduce a failure that was green yesterday
  and red today.
- **Operational consequence:** Two failure modes. (a) A breaking
  change in a transitive dependency causes CI to go red without any
  code change in this repo — and there is no record of which
  dependency tree was last-known-good. (b) Someone tries to bisect a
  six-month-old bug, can't reproduce because the installed dep tree
  is now different, and gives up. For a tool whose AGENTS.md frames
  it as "a bug in gitbulk can damage real work in real repos," this
  is more important than for a typical CLI.
- **Recommendation:** Pick a lockfile mechanism — `uv lock` is the
  lightest-touch option for a `pyproject.toml`-based project and
  produces `uv.lock`, which can be committed and consumed in CI via
  `uv sync --frozen` or `uv pip install --no-deps -r <(uv export ...)`.
  Update CI to use the locked install. Add a Dependabot config
  (`.github/dependabot.yml`) covering `pip` and `github-actions` so
  the lockfile gets refreshed on a schedule rather than rotting.

---

### F4: CI matrix tests three Python versions but the tool's runtime is a single-user dev box; the matrix optimizes for the wrong axis

- **Severity:** LOW
- **Confidence:** LIKELY
- **Location:** `.github/workflows/ci.yml:13-18`
- **Finding:** CI runs `pytest` on Python 3.10, 3.12, and 3.13. For a
  library being published to PyPI, that breadth is appropriate. For a
  personal CLI that the author runs from cron on one machine, with
  one Python version, it's wasted CI minutes and a source of false
  failures (one of the three Python versions ships a behavior change,
  CI goes red, the actual deployment is fine). `cli.py:36-39` already
  enforces `>=3.10` as a runtime check. The author either uses one
  specific Python (in which case test that one) or wants to remain
  portable (in which case the matrix is fine, but the install-doc
  needs a `.python-version` or equivalent so contributors don't
  guess). `pyproject.toml` does have `requires-python = ">=3.10"`,
  which is fine.
- **Operational consequence:** Minor. Mostly: false-failure noise on
  a personal repo where the author is the only person who will
  triage. Slightly extends CI feedback time on every PR.
- **Recommendation:** Either (a) drop to a single Python version
  matching the deployment host, or (b) keep the matrix but pin a
  `.python-version` file at the repo root so contributors know which
  one is "the real one" and the matrix is treated as "additionally,
  we want to know about regressions in newer/older Pythons." Cheap
  either way.

---

### F5: `bin/gitbulk-cron` is robust enough for happy paths but misses two cron-specific failure modes

- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `bin/gitbulk-cron:9-25`
- **Finding:** The wrapper does the basics well: it captures stdout
  and stderr to a timestamped log, sets `last-failure.log` on non-zero
  exit, and uses `set -u`. Three gaps for a tool intended to run
  unattended from cron:
  1. **No `set -e` / no `set -o pipefail`.** Failures in
     `mkdir -p "${LOG_DIR}"` and `date +...` will not abort the
     script; the next line will run with an undefined or empty
     `${LOG}` (saved by `set -u`, which catches `${LOG}` but not
     command-failure-without-failure). This is mostly cosmetic — the
     real `gitbulk "$@"` call will likely fail too — but cron will
     get an exit code that doesn't distinguish "wrapper broke" from
     "tool broke."
  2. **`PATH` is not set.** Cron runs with a minimal PATH
     (`/usr/bin:/bin` on most distros); if `gitbulk` is installed
     into `~/.local/bin/` or a venv, the cron job will fail with
     "command not found" until the user remembers to set PATH in
     the crontab itself. The wrapper could either prepend a
     known-good PATH or explicitly invoke the venv'd `gitbulk`
     binary by absolute path (passed via env var).
  3. **No flock guard against overlapping runs.** If a nightly run
     hangs (e.g., `gh` waits on `device_code` flow because the
     token expired), and cron fires again the next night, two
     gitbulks contend on the global lock. The tool's internal
     locking handles this correctly, but the *wrapper* could
     short-circuit faster with a top-level `flock -n
     ~/.cache/gitbulk/cron.lock` so the second instance exits
     quickly rather than blocking inside the lock-acquire timeout.
- **Operational consequence:** Cron is the entire deployment model
  for this tool. Each of the above produces a quiet failure mode that
  is exactly the kind of thing the user discovers two weeks later
  when wondering why the ATTENTION sentinel never trips. The PATH
  issue in particular is the single most common "my cron job doesn't
  run" cause in shops everywhere.
- **Recommendation:**
  - Add `set -eo pipefail` to the wrapper.
  - Source the user's venv (or pin `PATH=$HOME/.local/bin:/usr/bin:/bin`)
    at the top of the wrapper, or expose `GITBULK_BIN` as an
    overridable env var documented in `AGENTS.md §Where things live`.
  - Add a `flock -n` outer guard around the `gitbulk "$@"` call.
  - Optionally: emit one line of structured-ish status to stdout (so
    `MAILTO=` in crontab gives the user a one-line subject line) —
    e.g., `gitbulk report exit=0 attention=no log=…` — which makes
    the cron-email channel actually useful.

---

## Additional Patterns Noted

Below the top-5 threshold but worth recording:

- **`.gitignore` does not cover `~/.coverage`, but the working tree
  currently contains an `.coverage` file** (90 KB, line 1 of `ls -la`).
  The pattern `.coverage` *is* in `.gitignore`. Confirmed `.coverage`
  is not tracked. No action needed; noted because the file exists at
  repo root and could be mistaken for tracked.
- **`.ai-safety-check.dhh1128` is in the working tree and ignored
  correctly.** No action.
- **`.agent-bin/git` and `.agent-bin/gh` shims at front of PATH.**
  The shim model is a defensive depth-in-defense for AI-agent
  sessions: it blocks `git push` to protected branches, `gh pr
  merge`, and `gh repo delete`. The implementation (`.agent-bin/git`
  lines 4-11) is correct: it forwards to `agentprep shim` or to a
  vendored `agentprep.py`, falling back to exit 127 with a clear
  error if neither is found. **One small footgun:** if a contributor
  forgets to put `.agent-bin` on PATH (the AGENTS.md tells them to),
  *nothing protects them*. There is no enforcement of the
  PATH-prefix requirement, and no warning at gitbulk-CLI startup if
  the shims are not active. For a personal tool this is probably
  fine; for a defense-in-depth model it is a hole worth knowing about.
  Mitigation idea: `cli.py` could probe whether `git` resolves
  through `.agent-bin/` when invoked in an interactive shell and
  print a one-line reminder if not.
- **No `Makefile`, `Justfile`, or `tasks.py`.** The contributor
  workflow is documented as raw commands in README and AGENTS.md
  (`pytest -q`, `pip install -e ".[test]"`). For a tool with strict
  TDD discipline, adding a `Makefile` with targets `test`, `cov`,
  `lint`, `gc` would lower the friction of doing the right thing. Not
  a finding, just an ergonomics observation.
- **No `dependabot.yml`.** Combined with the no-lockfile finding (F3),
  this means CI and the `gh-actions` versions both drift without any
  automated nudging. AGENTS.md §"GitHub Actions" already warns about
  the Node-20 deprecation — Dependabot would have caught the
  `actions/checkout@v4 → v6` migration automatically. Worth adding
  even for a personal repo: it's a 12-line YAML file.
- **No license file.** `README.md:78` and `docs/architecture.md:262`
  both call this out as a known gap (`TODO — to be decided before the
  first remote push`). If the repo is intended to remain personal,
  fine. If it's going public, even a trivial `LICENSE` file is needed
  before others can contribute confidently.
- **CI workflow does not include `concurrency:`.** Two PRs pushing
  fast in succession will each run the full matrix. For a personal
  repo this is a non-issue (free CI minutes on a public repo); for a
  GitHub-Actions-quota-constrained scenario it adds up.
- **The CI job name says `pytest (py${{ matrix.python-version }})`,
  the workflow `name:` is `CI`, the README badge points at
  `ci.yml`.** Names match the badge URL pattern. Good. The role
  prompt's "lowercase single-word names (tests, docker, deploy)"
  convention is *not* followed, but for a non-Origin-deployed repo
  the convention is itself optional.
- **`copilot-review-gate.yml` has the same broad triggers as the CI
  workflow but operates on `secrets.GITHUB_TOKEN` with
  `permissions: pull-requests: write`.** That's correct least-privilege
  for what it does. No injection risk in the `run:` steps (PR_TITLE
  is passed via env, not inlined into the bash, which is the right
  pattern). One small issue: the `gh api … -f 'reviewers[]=Copilot'`
  swallows non-zero exit with `|| echo "..."`. That means if the API
  ever changes and Copilot can no longer be requested as a reviewer,
  the workflow silently continues to report green for years. Worth a
  comment so future-you knows to remove the `|| echo` if Copilot
  ever changes its name or removal mechanism.
- **No structured logging.** `runstate.py` writes JSONL to
  `invariants.log` and `errors.log` (good), and YAML to
  `state.yaml` / `manifest.yaml` (good). But Python module logging
  (`locks.py:23` uses `logging.getLogger("gitbulk.locks")`) is
  configured nowhere; messages emitted with `_log.debug(...)` go to
  the void. For a tool intended to be debugged at 2AM, the absence
  of any default log configuration means stderr gets nothing useful.
  Configure a stderr handler with structured-ish formatting in
  `main()`, with `--verbose` / `GITBULK_LOG_LEVEL` to dial it up.
- **`docs/architecture.md §10` notes "CI badge in README is a
  placeholder until then" (i.e., until first remote push).** The
  badge URL in README.md:3 points at `dhh1128/gitbulk` which does
  not yet exist on GitHub per the architecture doc. That's expected
  for Phase 1A; the badge will turn green on first push. No action.
- **No branch-protection-as-code.** GitHub repo settings (required
  status checks, required reviewers, signed commits, linear history)
  are not version-controlled. There is no `.github/settings.yml`
  (Probot) or Terraform/Pulumi for the repo. For a personal repo
  this is normal; for a tool whose AGENTS.md emphasizes signed
  commits and DCO discipline, it's worth at least *documenting* the
  intended branch-protection settings somewhere (a section in
  `AGENTS.md` or `docs/operations.md`) so they can be restored if
  the repo is recreated.

---

## What does not apply to this tool

(For completeness; one line each.)

- Multi-stage Dockerfile: no container.
- Non-root container USER: no container.
- Base-image pinning: no container.
- Kubernetes resource requests/limits: no k8s.
- Liveness/readiness/startup probes: no HTTP surface, no k8s.
- ConfigMap vs Secret: no k8s.
- Flyway migrations: no database.
- Helm chart parameterization: no chart.
- Docker Compose for local dev: no service to compose.
- Health endpoint compatible with k8s probes: no.
- Prometheus metrics endpoint: no, but see F1/F5 — equivalent of
  metrics for this tool is the structured state under
  `~/.cache/gitbulk/runs/` plus the ATTENTION sentinel; both are
  fine in principle, but lack GC (F1) and lack a way to surface
  rate-based abnormalities ("we've had 5 errored runs in a row")
  which is the closest analog to operational alerting.
- Horizontal scaling readiness: single-user, single-host.
- On-call rota: the user is the on-call rota.

---

## Residual Unknowns

- Whether the GC gap (F1) is consciously deferred in `this.i` as
  `jw3kpn4q` (architecture doc names this tension) — the rationale
  is in a file I am not permitted to read. The deferral may be
  justified for Phase 1A but the risk grows monotonically as more
  subcommands ship; if Phase 2 lands `report` (which writes a full
  run dir nightly), the clock starts.
- Whether the no-lockfile decision (F3) is conscious. `this.i` may
  have a node justifying it ("personal tool, accept dep drift");
  again, I can't read it. If it's conscious, ignore F3; if not, it
  is the cheapest credibility-improving change in this report.
- Whether there is any environment beyond the one dev box. If the
  author ever wants to run this on a second machine (laptop +
  desktop + cloud build host), every finding above gets more
  important, not less.

---

## Decisions Needed

1. **Accept / defer / rebut F1 (no GC):** the architecture doc
   explicitly defers `gitbulk gc` to Phase 5/6; do you want to bring
   forward a minimal retention sweep into Phase 1C/2 before any
   mutating subcommand ships, or stay deferred?
2. **Accept / defer / rebut F3 (no lockfile):** is the
   pip-resolve-fresh-every-time model intentional, or would you
   accept a `uv.lock`?
3. **Accept / defer / rebut F5 (cron wrapper gaps):** the
   `set -eo pipefail`, `PATH`, and outer `flock -n` fixes are
   ~5 lines of bash each.
4. **License decision before first remote push** (already in your
   TODO list, but noting that the badge in README.md will render as
   404-style "no such workflow" until that push happens, so the
   license decision is the gating concern, not the badge).
5. **Branch-protection-as-code or a runbook entry?** Even one
   `docs/operations.md` paragraph listing "required checks: CI;
   required reviewers: 1; require signed commits: yes" would
   suffice as a recoverability artifact.

---

## Note to author

Several findings (F1, F3, parts of F4 and F5) are exactly the kind of
"we'll do that in Phase N" items where the deferral may be intentional
and recorded in `this.i`. I followed the methodology rule and did not
read `this.i` or `docs/design-notes.md`, so I cannot confirm. If a
finding is already covered by a `deviation:` node, the right
disposition is **rebut** with a pointer to the node id, and that
rebuttal itself becomes useful evidence that the deferral is
intentional rather than forgotten.
