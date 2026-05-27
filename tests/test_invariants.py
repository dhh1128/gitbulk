"""Tests for the invariants framework (this.i node ivp4wq7n)."""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from gitbulk import paths
from gitbulk.config.policy import Policy
from gitbulk.invariants import (
    ChainResult,
    Fail,
    Invariant,
    InvariantContext,
    InvariantKind,
    Pass,
    Skip,
    all_invariants,
    for_subcommand,
    get,
    register,
    run_chain,
)
from gitbulk.invariants import registry as registry_mod
from gitbulk.runstate import RunState


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure each test starts with an empty registry and leaves no residue."""
    saved = dict(registry_mod._REGISTRY)
    registry_mod._clear()
    yield
    registry_mod._clear()
    registry_mod._REGISTRY.update(saved)


@pytest.fixture
def runstate(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    paths.ensure_directories()
    return RunState.begin("report", ["gitbulk", "report"], {})


@pytest.fixture
def ctx(runstate):
    return InvariantContext(policy=Policy(), runstate=runstate)


# ─── Result dataclasses ────────────────────────────────────────────────────


def test_pass_is_singleton_equality():
    assert Pass() == Pass()


def test_skip_equality_by_reason():
    assert Skip("a") == Skip("a")
    assert Skip("a") != Skip("b")


def test_fail_equality_by_reason():
    assert Fail("x") == Fail("x")
    assert Fail("x") != Fail("y")


def test_invariant_kind_enum_values():
    assert InvariantKind.UNIVERSAL.value == "universal"
    assert InvariantKind.PER_REPO.value == "per-repo"
    assert InvariantKind.PER_PR.value == "per-pr"


# ─── Registry ──────────────────────────────────────────────────────────────


def _make_invariant(name: str, *, kind=InvariantKind.UNIVERSAL, subcommands=("report",)):
    """Construct a concrete Invariant subclass with the given attributes.
    Caller is responsible for register(...)-ing the result if desired."""
    return type(
        f"_Inv_{name.replace('.', '_').replace('-', '_')}",
        (Invariant,),
        {
            "name": name,
            "kind": kind,
            "subcommands": frozenset(subcommands),
            "check": lambda self, ctx: Pass(),
        },
    )


def test_register_adds_class_to_registry():
    cls = _make_invariant("first")
    register(cls)
    assert get("first") is cls
    assert "first" in all_invariants()


def test_register_returns_class_unchanged():
    cls = _make_invariant("identity")
    assert register(cls) is cls


def test_register_duplicate_raises():
    a = _make_invariant("dup")
    register(a)
    b = _make_invariant("dup")
    with pytest.raises(ValueError, match="duplicate invariant name"):
        register(b)


def test_get_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        get("never-registered")


def test_all_invariants_is_shallow_copy():
    cls = _make_invariant("a")
    register(cls)
    snapshot = all_invariants()
    registry_mod._clear()
    # Snapshot must still hold the entry
    assert "a" in snapshot


def test_for_subcommand_filters_by_subcommands_set():
    register(_make_invariant("u", subcommands=("report", "merge")))
    register(_make_invariant("r", subcommands=("merge",)))
    register(_make_invariant("dispatch-only", subcommands=("dispatch",)))
    report_invariants = [c.name for c in for_subcommand("report")]
    assert report_invariants == ["u"]
    merge_invariants = sorted(c.name for c in for_subcommand("merge"))
    assert merge_invariants == ["r", "u"]
    assert [c.name for c in for_subcommand("nonexistent-subcommand")] == []


# ─── run_chain happy paths ─────────────────────────────────────────────────


def _make_check(name, result_factory, subcommands=("report",)):
    return type(
        f"_Inv_{name.replace('.', '_')}",
        (Invariant,),
        {
            "name": name,
            "kind": InvariantKind.UNIVERSAL,
            "subcommands": frozenset(subcommands),
            "check": lambda self, ctx: result_factory(),
        },
    )


def _read_invariants_log(rs: RunState) -> list[dict]:
    log_path = rs.run_dir / "invariants.log"
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines()]


def test_run_chain_all_pass_returns_passed(ctx):
    a = _make_check("a", Pass)
    b = _make_check("b", Pass)
    result = run_chain([a, b], ctx)
    assert result == ChainResult(passed=True, fail_reason=None, skips=())
    events = _read_invariants_log(ctx.runstate)
    assert [e["name"] for e in events] == ["a", "b"]
    assert all(e["result"] == "PASS" for e in events)


def test_run_chain_skip_records_and_continues(ctx):
    a = _make_check("a", lambda: Skip("reason a"))
    b = _make_check("b", Pass)
    result = run_chain([a, b], ctx)
    assert result.passed is True
    assert result.skips == (("a", "reason a"),)
    events = _read_invariants_log(ctx.runstate)
    assert [e["result"] for e in events] == ["SKIP", "PASS"]


def test_run_chain_fail_aborts(ctx):
    a = _make_check("a", Pass)
    b = _make_check("b", lambda: Fail("nope"))
    c = _make_check("c", Pass)  # must never run
    result = run_chain([a, b, c], ctx)
    assert result.passed is False
    assert result.fail_reason == "nope"
    events = _read_invariants_log(ctx.runstate)
    # Only a and b should have been recorded
    assert [e["name"] for e in events] == ["a", "b"]
    assert events[1]["result"] == "FAIL"


def test_run_chain_skip_set_skips_named_invariant(ctx):
    a = _make_check("a", Pass)
    b = _make_check("b", lambda: Fail("would have failed"))  # but it's skipped
    c = _make_check("c", Pass)
    result = run_chain([a, b, c], ctx, skip_set=frozenset({"b"}))
    assert result.passed is True
    assert any(name == "b" for name, _ in result.skips)
    events = _read_invariants_log(ctx.runstate)
    assert [e["result"] for e in events] == ["PASS", "SKIP", "PASS"]
    # The SKIP reason for b mentions the override
    assert "configuration" in events[1]["reason"] or "skip-check" in events[1]["reason"]


# ─── run_chain exception handling ──────────────────────────────────────────


def test_run_chain_exception_in_check_is_converted_to_fail(ctx):
    def raiser():
        raise RuntimeError("kaboom")

    a = _make_check("a", Pass)
    b = type(
        "_Raiser",
        (Invariant,),
        {
            "name": "b-raises",
            "kind": InvariantKind.UNIVERSAL,
            "subcommands": frozenset(["report"]),
            "check": lambda self, ctx: raiser(),
        },
    )
    c = _make_check("c", Pass)  # must never run
    result = run_chain([a, b, c], ctx)
    assert result.passed is False
    assert "RuntimeError" in result.fail_reason
    assert "kaboom" in result.fail_reason
    # invariants.log should record the fail
    events = _read_invariants_log(ctx.runstate)
    assert [e["name"] for e in events] == ["a", "b-raises"]
    assert events[1]["result"] == "FAIL"
    # errors.log should also record context for debugging
    errors_path = ctx.runstate.run_dir / "errors.log"
    assert errors_path.exists()
    error_event = json.loads(errors_path.read_text().splitlines()[0])
    assert error_event["context"]["exception_type"] == "RuntimeError"


def test_run_chain_non_result_return_raises_type_error(ctx):
    """A programmer who returns a string instead of a Result gets a loud failure."""
    bogus = type(
        "_Bogus",
        (Invariant,),
        {
            "name": "bogus",
            "kind": InvariantKind.UNIVERSAL,
            "subcommands": frozenset(["report"]),
            "check": lambda self, ctx: "not a result",  # type: ignore[return-value]
        },
    )
    with pytest.raises(TypeError, match="returned str"):
        run_chain([bogus], ctx)


# ─── Target labelling in invariants.log ───────────────────────────────────


def test_run_chain_records_target(ctx):
    a = _make_check("a", Pass)
    run_chain([a], ctx, target="dhh1128/gitbulk")
    events = _read_invariants_log(ctx.runstate)
    assert events[0]["target"] == "dhh1128/gitbulk"


# ─── ChainResult defaults ──────────────────────────────────────────────────


def test_chainresult_defaults():
    cr = ChainResult(passed=True)
    assert cr.fail_reason is None
    assert cr.skips == ()
