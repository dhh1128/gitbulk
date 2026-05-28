# Security Review: gitbulk (Phase 2 close-out)

**Date:** 2026-05-28
**Effort level:** medium (breadth-first), with the requested threat-model section deepened
**Reviewer role:** security-hawk
**Calibration:** single-user personal CLI, no HTTP surface, no DB, no
multi-tenancy. Service-style criteria (CSRF, IDOR, JWT, OAuth, CORS,
cell-boundary sealed-box) do not apply and are explicitly out of scope.
The focus is the requested threat: **bad software on the user's dev box
coercing gitbulk into damaging 150 real GitHub repos.**

**Context sources read:**
- `AGENTS.md`, `README.md`, `docs/architecture.md`
- `pyproject.toml`, `.github/workflows/ci.yml`,
  `.github/workflows/copilot-review-gate.yml`,
  `.github/dependabot.yml`, `.github/copilot-instructions.md`
- `bin/gitbulk-cron`
- `.githooks/pre-commit`, `.agent-bin/gh`, `.agent-bin/git`,
  `.agent-bin/config.json`
- Every file under `src/gitbulk/` (`cli.py`, `subcommands.py`, `gh.py`,
  `locks.py`, `paths.py`, `runstate.py`, `sentinel.py`, `gc.py`,
  `dashboard.py`, `classifier.py`, `org_members_cache.py`, `pr_info.py`,
  `config/repos.py`, `config/policy.py`, `commands/report.py`,
  `invariants/{base,registry,runner,catalog}.py`)
- `config/gitbulk.yaml.example`, `config/repos.txt.example`
- The role prompt at `../origin-platform/prompts/security-hawk.md`

