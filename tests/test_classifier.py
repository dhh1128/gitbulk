"""Tests for the humans-vs-bots classifier (this.i node ``hbcls4pq``).

The classifier is a pure function. Tests build canned Policy snapshots
and pass canned org-members frozensets; no mocking, no I/O.
"""

from __future__ import annotations

import pytest

from gitbulk.classifier import Classification, classify_login
from gitbulk.config.policy import HumansConfig, Policy


def _policy(
    *,
    always_human: tuple[str, ...] = (),
    exceptions: tuple[str, ...] = (),
    bots: tuple[str, ...] = (),
    org: str | None = "provenant-dev",
) -> Policy:
    return Policy(
        humans=HumansConfig(
            org=org, always_human=always_human, exceptions=exceptions
        ),
        bots=bots,
    )


# ─── Step 1: always_human shortcut ──────────────────────────────────────────


def test_always_human_returns_human():
    policy = _policy(always_human=("alice",))
    assert classify_login("alice", policy) == Classification.HUMAN


def test_always_human_beats_bot_list():
    """always_human is evaluated before the bot list."""
    policy = _policy(always_human=("alice",), bots=("alice",))
    assert classify_login("alice", policy) == Classification.HUMAN


def test_always_human_beats_exceptions_and_org_membership():
    """always_human wins even if the same login appears as an exception."""
    policy = _policy(always_human=("alice",), exceptions=("alice",))
    members = frozenset({"alice", "bob"})
    assert classify_login("alice", policy, members) == Classification.HUMAN


# ─── Step 2: bot list ───────────────────────────────────────────────────────


def test_bot_list_match_returns_bot():
    policy = _policy(bots=("dependabot[bot]",))
    assert classify_login("dependabot[bot]", policy) == Classification.BOT


def test_bot_list_beats_org_membership():
    """Step 2 (bots) is evaluated before step 3 (org membership)."""
    policy = _policy(bots=("carol",))
    members = frozenset({"carol"})
    assert classify_login("carol", policy, members) == Classification.BOT


# ─── Step 3: org membership ─────────────────────────────────────────────────


def test_org_member_not_in_exceptions_returns_human():
    policy = _policy()
    members = frozenset({"dhh1128", "alice"})
    assert classify_login("dhh1128", policy, members) == Classification.HUMAN


def test_org_member_in_exceptions_falls_through_to_bot():
    """An org member listed in exceptions is NOT classified as human."""
    policy = _policy(exceptions=("octobot",))
    members = frozenset({"octobot", "alice"})
    assert classify_login("octobot", policy, members) == Classification.BOT


# ─── Step 4: default fall-through to BOT ────────────────────────────────────


def test_unknown_login_with_empty_org_members_returns_bot():
    policy = _policy()
    assert classify_login("stranger", policy, frozenset()) == Classification.BOT


def test_unknown_login_with_no_org_members_returns_bot():
    """org_members=None must skip step 3 and fall through to step 4."""
    policy = _policy()
    assert classify_login("stranger", policy, None) == Classification.BOT


def test_unknown_login_default_org_members_arg_returns_bot():
    """Calling without the org_members kwarg uses the default None."""
    policy = _policy()
    assert classify_login("stranger", policy) == Classification.BOT


def test_empty_policy_unknown_login_returns_bot():
    """A wholly empty Policy (no org configured) classifies unknowns as BOT."""
    policy = Policy()
    assert classify_login("anyone", policy) == Classification.BOT
    assert classify_login("anyone", policy, frozenset()) == Classification.BOT


# ─── Layered precedence ─────────────────────────────────────────────────────


def test_login_in_multiple_lists_always_human_wins_over_bots_wins_over_org():
    """Construct three logins each colliding across two lists; verify order."""
    policy = _policy(
        always_human=("a",),
        bots=("a", "b"),  # 'a' in both always_human and bots
        exceptions=(),
    )
    members = frozenset({"a", "b", "c"})
    # 'a' in always_human and bots → HUMAN (step 1 wins)
    assert classify_login("a", policy, members) == Classification.HUMAN
    # 'b' in bots and org_members → BOT (step 2 wins over step 3)
    assert classify_login("b", policy, members) == Classification.BOT
    # 'c' only in org_members → HUMAN (step 3)
    assert classify_login("c", policy, members) == Classification.HUMAN


# ─── Enum surface ───────────────────────────────────────────────────────────


def test_classification_enum_values():
    """Stable string values are part of the schema; pin them."""
    assert Classification.HUMAN.value == "human"
    assert Classification.BOT.value == "bot"
    assert Classification.UNKNOWN.value == "unknown"


def test_classification_is_str_enum():
    """Classification members compare equal to their string values."""
    assert Classification.HUMAN == "human"


@pytest.mark.parametrize(
    "login,expected",
    [
        ("alice", Classification.HUMAN),  # always_human
        ("dependabot[bot]", Classification.BOT),  # bots
        ("dhh1128", Classification.HUMAN),  # org member, not exception
        ("octobot", Classification.BOT),  # org member, in exceptions
        ("stranger", Classification.BOT),  # default fall-through
    ],
)
def test_classify_login_table(login, expected):
    policy = _policy(
        always_human=("alice",),
        bots=("dependabot[bot]",),
        exceptions=("octobot",),
    )
    members = frozenset({"dhh1128", "octobot"})
    assert classify_login(login, policy, members) == expected
