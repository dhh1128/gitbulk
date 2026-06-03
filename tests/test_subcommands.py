"""Tests for the subcommands module (this.i node smodlpr3)."""

from __future__ import annotations

import pytest

from gitbulk.subcommands import KNOWN, NAMES, Subcommand, by_name


def test_known_contains_expected_subcommands():
    expected = {
        "report",
        "summarize",
        "dispatch",
        "merge",
        "rebase-pr",
        "close-stale",
        "prune-branches",
        "prune-worktrees",
        "show",
        "ack",
        "invariants",
    }
    assert {s.name for s in KNOWN} == expected


def test_names_tuple_matches_known():
    assert NAMES == tuple(s.name for s in KNOWN)


def test_subcommand_dataclass_is_frozen():
    sc = by_name("report")
    with pytest.raises((AttributeError, Exception)):
        sc.name = "renamed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "name,mutating,lock_mode,needs_clone",
    [
        ("report", False, "shared", False),
        ("summarize", False, "shared", False),
        ("dispatch", True, "exclusive", True),
        ("merge", True, "exclusive", False),
        ("rebase-pr", True, "exclusive", True),
        ("close-stale", True, "exclusive", False),
        ("prune-branches", True, "exclusive", False),
        ("prune-worktrees", True, "exclusive", True),
        ("show", False, "shared", False),
        ("ack", False, "shared", False),
        ("invariants", False, "shared", False),
    ],
)
def test_subcommand_metadata(name, mutating, lock_mode, needs_clone):
    sc = by_name(name)
    assert sc.mutating == mutating
    assert sc.lock_mode == lock_mode
    assert sc.needs_clone == needs_clone


def test_by_name_unknown_raises_keyerror():
    with pytest.raises(KeyError, match="unknown subcommand"):
        by_name("definitely-not-a-subcommand")


def test_mutating_subcommands_take_exclusive_lock():
    """Invariant from node lj5pqn4kr: every mutating subcommand uses exclusive."""
    for sc in KNOWN:
        if sc.mutating:
            assert sc.lock_mode == "exclusive", (
                f"{sc.name} is mutating but lock_mode={sc.lock_mode!r}"
            )


def test_read_only_subcommands_take_shared_lock():
    """Symmetric: every read-only subcommand uses shared lock."""
    for sc in KNOWN:
        if not sc.mutating:
            assert sc.lock_mode == "shared", (
                f"{sc.name} is read-only but lock_mode={sc.lock_mode!r}"
            )


def test_help_strings_are_non_empty():
    for sc in KNOWN:
        assert sc.help.strip(), f"{sc.name} has empty help string"


def test_subcommand_equality_by_value():
    a = Subcommand("x", "h", mutating=False, lock_mode="shared", needs_clone=False)
    b = Subcommand("x", "h", mutating=False, lock_mode="shared", needs_clone=False)
    assert a == b


# ─── invariant_chain field (this.i node ``scinv4qm``) ──────────────────────


def test_subcommand_has_invariant_chain_default_empty():
    sc = Subcommand("x", "h", mutating=False, lock_mode="shared", needs_clone=False)
    assert sc.invariant_chain == ()


@pytest.mark.parametrize(
    "name,expected_chain",
    [
        (
            "report",
            (
                "gh.authenticated",
                "config.parseable",
                "org.members.fresh",
                "github.reachable",
                "github.not_archived",
                "pr.base_is_default",
                "pr.author_known",
            ),
        ),
        (
            "summarize",
            (
                "gh.authenticated",
                "config.parseable",
                "org.members.fresh",
                "github.reachable",
                "github.not_archived",
                "pr.base_is_default",
                "pr.author_known",
            ),
        ),
        (
            "merge",
            (
                "gh.authenticated",
                "config.parseable",
                "org.members.fresh",
                "github.reachable",
                "github.not_archived",
                "pr.base_is_default",
                "pr.author_known",
                "pr.mergeable_state_clean",
                "pr.required_checks_green",
                "pr.approved_per_policy",
                "pr.no_unresolved_threads",
                "pr.age_threshold",
            ),
        ),
        (
            "close-stale",
            (
                "gh.authenticated",
                "config.parseable",
                "org.members.fresh",
                "github.reachable",
                "github.not_archived",
                "pr.base_is_default",
                "pr.author_known",
                "pr.inactive",
            ),
        ),
        (
            "dispatch",
            (
                "gh.authenticated",
                "config.parseable",
                "org.members.fresh",
                "local.exists",
                "local.remote_matches",
                "local.default_branch_in_sync",
                "github.reachable",
                "github.not_archived",
                "pr.base_is_default",
                "pr.author_known",
            ),
        ),
        (
            "rebase-pr",
            (
                "gh.authenticated",
                "config.parseable",
                "org.members.fresh",
                "local.exists",
                "local.remote_matches",
                "local.default_branch_in_sync",
                "github.reachable",
                "github.not_archived",
                "pr.base_is_default",
                "pr.author_known",
                "pr.needs_rebase",
            ),
        ),
        (
            "prune-branches",
            (
                "gh.authenticated",
                "config.parseable",
                "org.members.fresh",
                "github.reachable",
                "github.not_archived",
            ),
        ),
        (
            "prune-worktrees",
            (
                "gh.authenticated",
                "config.parseable",
                "org.members.fresh",
                "local.exists",
                "local.remote_matches",
                "github.reachable",
                "github.not_archived",
            ),
        ),
        ("show", ()),
        ("ack", ()),
        ("invariants", ()),
    ],
)
def test_subcommand_invariant_chain(name, expected_chain):
    assert by_name(name).invariant_chain == expected_chain


# ─── sets_attention field (this.i node ``aklr5pq3``) ───────────────────────


def test_subcommand_has_sets_attention_default_false():
    sc = Subcommand("x", "h", mutating=False, lock_mode="shared", needs_clone=False)
    assert sc.sets_attention is False


@pytest.mark.parametrize(
    "name,sets_attention",
    [
        ("report", True),
        ("summarize", True),
        ("dispatch", True),
        ("merge", True),
        ("rebase-pr", True),
        ("close-stale", True),
        ("prune-branches", True),
        ("prune-worktrees", True),
        ("show", False),
        ("ack", False),
        ("invariants", False),
    ],
)
def test_subcommand_sets_attention(name, sets_attention):
    assert by_name(name).sets_attention is sets_attention


def test_attention_producing_subcommands_match_nonempty_chains():
    """The six attention-producing subcommands are exactly those that run an
    invariant chain (they are the fleet operations that produce runs)."""
    producing = {s.name for s in KNOWN if s.sets_attention}
    chained = {s.name for s in KNOWN if s.invariant_chain}
    assert producing == chained


def test_clone_subcommands_have_local_invariants():
    """Symmetric: needs_clone ↔ chain includes local.* invariants."""
    for sc in KNOWN:
        local_names = {n for n in sc.invariant_chain if n.startswith("local.")}
        if sc.needs_clone:
            assert local_names, (
                f"{sc.name} has needs_clone=True but no local.* invariants"
            )
        else:
            assert not local_names, (
                f"{sc.name} has needs_clone=False but chain contains "
                f"local.* invariants: {local_names}"
            )
