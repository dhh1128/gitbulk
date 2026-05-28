"""Tests for config/policy.py (this.i node ck5pwr2n)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitbulk import paths
from gitbulk.config.policy import (
    Defaults,
    HumansConfig,
    Policy,
    RepoOverride,
    load_policy,
    policy_for,
)
from gitbulk.config.repos import ConfigError


def _write_policy(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "gitbulk.yaml"
    p.write_text(content)
    return p


# ─── Defaults: missing/empty file ──────────────────────────────────────────


def test_missing_file_returns_default_policy(tmp_path):
    policy = load_policy(tmp_path / "nonexistent.yaml")
    assert policy == Policy()
    assert policy.defaults.merge_policy == "strict"
    assert policy.defaults.merge_method == "rebase"
    assert policy.defaults.min_business_days == 3
    assert policy.defaults.unresolved_burden == "me"
    assert policy.defaults.bot_threads_block is True


def test_empty_file_returns_default_policy(tmp_path):
    path = _write_policy(tmp_path, "")
    assert load_policy(path) == Policy()


def test_default_path_uses_paths_module(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    # File does not exist there → defaults
    assert load_policy() == Policy()


def test_worktree_root_default_from_paths_module(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    policy = Policy()
    assert policy.worktree_root == paths.default_worktree_root()


# ─── Defaults: explicit values ─────────────────────────────────────────────


def test_full_defaults_load(tmp_path):
    content = """
defaults:
  merge_policy: ci-only
  min_business_days: 5
  unresolved_burden: either
  bot_threads_block: false
  stale_age_days: 90
  stale_cooloff_days: 14
  retain_runs: 7
  skip_checks: [foo, bar]
  extra_checks: [baz]
"""
    p = _write_policy(tmp_path, content)
    policy = load_policy(p)
    assert policy.defaults.merge_policy == "ci-only"
    assert policy.defaults.min_business_days == 5
    assert policy.defaults.unresolved_burden == "either"
    assert policy.defaults.bot_threads_block is False
    assert policy.defaults.stale_age_days == 90
    assert policy.defaults.stale_cooloff_days == 14
    assert policy.defaults.retain_runs == 7
    assert policy.defaults.skip_checks == ("foo", "bar")
    assert policy.defaults.extra_checks == ("baz",)


def test_retain_runs_default():
    from gitbulk.config.policy import Defaults
    assert Defaults().retain_runs == 30


# ─── merge_method ─────────────────────────────────────────────────────────


def test_merge_method_default_is_rebase():
    """Per this.i node gji4dyze, default merge method is `rebase` —
    preserves per-commit history (unlike squash) while keeping a
    linear main branch (unlike merge commits). Flipped from `merge`
    during real-use onboarding 2026-05-28."""
    from gitbulk.config.policy import Defaults
    assert Defaults().merge_method == "rebase"


def test_merge_method_explicit_in_defaults(tmp_path):
    content = "defaults:\n  merge_method: squash\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.defaults.merge_method == "squash"


def test_merge_method_rebase_in_defaults(tmp_path):
    content = "defaults:\n  merge_method: rebase\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.defaults.merge_method == "rebase"


def test_merge_method_rejects_invalid_value(tmp_path):
    content = "defaults:\n  merge_method: ff-only\n"
    with pytest.raises(ConfigError, match="merge_method"):
        load_policy(_write_policy(tmp_path, content))


def test_merge_method_per_repo_override(tmp_path):
    content = """
defaults:
  merge_method: merge
repos:
  owner/legacy:
    merge_method: squash
  owner/normal:
    merge_policy: ci-only
