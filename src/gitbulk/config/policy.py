"""Loader for ~/.config/gitbulk/gitbulk.yaml.

See this.i node ``ck5pwr2n`` for the schema and validation contract.
Defaults are pinned to other this.i nodes (``bg4pqn7m`` for
``min_business_days``, ``hj3nq5kp`` for ``unresolved_burden``,
``zk3r4nqp`` for ``bot_threads_block``, ``mw6kp2nq`` for
``worktree_root``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from gitbulk import paths
from gitbulk.config.repos import ConfigError
from gitbulk.filters import FilterSpec

if TYPE_CHECKING:
    from gitbulk.agent import AgentProfile

_VALID_MERGE_POLICIES = {"strict", "ci-only", "never"}
_VALID_UNRESOLVED_BURDENS = {"me", "other", "either"}
_VALID_MERGE_METHODS = {"merge", "squash", "rebase"}
_VALID_STALE_POLICIES = {"warn-and-close", "warn-only", "never"}

_TOP_LEVEL_KEYS = {
    "defaults",
    "humans",
    "bots",
    "repos",
    "filters",
    "worktree_root",
    "agents",  # pluggable coding-agent profiles (this.i agprof4k)
    "default_agent",  # run-level default agent name
    "sandbox_fallback",  # behavior when a requested sandbox is unavailable
    "notifications",  # forward-compat placeholder; contents ignored
}

#: Accepted values for the top-level ``sandbox_fallback`` (this.i agsbx3k).
#: Mirrors gitbulk.agent.VALID_SANDBOX_FALLBACKS; duplicated to keep config
#: parsing free of an import on the agent/subprocess machinery.
_VALID_SANDBOX_FALLBACKS = {"refuse", "warn-run"}

#: Keys allowed inside one ``filters.<name>`` block. Mirror the v1
#: filter dimensions (this.i node ``flt7arg2``); singular forms because
#: a YAML scalar or list is accepted for each.
_FILTER_KEYS = {"org", "repo", "base", "mergeable_state", "author"}

_DEFAULTS_KEYS = {
    "merge_policy",
    "merge_method",
    "min_business_days",
    "unresolved_burden",
    "bot_threads_block",
    "stale_age_days",
    "stale_cooloff_days",
    "stale_policy",
    "retain_runs",
    "prune_min_age_days",
    "prune_plan_max_age_minutes",
    "skip_checks",
    "extra_checks",
    "sacred_branches",
}

#: Defaults-only keys: valid under ``defaults:`` but NOT as a per-repo
#: override, because they configure a process-global resource (e.g. the
#: prune scan thread pool, node prnpf8nq). Kept out of _DEFAULTS_KEYS so a
#: per-repo use is a loud ConfigError rather than a silent no-op.
_DEFAULTS_ONLY_KEYS = {"prune_scan_concurrency"}

_HUMANS_KEYS = {"org", "cache_ttl_hours", "exceptions", "always_human"}

# per-repo can override any defaults key, plus pick a per-repo agent
_REPO_OVERRIDE_KEYS = _DEFAULTS_KEYS | {"agent"}


@dataclass(frozen=True)
class Defaults:
    merge_policy: str = "strict"
    #: Merge method gitbulk passes to ``gh pr merge``. Default ``rebase``:
    #: keeps per-commit history (unlike squash) AND a linear main branch
    #: (unlike merge commits). Choice flipped from ``merge`` during real-
    #: use onboarding 2026-05-28. See this.i node ``gji4dyze``.
    merge_method: str = "rebase"
    min_business_days: int = 3
    unresolved_burden: str = "me"
    bot_threads_block: bool = True
    stale_age_days: int = 90
    stale_cooloff_days: int = 7
    #: How close-stale handles inactive PRs. ``warn-and-close`` (default)
    #: posts a heads-up comment, waits stale_cooloff_days, then closes.
    #: ``warn-only`` only ever posts the comment (useful while tuning
    #: thresholds). ``never`` disables close-stale for this repo.
    stale_policy: str = "warn-and-close"
    retain_runs: int = 30
    #: Grace period for the prune subcommands (node prgrc3kp). A branch or
    #: worktree whose PR was merged/closed fewer than this many days ago is
    #: left alone, so just-merged work (hotfixes, deploy refs) is not swept.
    prune_min_age_days: int = 7
    #: Worker count for the prune-branches parallel scan (node prnpf8nq).
    #: Defaults-only (the pool is global, so a per-repo value is meaningless);
    #: 12 stays comfortably under GitHub's REST secondary-rate limits while
    #: turning a ~25-min sequential fleet scan into ~2-3 min.
    prune_scan_concurrency: int = 12
    #: How long a prune-branches plan entry stays reusable before a re-run
    #: re-scans the repo (node prnsh5kp). 720 min = 12 h balances cache savings
    #: against missing a branch that became deletable; per-repo overridable
    #: (a volatile repo can carry a shorter window). ``--max-age``/``--force-scan``
    #: override at the CLI.
    prune_plan_max_age_minutes: int = 720
    skip_checks: tuple[str, ...] = ()
    extra_checks: tuple[str, ...] = ()
    #: Extra branch names prune-worktrees treats as SACRED — never auto-pruned,
    #: exactly like the always-sacred ``main``/``master`` and the repo's GitHub
    #: default branch. Match is exact and case-sensitive (git branch names are).
    #: For org conventions like ``develop``/``trunk``/``release`` that the
    #: local-branch sweep must never touch. Appended to (not replacing) the
    #: built-in sacred set; a per-repo override appends further (node prnwlb7q).
    sacred_branches: tuple[str, ...] = ()


@dataclass(frozen=True)
class HumansConfig:
    org: str | None = None
    # 7 days. Org membership changes closer to never than to daily, and
    # since every subcommand now auto-refreshes a stale cache before its
    # preflight (ormrf7kq), a longer TTL just means fewer needless
    # refetches; staleness only degrades to the conservative BOT default
    # (never destructive), mirroring the default-branch cache (rj7p4kqn).
    cache_ttl_hours: int = 168
    exceptions: tuple[str, ...] = ()
    always_human: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepoOverride:
    merge_policy: str | None = None
    merge_method: str | None = None
    min_business_days: int | None = None
    unresolved_burden: str | None = None
    bot_threads_block: bool | None = None
    stale_age_days: int | None = None
    stale_cooloff_days: int | None = None
    stale_policy: str | None = None
    retain_runs: int | None = None
    prune_min_age_days: int | None = None
    prune_plan_max_age_minutes: int | None = None
    skip_checks: tuple[str, ...] = ()  # appended to defaults
    extra_checks: tuple[str, ...] = ()  # appended to defaults
    sacred_branches: tuple[str, ...] = ()  # appended to defaults
    #: Per-repo coding-agent selection (this.i agprof4k); names a profile
    #: under ``agents:`` or a built-in preset. ``None`` ⇒ use ``default_agent``.
    agent: str | None = None


@dataclass(frozen=True)
class Policy:
    defaults: Defaults = field(default_factory=Defaults)
    humans: HumansConfig = field(default_factory=HumansConfig)
    bots: tuple[str, ...] = ()
    repos: dict[str, RepoOverride] = field(default_factory=dict)
    #: Named fleet-subset filter sets, referenced by ``--filter NAME``.
    #: Maps name → FilterSpec (see gitbulk.filters / node flt7arg2).
    filters: dict[str, "FilterSpec"] = field(default_factory=dict)
    worktree_root: Path = field(default_factory=lambda: paths.default_worktree_root())
    #: Pluggable coding-agent profiles, name → AgentProfile (this.i agprof4k).
    #: Empty ⇒ only the built-in presets are available.
    agents: dict[str, "AgentProfile"] = field(default_factory=dict)
    #: Run-level default agent name; ``None`` ⇒ the ``claude`` preset.
    default_agent: str | None = None
    #: What to do when a profile requests a sandbox the host can't provide
    #: (this.i agsbx3k): ``None``/``"refuse"`` ⇒ refuse to run; ``"warn-run"``
    #: ⇒ run unsandboxed with a warning.
    sandbox_fallback: str | None = None


# ─── Validation helpers ────────────────────────────────────────────────────


def _validate_keys(actual: set[str], allowed: set[str], where: str) -> None:
    extra = actual - allowed
    if extra:
        raise ConfigError(
            f"{where}: unknown key(s) {sorted(extra)!r}; "
            f"allowed: {sorted(allowed)!r}"
        )


def _ensure_int(value: Any, where: str, *, minimum: int | None = None) -> int:
    # bool is a subclass of int; explicitly reject it.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected int, got {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {value}")
    return value


def _ensure_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected bool, got {type(value).__name__}")
    return value


def _ensure_str(value: Any, where: str, *, allowed: set[str] | None = None) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected str, got {type(value).__name__}")
    if allowed is not None and value not in allowed:
        raise ConfigError(
            f"{where}: {value!r} not in allowed values {sorted(allowed)!r}"
        )
    return value


def _ensure_str_list(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{where}: expected list, got {type(value).__name__}")
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(
                f"{where}[{i}]: expected str, got {type(item).__name__}"
            )
    return tuple(value)


# ─── Section parsers ───────────────────────────────────────────────────────


def _parse_defaults(raw: dict[str, Any], where: str) -> Defaults:
    _validate_keys(set(raw.keys()), _DEFAULTS_KEYS | _DEFAULTS_ONLY_KEYS, where)
    kwargs: dict[str, Any] = {}
    if "merge_policy" in raw:
        kwargs["merge_policy"] = _ensure_str(
            raw["merge_policy"], f"{where}.merge_policy", allowed=_VALID_MERGE_POLICIES
        )
    if "merge_method" in raw:
        kwargs["merge_method"] = _ensure_str(
            raw["merge_method"], f"{where}.merge_method", allowed=_VALID_MERGE_METHODS
        )
    if "min_business_days" in raw:
        kwargs["min_business_days"] = _ensure_int(
            raw["min_business_days"], f"{where}.min_business_days", minimum=0
        )
    if "unresolved_burden" in raw:
        kwargs["unresolved_burden"] = _ensure_str(
            raw["unresolved_burden"],
            f"{where}.unresolved_burden",
            allowed=_VALID_UNRESOLVED_BURDENS,
        )
    if "bot_threads_block" in raw:
        kwargs["bot_threads_block"] = _ensure_bool(
            raw["bot_threads_block"], f"{where}.bot_threads_block"
        )
    if "stale_age_days" in raw:
        kwargs["stale_age_days"] = _ensure_int(
            raw["stale_age_days"], f"{where}.stale_age_days", minimum=0
        )
    if "stale_cooloff_days" in raw:
        kwargs["stale_cooloff_days"] = _ensure_int(
            raw["stale_cooloff_days"], f"{where}.stale_cooloff_days", minimum=0
        )
    if "stale_policy" in raw:
        kwargs["stale_policy"] = _ensure_str(
            raw["stale_policy"], f"{where}.stale_policy", allowed=_VALID_STALE_POLICIES
        )
    if "retain_runs" in raw:
        kwargs["retain_runs"] = _ensure_int(
            raw["retain_runs"], f"{where}.retain_runs", minimum=1
        )
    if "prune_min_age_days" in raw:
        kwargs["prune_min_age_days"] = _ensure_int(
            raw["prune_min_age_days"], f"{where}.prune_min_age_days", minimum=0
        )
    if "prune_scan_concurrency" in raw:
        kwargs["prune_scan_concurrency"] = _ensure_int(
            raw["prune_scan_concurrency"],
            f"{where}.prune_scan_concurrency",
            minimum=1,
        )
    if "prune_plan_max_age_minutes" in raw:
        kwargs["prune_plan_max_age_minutes"] = _ensure_int(
            raw["prune_plan_max_age_minutes"],
            f"{where}.prune_plan_max_age_minutes",
            minimum=0,
        )
    if "skip_checks" in raw:
        kwargs["skip_checks"] = _ensure_str_list(
            raw["skip_checks"], f"{where}.skip_checks"
        )
    if "extra_checks" in raw:
        kwargs["extra_checks"] = _ensure_str_list(
            raw["extra_checks"], f"{where}.extra_checks"
        )
    if "sacred_branches" in raw:
        kwargs["sacred_branches"] = _ensure_str_list(
            raw["sacred_branches"], f"{where}.sacred_branches"
        )
    return Defaults(**kwargs)


def _parse_humans(raw: dict[str, Any], where: str) -> HumansConfig:
    _validate_keys(set(raw.keys()), _HUMANS_KEYS, where)
    kwargs: dict[str, Any] = {}
    if "org" in raw:
        org = raw["org"]
        if org is not None and not isinstance(org, str):
            raise ConfigError(
                f"{where}.org: expected str or null, got {type(org).__name__}"
            )
        kwargs["org"] = org
    if "cache_ttl_hours" in raw:
        kwargs["cache_ttl_hours"] = _ensure_int(
            raw["cache_ttl_hours"], f"{where}.cache_ttl_hours", minimum=0
        )
    if "exceptions" in raw:
        kwargs["exceptions"] = _ensure_str_list(
            raw["exceptions"], f"{where}.exceptions"
        )
    if "always_human" in raw:
        kwargs["always_human"] = _ensure_str_list(
            raw["always_human"], f"{where}.always_human"
        )
    return HumansConfig(**kwargs)


def _parse_repo_override(raw: dict[str, Any], where: str) -> RepoOverride:
    _validate_keys(set(raw.keys()), _REPO_OVERRIDE_KEYS, where)
    kwargs: dict[str, Any] = {}
    if "merge_policy" in raw:
        kwargs["merge_policy"] = _ensure_str(
            raw["merge_policy"], f"{where}.merge_policy", allowed=_VALID_MERGE_POLICIES
        )
    if "merge_method" in raw:
        kwargs["merge_method"] = _ensure_str(
            raw["merge_method"], f"{where}.merge_method", allowed=_VALID_MERGE_METHODS
        )
    if "min_business_days" in raw:
        kwargs["min_business_days"] = _ensure_int(
            raw["min_business_days"], f"{where}.min_business_days", minimum=0
        )
    if "unresolved_burden" in raw:
        kwargs["unresolved_burden"] = _ensure_str(
            raw["unresolved_burden"],
            f"{where}.unresolved_burden",
            allowed=_VALID_UNRESOLVED_BURDENS,
        )
    if "bot_threads_block" in raw:
        kwargs["bot_threads_block"] = _ensure_bool(
            raw["bot_threads_block"], f"{where}.bot_threads_block"
        )
    if "stale_age_days" in raw:
        kwargs["stale_age_days"] = _ensure_int(
            raw["stale_age_days"], f"{where}.stale_age_days", minimum=0
        )
    if "stale_cooloff_days" in raw:
        kwargs["stale_cooloff_days"] = _ensure_int(
            raw["stale_cooloff_days"], f"{where}.stale_cooloff_days", minimum=0
        )
    if "stale_policy" in raw:
        kwargs["stale_policy"] = _ensure_str(
            raw["stale_policy"], f"{where}.stale_policy", allowed=_VALID_STALE_POLICIES
        )
    if "retain_runs" in raw:
        kwargs["retain_runs"] = _ensure_int(
            raw["retain_runs"], f"{where}.retain_runs", minimum=1
        )
    if "prune_min_age_days" in raw:
        kwargs["prune_min_age_days"] = _ensure_int(
            raw["prune_min_age_days"], f"{where}.prune_min_age_days", minimum=0
        )
    if "prune_plan_max_age_minutes" in raw:
        kwargs["prune_plan_max_age_minutes"] = _ensure_int(
            raw["prune_plan_max_age_minutes"],
            f"{where}.prune_plan_max_age_minutes",
            minimum=0,
        )
    if "skip_checks" in raw:
        kwargs["skip_checks"] = _ensure_str_list(
            raw["skip_checks"], f"{where}.skip_checks"
        )
    if "extra_checks" in raw:
        kwargs["extra_checks"] = _ensure_str_list(
            raw["extra_checks"], f"{where}.extra_checks"
        )
    if "sacred_branches" in raw:
        kwargs["sacred_branches"] = _ensure_str_list(
            raw["sacred_branches"], f"{where}.sacred_branches"
        )
    if "agent" in raw:
        kwargs["agent"] = _ensure_str(raw["agent"], f"{where}.agent")
    return RepoOverride(**kwargs)


# ─── Top-level loader and effective-policy helper ─────────────────────────


def load_policy(path: Path | None = None) -> Policy:
    """Parse gitbulk.yaml. Missing or empty file returns Policy() defaults."""
    if path is None:
        path = paths.policy_file()
    if not path.exists():
        return Policy()
    with path.open() as f:
        raw = yaml.safe_load(f)
    if raw is None:
        return Policy()
    if not isinstance(raw, dict):
        raise ConfigError(
            f"{path}: top-level must be a YAML mapping, got {type(raw).__name__}"
        )
    _validate_keys(set(raw.keys()), _TOP_LEVEL_KEYS, str(path))

    kwargs: dict[str, Any] = {}
    if raw.get("defaults") is not None:
        kwargs["defaults"] = _parse_defaults(raw["defaults"], f"{path}.defaults")
    if raw.get("humans") is not None:
        kwargs["humans"] = _parse_humans(raw["humans"], f"{path}.humans")
    if raw.get("bots") is not None:
        kwargs["bots"] = _ensure_str_list(raw["bots"], f"{path}.bots")
    if raw.get("repos") is not None:
        if not isinstance(raw["repos"], dict):
            raise ConfigError(
                f"{path}.repos: expected mapping, got {type(raw['repos']).__name__}"
            )
        repo_dict: dict[str, RepoOverride] = {}
        for slug, override_raw in raw["repos"].items():
            if not isinstance(override_raw, dict):
                raise ConfigError(
                    f"{path}.repos.{slug}: expected mapping, "
                    f"got {type(override_raw).__name__}"
                )
            repo_dict[slug] = _parse_repo_override(
                override_raw, f"{path}.repos.{slug}"
            )
        kwargs["repos"] = repo_dict
    if raw.get("filters") is not None:
        if not isinstance(raw["filters"], dict):
            raise ConfigError(
                f"{path}.filters: expected mapping, "
                f"got {type(raw['filters']).__name__}"
            )
        filters_dict: dict[str, FilterSpec] = {}
        for name, spec_raw in raw["filters"].items():
            if not isinstance(spec_raw, dict):
                raise ConfigError(
                    f"{path}.filters.{name}: expected mapping, "
                    f"got {type(spec_raw).__name__}"
                )
            filters_dict[name] = _parse_filter(spec_raw, f"{path}.filters.{name}")
        kwargs["filters"] = filters_dict
    if raw.get("worktree_root") is not None:
        wt = raw["worktree_root"]
        if not isinstance(wt, str):
            raise ConfigError(
                f"{path}.worktree_root: expected str, got {type(wt).__name__}"
            )
        kwargs["worktree_root"] = Path(wt).expanduser()
    if raw.get("agents") is not None:
        # Imported here (not at module top) to keep config.policy importable
        # without pulling in the agent/subprocess machinery on every load.
        from gitbulk.agent import parse_agents_config

        kwargs["agents"] = parse_agents_config(raw["agents"], f"{path}.agents")
    if raw.get("default_agent") is not None:
        kwargs["default_agent"] = _ensure_str(
            raw["default_agent"], f"{path}.default_agent"
        )
    if raw.get("sandbox_fallback") is not None:
        kwargs["sandbox_fallback"] = _ensure_str(
            raw["sandbox_fallback"],
            f"{path}.sandbox_fallback",
            allowed=_VALID_SANDBOX_FALLBACKS,
        )
    # "notifications" key, if present, is intentionally ignored
    return Policy(**kwargs)


def _scalar_or_list(value: Any, where: str) -> tuple[str, ...]:
    """Accept a single string OR a list of strings → tuple of strings.

    Filter blocks read naturally with a scalar (``org: provenant-dev``)
    or a list (``repo: ['origin-*', 'vvp-*']``); both normalize here.
    """
    if isinstance(value, str):
        return (value,)
    return _ensure_str_list(value, where)


def _parse_filter(raw: dict[str, Any], where: str) -> FilterSpec:
    _validate_keys(set(raw.keys()), _FILTER_KEYS, where)
    return FilterSpec(
        orgs=_scalar_or_list(raw["org"], f"{where}.org") if "org" in raw else (),
        repo_globs=_scalar_or_list(raw["repo"], f"{where}.repo") if "repo" in raw else (),
        bases=_scalar_or_list(raw["base"], f"{where}.base") if "base" in raw else (),
        mergeable_states=(
            _scalar_or_list(raw["mergeable_state"], f"{where}.mergeable_state")
            if "mergeable_state" in raw
            else ()
        ),
        authors=_scalar_or_list(raw["author"], f"{where}.author") if "author" in raw else (),
    )


def policy_for(policy: Policy, slug: str) -> Defaults:
    """Return the effective Defaults for ``slug`` after applying any per-repo override."""
    base = policy.defaults
    override = policy.repos.get(slug)
    if override is None:
        return base
    updates: dict[str, Any] = {}
    for key in (
        "merge_policy",
        "merge_method",
        "min_business_days",
        "unresolved_burden",
        "bot_threads_block",
        "stale_age_days",
        "stale_cooloff_days",
        "stale_policy",
        "retain_runs",
        "prune_min_age_days",
        "prune_plan_max_age_minutes",
    ):
        v = getattr(override, key)
        if v is not None:
            updates[key] = v
    updates["skip_checks"] = base.skip_checks + override.skip_checks
    updates["extra_checks"] = base.extra_checks + override.extra_checks
    updates["sacred_branches"] = base.sacred_branches + override.sacred_branches
    return replace(base, **updates)
