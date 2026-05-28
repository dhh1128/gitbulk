"""Tests for pr_info.py (this.i node prdtm4kn)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gitbulk.pr_info import (
    KNOWN_CHECKS_STATUSES,
    KNOWN_MERGEABLE_STATES,
    KNOWN_REVIEW_DECISIONS,
    PRInfo,
)


def _make_pr(**overrides) -> PRInfo:
    """Construct a PRInfo with sensible defaults; tests override specific fields."""
    defaults = dict(
        slug="dhh1128/gitbulk",
        number=42,
        title="Fix the thing",
        url="https://github.com/dhh1128/gitbulk/pull/42",
        author="dhh1128",
        base_ref="main",
        head_ref="feature/x",
        head_sha="a" * 40,
        state="OPEN",
        is_draft=False,
        mergeable_state="CLEAN",
        created_at=datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc),
        last_pushed_at=datetime(2026, 5, 28, 11, 0, 0, tzinfo=timezone.utc),
        labels=(),
        review_decision="APPROVED",
        checks_status="SUCCESS",
    )
    defaults.update(overrides)
    return PRInfo(**defaults)


def test_prinfo_basic_construction():
    pr = _make_pr()
    assert pr.slug == "dhh1128/gitbulk"
    assert pr.number == 42
    assert pr.state == "OPEN"


def test_prinfo_is_frozen():
    pr = _make_pr()
    with pytest.raises((AttributeError, Exception)):
        pr.number = 99  # type: ignore[misc]


def test_prinfo_equality_by_value():
    a = _make_pr()
    b = _make_pr()
    assert a == b


def test_prinfo_inequality_by_value():
    a = _make_pr(number=1)
    b = _make_pr(number=2)
    assert a != b


def test_prinfo_optional_fields_accept_none():
    pr = _make_pr(
        mergeable_state=None,
        last_pushed_at=None,
        review_decision=None,
        checks_status=None,
    )
    assert pr.mergeable_state is None
    assert pr.last_pushed_at is None
    assert pr.review_decision is None
    assert pr.checks_status is None


def test_prinfo_labels_is_tuple_of_strings():
    pr = _make_pr(labels=("bug", "needs-review"))
    assert pr.labels == ("bug", "needs-review")


def test_known_value_sets_contain_expected_members():
    # The exact lists may grow as GitHub adds states; these are the
    # baseline values we know exist at Phase 2 entry. The test asserts
    # the well-known values without claiming the sets are closed.
    assert "CLEAN" in KNOWN_MERGEABLE_STATES
    assert "DIRTY" in KNOWN_MERGEABLE_STATES
    assert "BLOCKED" in KNOWN_MERGEABLE_STATES

    assert "APPROVED" in KNOWN_REVIEW_DECISIONS
    assert "CHANGES_REQUESTED" in KNOWN_REVIEW_DECISIONS
    assert "REVIEW_REQUIRED" in KNOWN_REVIEW_DECISIONS

    assert "SUCCESS" in KNOWN_CHECKS_STATUSES
    assert "FAILURE" in KNOWN_CHECKS_STATUSES
    assert "PENDING" in KNOWN_CHECKS_STATUSES


def test_known_value_sets_are_frozen():
    """Frozenset prevents accidental in-place mutation by callers."""
    for s in (KNOWN_MERGEABLE_STATES, KNOWN_REVIEW_DECISIONS, KNOWN_CHECKS_STATUSES):
        assert isinstance(s, frozenset)