"""
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.defaults.merge_method == "merge"
    assert policy_for(policy, "owner/legacy").merge_method == "squash"
    # No merge_method override → effective is default
    assert policy_for(policy, "owner/normal").merge_method == "merge"
    # Repo not in config at all → effective is default
    assert policy_for(policy, "owner/other").merge_method == "merge"


# ─── stale_policy ─────────────────────────────────────────────────────────


def test_stale_policy_default_is_warn_and_close():
    from gitbulk.config.policy import Defaults
    assert Defaults().stale_policy == "warn-and-close"


def test_stale_policy_explicit_warn_only(tmp_path):
    content = "defaults:\n  stale_policy: warn-only\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.defaults.stale_policy == "warn-only"


def test_stale_policy_explicit_never(tmp_path):
    content = "defaults:\n  stale_policy: never\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.defaults.stale_policy == "never"


def test_stale_policy_rejects_invalid_value(tmp_path):
    content = "defaults:\n  stale_policy: aggressive\n"
    with pytest.raises(ConfigError, match="stale_policy"):
        load_policy(_write_policy(tmp_path, content))


def test_stale_policy_per_repo_override(tmp_path):
    content = """
defaults:
  stale_policy: warn-and-close
repos:
  owner/archive:
    stale_policy: never
  owner/sandbox:
    stale_policy: warn-only
"""
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.defaults.stale_policy == "warn-and-close"
    assert policy_for(policy, "owner/archive").stale_policy == "never"
    assert policy_for(policy, "owner/sandbox").stale_policy == "warn-only"
    assert policy_for(policy, "owner/other").stale_policy == "warn-and-close"


def test_stale_policy_per_repo_override_rejects_invalid(tmp_path):
    content = """
repos:
  owner/repo:
    stale_policy: ruthless
"""
    with pytest.raises(ConfigError, match="stale_policy"):
        load_policy(_write_policy(tmp_path, content))


def test_merge_method_per_repo_override_rejects_invalid(tmp_path):
    content = """
repos:
  owner/repo:
    merge_method: bogus
"""
    with pytest.raises(ConfigError, match="merge_method"):
        load_policy(_write_policy(tmp_path, content))


def test_retain_runs_rejects_zero(tmp_path):
    content = "defaults:\n  retain_runs: 0\n"
    with pytest.raises(ConfigError, match="must be >= 1"):
        load_policy(_write_policy(tmp_path, content))


def test_retain_runs_per_repo_override(tmp_path):
    content = """
defaults:
  retain_runs: 30
repos:
  owner/repo:
    retain_runs: 7
"""
    policy = load_policy(_write_policy(tmp_path, content))
    effective = policy_for(policy, "owner/repo")
    assert effective.retain_runs == 7


def test_humans_section(tmp_path):
    content = """
humans:
  org: provenant-dev
  cache_ttl_hours: 48
  exceptions: [some-bot-that-looks-human]
  always_human: [external-collab]
"""
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.humans == HumansConfig(
        org="provenant-dev",
        cache_ttl_hours=48,
        exceptions=("some-bot-that-looks-human",),
        always_human=("external-collab",),
    )


def test_humans_org_null_allowed(tmp_path):
    content = "humans:\n  org: null\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.humans.org is None


def test_humans_without_org_still_parses(tmp_path):
    # Exercise the branch where humans is present but `org` key is absent.
    content = "humans:\n  cache_ttl_hours: 12\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.humans.org is None
    assert policy.humans.cache_ttl_hours == 12


def test_bots_list(tmp_path):
    content = "bots:\n  - dependabot[bot]\n  - renovate[bot]\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.bots == ("dependabot[bot]", "renovate[bot]")


def test_worktree_root_expanduser(tmp_path):
    content = "worktree_root: ~/custom-worktrees\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.worktree_root == Path.home() / "custom-worktrees"


def test_notifications_key_is_ignored(tmp_path):
    # Currently forward-compat: present in YAML but contents don't matter.
    content = "notifications:\n  ntfy: example\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy == Policy()


# ─── Per-repo overrides ────────────────────────────────────────────────────


def test_repo_override_loaded(tmp_path):
    content = """
defaults:
  merge_policy: strict
  skip_checks: [global_skip]
repos:
  owner/repo:
    merge_policy: ci-only
    min_business_days: 1
    skip_checks: [repo_skip]
