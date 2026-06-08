"""Tests for gitbulk.util.gitref (node gtargv7n).

The validators are the primary defense against argument/path injection from
untrusted gh JSON: a ref beginning with ``-`` becomes a git OPTION (e.g.
``--upload-pack=<cmd>`` → RCE under cron), and a malformed sha can redirect a
REST API path.
"""

from __future__ import annotations

import pytest

from gitbulk.util.gitref import (
    UnsafeGitValue,
    ensure_safe_ref,
    ensure_valid_sha,
    is_safe_ref,
    is_valid_sha,
)


# ─── is_safe_ref ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "feature/x",
        "release-1.2",
        "user/topic.branch",
        "a" * 200,
        "renovate/lock-file-maintenance",
    ],
)
def test_is_safe_ref_accepts_legitimate_refs(ref):
    assert is_safe_ref(ref) is True


@pytest.mark.parametrize(
    "ref",
    [
        "",  # empty
        "-main",  # leading dash → parsed as a git option
        "--upload-pack=/tmp/evil",  # the RCE payload
        "-",
        "has space",
        "tab\tinside",
        "newline\ninside",
        "ctrl\x01char",
        "trailing\x7f",  # DEL
    ],
)
def test_is_safe_ref_rejects_unsafe_refs(ref):
    assert is_safe_ref(ref) is False


def test_is_safe_ref_rejects_non_str():
    assert is_safe_ref(None) is False  # type: ignore[arg-type]


# ─── is_valid_sha ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sha",
    [
        "a" * 40,
        "0123456",  # 7 chars, abbreviated
        "deadbeef",
        "0" * 40,
    ],
)
def test_is_valid_sha_accepts_hex(sha):
    assert is_valid_sha(sha) is True


@pytest.mark.parametrize(
    "sha",
    [
        "",
        "abc",  # too short (<7)
        "a" * 41,  # too long (>40)
        "g" * 40,  # non-hex
        "A" * 40,  # uppercase not allowed (git emits lowercase)
        "../../etc",  # path traversal
        "a" * 39 + "?",  # query injection
        "dead beef",
    ],
)
def test_is_valid_sha_rejects_bad(sha):
    assert is_valid_sha(sha) is False


def test_is_valid_sha_rejects_non_str():
    assert is_valid_sha(None) is False  # type: ignore[arg-type]


# ─── ensure_* (raising variants) ──────────────────────────────────────────────


def test_ensure_safe_ref_returns_ref_when_valid():
    assert ensure_safe_ref("main") == "main"


def test_ensure_safe_ref_raises_on_injection():
    with pytest.raises(UnsafeGitValue) as exc:
        ensure_safe_ref("--upload-pack=/tmp/evil")
    assert "ref" in str(exc.value)


def test_ensure_valid_sha_returns_sha_when_valid():
    assert ensure_valid_sha("a" * 40) == "a" * 40


def test_ensure_valid_sha_raises_on_bad():
    with pytest.raises(UnsafeGitValue) as exc:
        ensure_valid_sha("../../etc/passwd")
    assert "sha" in str(exc.value)