**Context deliberately NOT read** (per the invoker's independence rule):
`this.i`, `docs/design-notes.md`. Several findings below name the
absence of justification visible from the code surface; some of those
rationales may exist in `this.i`. Where that is the case, the finding's
right resolution may be "annotate the rationale in code" rather than
"change the behavior."

---

## Evidence Inventory

- All Python sources are list-form `subprocess.run`; **no `shell=True`,
  no `os.system`, no `eval`/`exec`** anywhere in `src/`.
- All YAML loaders are `yaml.safe_load`; no `pickle`, no `yaml.load`,
  no untyped JSON-driven object construction.
- Phase 2 is genuinely read-only at the gh layer: the `GHClient`
  Protocol exposes only `authenticated_user`, `org_members`,
  `default_branch`, `my_open_prs`. There are no merge/push/close/delete
  surfaces yet (gh.py:36-86).
- Local-git invariants restrict subprocess to the read-only allow-list
  `rev-parse`, `remote get-url`, `symbolic-ref` (catalog.py:198, 221,
  269), matching the local-git safety contract in AGENTS.md §"Hard
  rules / Local-git safety contract".
- Locks are POSIX `fcntl.flock`, with timeout + holder-PID metadata
  (locks.py:101-148). The metadata file is `0o644` (locks.py:143).
- Test suite never reaches a network: gh is dependency-injected
  (FakeGHClient at gh.py:111-196) and every test file goes through it.

---

## Executive Summary

The Phase 2 surface is small and well-disciplined. The auth model
(delegate everything to `gh` and `ssh-agent`), the lock model, the
exit-code-driven sentinel, and the typed `Subcommand` registry all
withstand adversarial scrutiny for **what's currently shipped**. The
hard rule "no working-tree mutation, ever" is consistently honored by
the invariant catalog: every git invocation is on the read-only
allow-list.

What the review does surface — and what the user should care about
most — are two **structural exposures that become critical at Phase 5**
when the mutating subcommands land:

1. **`code_root / name` slug-to-path expansion accepts `..` segments**
   (config/repos.py:83, paths.py:_normalize_slug only filters `/`).
   A malicious `repos.txt` line (`foo/..` or similar) makes
   `local_path` resolve to `~/` or a sibling repo. In Phase 2 this is
   bounded to read-only git probes; once `dispatch` / `rebase-onto-
   default` create worktrees off `local_path`, this becomes a path-
   traversal write primitive.
2. **`ProductionGHClient` defaults `gh_path="gh"` (gh.py:288), so
   every GitHub call resolves through `$PATH`**. A compromised
   `~/.local/bin`, a compromised venv `bin/`, or a malicious entry
   prepended to the user's shell PATH silently substitutes a fake
   `gh` for every gitbulk invocation. The substitute can authenticate
   any future Phase 5 mutating call against the real GitHub token
   stored in `~/.config/gh/`.

Everything else is medium-or-lower and either defense-in-depth or a
Phase 5 forward-look concern.

---

## Threat model: "bad software on my dev box"

The user asked for this section explicitly. Here is what the four
sub-scenarios can do, ranked by realistic impact.

### Scenario A — RCE on the WSL2 box (attacker == user)

If the attacker is `daniel@wsl`, gitbulk's defenses are irrelevant:
the attacker already has the gh token, the ssh-agent, and write access
to `~/code/`. Gitbulk's only contribution to the attacker's life is
**convenience**: it can be tricked into doing bulk operations the
attacker would otherwise have to script.

The relevant question is: **can the attacker make gitbulk's existing
audit trail under `~/.cache/gitbulk/runs/` hide the actions?** Answer:
yes, partially. The runstate writes are plain files with default umask
(typically 0o644). An attacker with the user's uid can rewrite
`manifest.yaml`, append fake entries to `invariants.log`, or replace
the `latest-report` symlink. There is no hash chain, no detached
signature, no append-only WORM convention. *This is acceptable* for a
personal tool — but worth saying out loud: **the audit trail is
exactly as trustworthy as the dev-box uid**, not stronger.

### Scenario B — supply-chain compromise of a Python dependency

Today the only runtime dependency is `PyYAML>=6.0` (pyproject.toml:13).
Test extras add `pytest` and `pytest-cov`. uv.lock is committed and
the CI installs with `uv sync --frozen` (ci.yml:34). Dependabot
weekly bumps are configured (dependabot.yml:9-16).

A compromised `PyYAML` could redefine `safe_load`. Every config read
(`policy.py:269`, `org_members_cache.py:118`, `runstate.py:171`,
`dashboard.py:29`) would then be attacker-controlled. The attacker
would gain:
- Arbitrary attribute values inside `Policy` / `RepoOverride`.
- The ability to inject extra slug entries into `policy.repos`.
- For Phase 5, the ability to flip `merge_policy="strict"` to
  `merge_policy="ci-only"` org-wide, or inject `extra_checks: []`
  that bypass invariants.

Mitigations in place: uv.lock pinning + dependabot watch + the very
small dependency footprint (just PyYAML). Mitigation **not** in
place: no hash-verifying install in CI (`uv sync --frozen` pins
versions; whether it verifies wheel hashes depends on the lockfile
contents — uv lockfile does carry hashes, so this is OK in practice).

The most acute supply-chain risk is therefore **transitive packages
the user pip-installs into the same venv** (gh wrappers, ssh-agent
helpers, dev tools). Those bypass gitbulk's pyproject.toml entirely.

### Scenario C — compromise of the `gh` CLI itself

If `gh` is replaced (either by binary swap in `~/.local/bin` or by
PATH hijack), gitbulk has **no defense**. It delegates auth, transport,
parsing, and rate-limiting all to `gh` (constraint `hp4nck2v` per
architecture.md:67, gh.py:298-355). Every JSON shape the production
client parses (gh.py:364, 422) comes back from `gh`. A malicious
`gh` could:
- Lie about `authenticated_user` to pass the `gh.authenticated`
  invariant (catalog.py:84-93).
- Lie about `default_branch` so that `pr.base_is_default`
  (catalog.py:343-357) returns Pass for any PR.
- Lie about `my_open_prs` to inject phantom PRs that, once mutating
  subcommands land, become merge / close targets.

This is **the single biggest "bad software" risk surface**, and
gitbulk's architecture makes it unavoidable: `gh` is a singleton trust
boundary. The realistic mitigation is **not** to verify `gh` (the
user has decided it's the auth substrate) but to **pin its path**
(see F2 below).

### Scenario D — malicious commit to gitbulk's own repo

A malicious PR to this repo that modifies (e.g.) `subcommands.py` to
flip `mutating=True → mutating=False` on `merge`, or to silently drop
`gh.authenticated` from `_GH_TOUCHING_CHAIN`, would bypass every
defense. The repository's defenses against this are:
- The `agentprep verify` pre-commit hook (`.githooks/pre-commit`),
  which blocks AI-authored commits without certification but is
  bypassed by `AGENTPREP_NO_AI=1` (line 6). Honor system.
- The CI workflow (ci.yml) runs pytest + 100% branch-coverage gate.
  100% branch coverage **does not** prove correctness of the
  invariant chain composition; a malicious change to
  `_GH_TOUCHING_CHAIN` (subcommands.py:24-43) that drops
  `gh.authenticated` would still pass all current tests.
- No CODEOWNERS, no required reviews, no signed commits.

For a single-author repo this is reasonable. It does mean **the
adversarial value of the test suite as a defense against malicious
self-modification is approximately zero**. The defense is "Daniel
reads diffs before merging." If that breaks down, gitbulk has no
backstop.

---

## Top Findings

Ordered by bang-for-buck — highest realistic risk reduction per unit
of fix effort, first.

### F1 (CRITICAL, LIKELY): repos.txt slug regex accepts `..` segments; `code_root / name` joins them into a path-traversal primitive

- **Severity:** CRITICAL (against Phase 5; HIGH against Phase 2)
- **Confidence:** LIKELY (path arrives, but no write happens through it in Phase 2)
- **Location:** `src/gitbulk/config/repos.py:18` (`_SLUG_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")`), `src/gitbulk/config/repos.py:83` (`local_path=code_root / name`), `src/gitbulk/paths.py:16` (`_SLUG_PATTERN = re.compile(r"^[^/]+/[^/]+$")`)

**Finding.** The slug regex rejects `/` and (in `repos.py`) whitespace,
but accepts every other character — including `..`. A `repos.txt`
line of:

```
attacker/..
```

passes validation, becomes `RepoEntry(slug="attacker/..", owner="attacker", name="..", local_path=~/code/..)`. Note that `~/code/..` resolves to `~/`. Every per-repo invariant then runs `git -C ~/ rev-parse --is-inside-work-tree` (catalog.py:198) against the user's home directory — which **is** likely a git working tree for some other repo, so the probe may falsely succeed.

A more weaponized slug:

```
attacker/..%2F..%2Fother-repo     # if the input layer ever URL-decodes
```

or simply

```
provenant-dev/../../etc
```

which yields `name="../etc"`, `local_path=~/code/../etc = /home/daniel/etc`.

**Why it matters in Phase 2.** Read-only: bounded. The worst that
happens is that gitbulk probes the wrong directory and either Skips
(falls through `local.exists`) or returns spurious "remote does not
match" results. Annoying, not damaging.

**Why it matters in Phase 5.** `paths.worktree_dir(runid, slug, root)`
uses `_normalize_slug` which replaces `/` with `__`, giving e.g.
`~/.cache/gitbulk/worktrees/<runid>/attacker__..` — that path's
`/..` doesn't escape until the worktree code (Phase 4 dispatch / Phase
5 rebase) does `git worktree add <path>`, and git itself may or may
not refuse. The mutating subcommands `rebase-onto-default` and
`dispatch` use `local_path` directly when constructing worktrees, per
AGENTS.md §"Worktree path verification". A `..`-bearing slug breaks
that verification's assumption.

**Exploit path.** Attacker (any of scenarios A/B/C/D above) writes a
crafted slug into `~/.config/gitbulk/repos.txt`. Once Phase 5 mutating
subcommands run, the per-repo branch operates against the wrong
directory — e.g. force-pushing a rebase to the wrong upstream, or
writing a worktree into an arbitrary path under `~/`.

**Recommendation.** Tighten the slug regex in **both** locations to
forbid `.` and `..` segments and to forbid path-meaningful characters:

```python
_SEGMENT = r"[A-Za-z0-9._-]+"
_SLUG_PATTERN = re.compile(rf"^{_SEGMENT}/{_SEGMENT}$")
# and after match, reject if either segment is "." or ".."
```

After parsing, **also** assert
`local_path.resolve().is_relative_to(code_root.resolve())`
before passing to any invariant that uses it. Belt-and-suspenders is
appropriate here because the slug is user-controlled config and the
downstream consequence (Phase 5 worktree write) is irreversible.

---

### F2 (HIGH, CONFIRMED): `ProductionGHClient(gh_path="gh")` resolves the gh binary via $PATH; no integrity check on the executable

- **Severity:** HIGH
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/gh.py:285-295`, `src/gitbulk/commands/report.py:240` (`refresh_gh = ProductionGHClient()`), `src/gitbulk/commands/report.py:291` (`gh = ProductionGHClient()`)

**Finding.** Every production gh invocation goes through
`subprocess.run([self._gh_path, ...])` with `gh_path` defaulting to the
bare string `"gh"` (gh.py:288). This delegates resolution to the
inherited PATH. AGENTS.md §"AgentPrep AI Operating Rules" recommends
the user prepend `.agent-bin` to PATH in agent shells — meaning
**`.agent-bin/gh` (a Python-dispatched shim) is the binary gitbulk
will invoke** when run interactively from such a shell. That shim
calls `agentprep shim ... gh "$@"`, which is itself a trust dependency
not under gitbulk's control.

More importantly, **any** $PATH entry the attacker can prepend (e.g.
by injecting into `.bashrc`, into a venv `bin/activate`, into a `setup.py`
install hook, or into the cron environment via a wrapper) silently
replaces `gh`. The fake `gh` can:
- Pass `authenticated_user` so `gh.authenticated` invariant goes
  green (catalog.py:84-93).
- Return arbitrary `my_open_prs` payloads — phantom PRs that, in
  Phase 5, become merge / close targets.
- Once Phase 5 lands and calls e.g. `gh pr merge`, replace it with a
  call against attacker-chosen repos.

`bin/gitbulk-cron` pins PATH to `~/.local/bin:/usr/local/bin:/usr/bin:/bin`
(line 50) **only for the gitbulk binary lookup**, and only when
`GITBULK_BIN` and `VIRTUAL_ENV` are both unset. Even when that branch
runs, the inherited PATH passed to the gitbulk **child process**
(line 79) is whatever cron handed in, *not* the curated `~/.local/bin:…`
PATH — `export PATH=…` set inside `bin/gitbulk-cron` does propagate to
children via subshell environment, so in practice this branch is OK.
But the other two branches (`GITBULK_BIN` set, `VIRTUAL_ENV` set) do
**not** sanitize PATH at all; whatever cron's PATH is, gitbulk
inherits, and `gh` resolves against it.

**Exploit path.** Attacker drops a wrapper at `~/.local/bin/gh` that
calls the real `/usr/bin/gh` for read operations but rewrites
`merge`/`close-stale` arguments for Phase 5 mutating operations.
Gitbulk has no way to know. The attacker leaves no trace in
`~/.cache/gitbulk/runs/` because the audit log records the *requested*
operation, not what gh actually did.

**Recommendation.**
1. At process startup (in `cli.main` before any handler runs), resolve
   `gh` once via `shutil.which("gh")`, **explicitly excluding** `.agent-bin`
   and any non-system prefix, and store the absolute path on a module
   constant. Pass that absolute path to every `ProductionGHClient()`.
2. Optionally `stat` the resolved binary and reject if it's not owned
   by root (or by the user) and not world-writable.
3. In `bin/gitbulk-cron`, set PATH **unconditionally** to the curated
   list before launching gitbulk, regardless of which `GITBULK_BIN`
   branch was taken.

This is the single highest-impact change in the report. It removes
the most plausible "bad software" attack against Phase 5.

---

### F3 (HIGH, LIKELY): no umask discipline — audit trail and lock-holder metadata default to world-readable

- **Severity:** HIGH on a multi-user host; MEDIUM on the user's single-user WSL2 box
- **Confidence:** LIKELY (depends on the user's umask; default is 022 → 0o644)
- **Location:** `src/gitbulk/locks.py:143` (`os.open(path, ..., 0o644)` — explicit world-readable mode on lock file), `src/gitbulk/runstate.py` and `src/gitbulk/paths.py:ensure_directories` (all use default umask)

**Finding.** Every file gitbulk writes under `~/.cache/gitbulk/` —
runs, summaries, errors, manifests, the org-members cache, the
ATTENTION sentinel — uses Python's default file-creation mode, which
combined with a default umask (022) yields `0o644`. The lock file is
explicitly `0o644` (locks.py:143). On a single-user WSL2 box this is
moot. On any shared host (e.g. if the user ever pulls this onto a
shared dev VM, an HPC login node, or a CI scratch home), every other
local user can read:
- The repo list (which orgs and which projects the user contributes to).
- Run history (PR titles, repo names, classification decisions).
- The lock holder pid + start time + subcommand (a process map).

**Why it matters for the threat model.** A non-uid-daniel attacker
shouldn't be in scope per the calibration. But scenario B (compromised
dependency) and scenario C (compromised `gh`) both run as the user's
uid and therefore can read these anyway. The **bigger** concern is the
*opposite* direction: gitbulk writes to `~/.cache/gitbulk/` with
exact-uid ownership but world-readable mode, meaning anyone on the
host who momentarily gets a different uid (an LXC container, a
docker-with-host-mounts development setup, a misconfigured nfs share)
sees the audit trail. The mitigation is one line.

**Exploit path.** Speculative: a docker container mounted with
`-v $HOME:/home/daniel` exposes the runs/ directory to anything that
runs inside. Common in development workflows.

**Recommendation.** Add at the top of `cli.main()`:

```python
os.umask(0o077)
```

and change `locks.py:143` from `0o644` to `0o600`. This forces every
gitbulk-created file to be uid-only. There is no downside on a
single-user host; this is pure defense-in-depth.

---

### F4 (MEDIUM, CONFIRMED): `--refresh-org-members` runs BEFORE the global lock, racing concurrent runs

- **Severity:** MEDIUM
- **Confidence:** CONFIRMED
- **Location:** `src/gitbulk/commands/report.py:239-250` (network call), vs. `src/gitbulk/commands/report.py:256-262` (lock acquisition)

**Finding.** The pipeline order in `report_handler` is (1) load config,
(2) **optional cache refresh via `refresh_cache(refresh_gh, …)`**,
(3) acquire global lock. The refresh writes the org-members cache file
under `~/.cache/gitbulk/org-members/<org>.yaml`. Two concurrent
`gitbulk report --refresh-org-members` invocations can both fetch from
GitHub and both write the cache file. The write is atomic
(`os.replace` at org_members_cache.py:147), so the file never tears,
but **the gh network call happens outside the lock**.

**Why it matters for the threat model.** Marginally. Two effects:
1. The audit trail records the refresh as having "happened" with a
   `fetched_at` timestamp, but the actually-used cache during the
   chain run may be the other process's write. If the two runs see
   different org membership (e.g. the attacker just added themselves
   to the org and is racing to land their PR), the human-classified
   author may flip mid-run. In Phase 2 read-only this is bounded; in
   Phase 5 close-stale / merge it changes which PRs are eligible.
2. The "refresh failure → exit 1 without sentinel" branch
   (report.py:246-250) returns before any RunState exists, so the
   failure leaves *no trace* in `~/.cache/gitbulk/runs/`. The cron
   wrapper records it via `last-failure.log`, but the in-tool audit
   log is empty.

**Recommendation.** Move the refresh inside `_run_under_lock`. The
RunState is begun there; the refresh becomes an auditable event with
a recorded `WARNING` if it fails. The lock budget of 300s
(report.py:67) is more than enough to absorb a `gh api orgs/<org>/members
--paginate` call.

---

### F5 (MEDIUM, LIKELY): pre-commit hook bypass `AGENTPREP_NO_AI=1` is honor-system; combined with no required reviews, compromised local dev shell can land arbitrary changes

- **Severity:** MEDIUM (scenario D in the threat model)
- **Confidence:** LIKELY
- **Location:** `.githooks/pre-commit:6-8`; no CODEOWNERS file; CI workflow has no required-review gate (`.github/workflows/ci.yml`)

**Finding.** The pre-commit hook is the only guard preventing a
malicious AI-driven commit from landing. It is bypassed by
`AGENTPREP_NO_AI=1`. If the user's `.bashrc` or shell config is
compromised to export that variable, every subsequent commit silently
bypasses verification. There is no remote-side enforcement: the
repository has no branch protection rules visible in this checkout, no
CODEOWNERS, no required-reviewer count. The CI workflow gates merges
only on tests + coverage; a malicious PR that passes both lands as
long as the user clicks merge.

The 100% branch-coverage gate is **not** a defense against the kinds
of changes a sophisticated attacker would make. Examples of changes
that pass 100% coverage but break gitbulk's safety:
- Drop `gh.authenticated` from `_GH_TOUCHING_CHAIN`
  (subcommands.py:24-31). All chain tests still cover the remaining
  invariants; no test asserts the *list* of chain members.
- Change `Subcommand(name="merge", mutating=True, ...)` to
  `mutating=False`. Tests check fields individually; only the
  `test_subcommands.py` assertions on the registry would catch this.
  (Worth checking — see Additional Patterns.)
- Add a new `_SPECIAL_HANDLERS` entry that runs before the lock is
  acquired.

**Recommendation.** Two layers:
1. Add a property test that *enumerates* every mutating subcommand and
   asserts its `lock_mode == "exclusive"` and its `invariant_chain`
   contains `gh.authenticated`. Coverage alone doesn't catch chain
   composition; an explicit test does.
2. When `gitbulk` is pushed to GitHub (per architecture.md §10, "No
   remote/CI history yet"), enable branch protection on `main`:
   require one review (the user themselves count, since this is
   single-author — but the dual-control gesture forces a UI step that
   a malicious shell can't fake), and require linear history.

---

## Additional Patterns Noted

Issues found but below the top-5 threshold; named but not elaborated.

- **`os.environ["XDG_CONFIG_HOME"] = parent` is mutated by
  `cli._apply_config_root`** (cli.py:215). Process-global env mutation
  persists into any subprocess; if `gh` reads `XDG_CONFIG_HOME` (it
  doesn't currently, but the var is conventional), an attacker-supplied
  `--config-root` would redirect the gh CLI's own config too. Low.
- **`lock_op | fcntl.LOCK_NB` retry in `_acquire`** (locks.py:108)
  reads holder metadata only when timing out, so a holder that wrote
  garbage metadata won't crash the wait; the JSON validation in
  `_read_holder_metadata` is sufficient. Fine.
- **`shutil.rmtree(candidate)` in `gc.prune_runs`** (gc.py:87) is safe
  against symlink-following because (a) candidate selection excludes
  top-level symlinks (gc.py:62) and (b) Python's shutil.rmtree since
  3.4 does not follow symlinks for directory entries by default. Fine.
- **`refresh_cache` returns the in-memory `CachedMembers`** but the
  caller in `report.py:242` discards it. Subsequent code re-reads the
  file. Not a bug, but a tiny TOCTOU window: the disk file could be
  swapped between save and re-read. Low.
- **`_pr_info_from_graphql_node` reads `node["repository"]
  ["nameWithOwner"]`** (gh.py:452) without validating shape. A
  malicious `gh` substitute can return a slug like `attacker/..` or
  one not in the requested set; `report.py` does
  `grouped.setdefault(pr.slug, []).append(pr)` (gh.py:439), which
  silently adds an entry for any slug, even one not in the passing
  set. In Phase 5 a mutating subcommand iterating `prs_by_repo` would
  act on slugs the user didn't ask for. Move the filter
  `if pr.slug not in slug_set: continue` into `my_open_prs`.
- **`mergeable_state` field shape**: `pr_info.KNOWN_MERGEABLE_STATES`
  is documented as "for validation" but no code actually validates
  incoming values against it. A `gh` substitute can return
  `mergeable_state="CLEAN"` for a PR that isn't, bypassing whatever
  Phase 5 merge invariant checks. Tie to F2.
- **`copilot-review-gate.yml`** uses env-pass-through correctly
  (workflow:27-30), avoiding the standard `${{ }}`-in-bash-script
  injection. Good. The `|| echo …` fallbacks (workflow:63, 77) are
  flagged in-file as TECH_DEBT and acknowledged.
- **No CI step verifies `uv.lock` integrity against pyproject.toml**.
  `uv sync --frozen` enforces the lock matches, but doesn't verify
  the lock was produced from the current pyproject. A malicious PR
  could land both files in lockstep. Lock-pin discipline is upstream
  uv's concern; flag for awareness only.
- **No `.gitleaks` / `truffleHog` / `git-secrets` pre-commit step**.
  The role prompt calls this out as a required check. For a personal
  repo about to publish to GitHub, configuring one of these (even
  just in a CI step) is cheap insurance. Low.
- **No LICENSE file** — flagged in README §License. Not security per
  se, but blocks any downstream redistribution and complicates
  liability framing.
- **`copilot-instructions.md` says workflows under `.github/workflows`
  belong to the repo**, but the only two checked-in workflows
  (`ci.yml`, `copilot-review-gate.yml`) use `actions/checkout@v6`,
  `actions/setup-python@v6`, `astral-sh/setup-uv@v7` — pinned to
  major versions, not commit SHAs. Per the role prompt's "supply
  chain integrity" section, immutable-SHA pinning is the standard.
  Acceptable trade-off for a personal repo (dependabot can keep
  majors current), but worth recording the deviation.

---

## Residual Unknowns

- **`this.i` may already contain rationales for some of the above
  trade-offs** (umask, gh-path resolution, slug regex looseness).
  Where it does, the right resolution is to lift those rationales into
  comments at the relevant call sites, not to change behavior. The
  review intentionally did not read `this.i` to preserve independence
  per the invoker's instruction.
- **Phase 5's `--apply` argparse wiring is not yet in the tree**; the
  finding that "no config or env path can set `--apply`" is true at
  Phase 2 (cli.py:178-183 doesn't declare it) but cannot be confirmed
  for Phase 5. Recommend adding a test now that asserts argparse's
  `--apply` for every mutating subcommand has `default=False` and is
  *only* settable from `argv`, not from a config-file mapping or env
  var. Lock that invariant before Phase 5 introduces it.
- **The mutating-subcommand lock and dry-run defaults**
  (subcommands.py:67-140) are present in the type, but no Phase 2 code
  actually consumes the `mutating` field yet — `_not_implemented`
  handles every mutating subcommand. The structural protection is
  *declared* but not *exercised*. Phase 5's first task should be a
  test that imports `KNOWN`, filters to mutating ones, and asserts
  each handler uses the exclusive lock and respects `--dry-run` by
  default.

---

## Decisions Needed

These are not findings — they are design calls the user should make
before Phase 5 begins, because they affect the security posture.

1. **Pin `gh` to an absolute path at process startup?** (F2.) The
   current bare-`gh` resolution is conventional and convenient, but
   it is the highest-impact compromise vector once mutation lands.
   Pick: pin via `shutil.which("gh")` once, or accept the risk and
   document it.
2. **Tighten the slug regex to forbid `..` and dotfile segments?**
   (F1.) The current liberal regex was probably chosen so that user
   org names with `.` or `-` continue to work; the tighter
   `[A-Za-z0-9._-]+` segments still permits both but forbids the
   traversal vectors. Pick: tighten now (cheap), or wait until Phase
   4 worktree code adds a `is_relative_to` check at the write site.
3. **Set `os.umask(0o077)`?** (F3.) No-downside on a single-user
   host. Hardens against the multi-user-host failure mode you may
   never encounter. Trivial to add now; awkward to add retroactively
   to existing files later.
4. **Hash-chain or signed audit log?** (Threat model scenario A.)
   Probably out of scope for a personal tool, but worth saying out
   loud: the runstate is uid-trustworthy, not crypto-trustworthy.
5. **Move `--refresh-org-members` inside the global lock?** (F4.)
   Marginal benefit in Phase 2; meaningful in Phase 5 when the
   refresh result steers close-stale / merge decisions.

---

## Closing Note

For a Phase-2 read-only personal CLI, gitbulk's security posture is
deliberate and largely sound. The author has clearly thought about
the trust boundaries (delegate to gh + ssh-agent, never touch working
trees, default mutating commands to dry-run, lock concurrent runs).
The two issues that warrant action **now**, while the surface is
small and the test suite is fast, are F1 (slug regex) and F2 (gh
path resolution). Both are one- to two-line changes with covering
unit tests; both close the most realistic Phase-5 attack paths.
F3 (umask) is free defense-in-depth. F4 and F5 are tightenings best
made before Phase 5 lands.