"""
    policy = load_policy(_write_policy(tmp_path, content))
    override = policy.repos["owner/repo"]
    assert override.merge_policy == "ci-only"
    assert override.min_business_days == 1
    assert override.skip_checks == ("repo_skip",)


def test_repo_override_all_scalar_fields(tmp_path):
    """Exercise every scalar override branch in _parse_repo_override."""
    content = """
defaults:
  merge_policy: strict
  min_business_days: 3
  unresolved_burden: me
  bot_threads_block: true
  stale_age_days: 60
  stale_cooloff_days: 7
repos:
  owner/repo:
    merge_policy: ci-only
    min_business_days: 1
    unresolved_burden: other
    bot_threads_block: false
    stale_age_days: 30
    stale_cooloff_days: 3
    extra_checks: [extra_repo]
"""
    policy = load_policy(_write_policy(tmp_path, content))
    override = policy.repos["owner/repo"]
    assert override.merge_policy == "ci-only"
    assert override.min_business_days == 1
    assert override.unresolved_burden == "other"
    assert override.bot_threads_block is False
    assert override.stale_age_days == 30
    assert override.stale_cooloff_days == 3
    assert override.extra_checks == ("extra_repo",)

    effective = policy_for(policy, "owner/repo")
    assert effective.merge_policy == "ci-only"
    assert effective.min_business_days == 1
    assert effective.unresolved_burden == "other"
    assert effective.bot_threads_block is False
    assert effective.stale_age_days == 30
    assert effective.stale_cooloff_days == 3
    assert effective.extra_checks == ("extra_repo",)


# ─── policy_for(): effective defaults computation ──────────────────────────


def test_policy_for_no_override_returns_defaults(tmp_path):
    policy = Policy()
    assert policy_for(policy, "owner/repo") == policy.defaults


def test_policy_for_scalar_override(tmp_path):
    content = """
defaults:
  merge_policy: strict
  min_business_days: 3
repos:
  owner/repo:
    merge_policy: never
"""
    policy = load_policy(_write_policy(tmp_path, content))
    effective = policy_for(policy, "owner/repo")
    assert effective.merge_policy == "never"
    assert effective.min_business_days == 3  # inherited


def test_policy_for_list_fields_append(tmp_path):
    content = """
defaults:
  skip_checks: [global_a, global_b]
  extra_checks: [extra_global]
repos:
  owner/repo:
    skip_checks: [repo_a]
    extra_checks: [extra_repo]
"""
    policy = load_policy(_write_policy(tmp_path, content))
    effective = policy_for(policy, "owner/repo")
    assert effective.skip_checks == ("global_a", "global_b", "repo_a")
    assert effective.extra_checks == ("extra_global", "extra_repo")


def test_policy_for_override_with_empty_lists(tmp_path):
    # If repo override doesn't specify skip_checks, defaults apply.
    content = """
defaults:
  skip_checks: [global_only]
repos:
  owner/repo:
    merge_policy: ci-only
