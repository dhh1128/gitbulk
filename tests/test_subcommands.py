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
        "rebase-onto-default",
        "close-stale",
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
        ("rebase-onto-default", True, "exclusive", True),
        ("close-stale", True, "exclusive", False),
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