"""
    policy = load_policy(_write_policy(tmp_path, content))
    effective = policy_for(policy, "owner/repo")
    assert effective.skip_checks == ("global_only",)


# ─── Validation: unknown keys raise ConfigError ────────────────────────────


def test_unknown_top_level_key_raises(tmp_path):
    with pytest.raises(ConfigError, match="unknown key"):
        load_policy(_write_policy(tmp_path, "mystery_section: {}\n"))


def test_unknown_defaults_key_raises(tmp_path):
    content = "defaults:\n  min_buisness_days: 3\n"  # typo
    with pytest.raises(ConfigError, match="unknown key"):
        load_policy(_write_policy(tmp_path, content))


def test_unknown_humans_key_raises(tmp_path):
    content = "humans:\n  organization: foo\n"  # should be 'org'
    with pytest.raises(ConfigError, match="unknown key"):
        load_policy(_write_policy(tmp_path, content))


def test_unknown_repo_override_key_raises(tmp_path):
    content = "repos:\n  owner/repo:\n    merge_polciy: strict\n"  # typo
    with pytest.raises(ConfigError, match="unknown key"):
        load_policy(_write_policy(tmp_path, content))


# ─── Validation: bad values raise ConfigError ──────────────────────────────


def test_invalid_merge_policy_raises(tmp_path):
    content = "defaults:\n  merge_policy: aggressive\n"
    with pytest.raises(ConfigError, match="not in allowed values"):
        load_policy(_write_policy(tmp_path, content))


def test_invalid_unresolved_burden_raises(tmp_path):
    content = "defaults:\n  unresolved_burden: nobody\n"
    with pytest.raises(ConfigError, match="not in allowed values"):
        load_policy(_write_policy(tmp_path, content))


def test_negative_int_raises(tmp_path):
    content = "defaults:\n  min_business_days: -1\n"
    with pytest.raises(ConfigError, match="must be >= 0"):
        load_policy(_write_policy(tmp_path, content))


def test_string_for_int_raises(tmp_path):
    content = "defaults:\n  min_business_days: 'three'\n"
    with pytest.raises(ConfigError, match="expected int"):
        load_policy(_write_policy(tmp_path, content))


def test_bool_for_int_raises(tmp_path):
    # bool is technically subclass of int in Python; we reject explicitly
    content = "defaults:\n  min_business_days: true\n"
    with pytest.raises(ConfigError, match="expected int"):
        load_policy(_write_policy(tmp_path, content))


def test_int_for_bool_raises(tmp_path):
    content = "defaults:\n  bot_threads_block: 1\n"
    with pytest.raises(ConfigError, match="expected bool"):
        load_policy(_write_policy(tmp_path, content))


def test_int_for_str_raises(tmp_path):
    content = "defaults:\n  merge_policy: 42\n"
    with pytest.raises(ConfigError, match="expected str"):
        load_policy(_write_policy(tmp_path, content))


def test_non_list_for_list_field_raises(tmp_path):
    content = "defaults:\n  skip_checks: 'just-one'\n"
    with pytest.raises(ConfigError, match="expected list"):
        load_policy(_write_policy(tmp_path, content))


def test_non_string_in_list_raises(tmp_path):
    content = "defaults:\n  skip_checks: [valid, 42]\n"
    with pytest.raises(ConfigError, match="expected str"):
        load_policy(_write_policy(tmp_path, content))


def test_non_string_org_raises(tmp_path):
    content = "humans:\n  org: 42\n"
    with pytest.raises(ConfigError, match="expected str or null"):
        load_policy(_write_policy(tmp_path, content))


def test_non_string_worktree_root_raises(tmp_path):
    content = "worktree_root: 42\n"
    with pytest.raises(ConfigError, match="expected str"):
        load_policy(_write_policy(tmp_path, content))


def test_top_level_not_mapping_raises(tmp_path):
    content = "- this is a list, not a mapping\n"
    with pytest.raises(ConfigError, match="top-level must be a YAML mapping"):
        load_policy(_write_policy(tmp_path, content))


def test_repos_not_mapping_raises(tmp_path):
    content = "repos: [list, instead, of, map]\n"
    with pytest.raises(ConfigError, match="expected mapping"):
        load_policy(_write_policy(tmp_path, content))


def test_per_repo_override_not_mapping_raises(tmp_path):
    content = "repos:\n  owner/repo: 'just a string'\n"
    with pytest.raises(ConfigError, match="expected mapping"):
        load_policy(_write_policy(tmp_path, content))


def test_null_list_treated_as_empty(tmp_path):
    content = "defaults:\n  skip_checks: null\n"
    policy = load_policy(_write_policy(tmp_path, content))
    assert policy.defaults.skip_checks == ()


# ─── Example file parses cleanly ────────────────────────────────────────────


def test_example_file_loads_cleanly():
    """The shipped config/gitbulk.yaml.example must always be valid."""
    example_path = Path(__file__).resolve().parent.parent / "config" / "gitbulk.yaml.example"
    # Should not raise.
    policy = load_policy(example_path)
    # Spot-check that values from the example match expectations
    assert policy.defaults.merge_policy == "strict"
    assert policy.defaults.min_business_days == 3
    assert policy.humans.org == "provenant-dev"
    assert "dependabot[bot]" in policy.bots
