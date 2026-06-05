"""Self-update (this.i nodes ``updnc5kr``, ``updtg6qn``, ``shano4kp``).

All tests inject the manifest, the payload fetcher, and ``subprocess.run``
so nothing touches the network (AGENTS.md offline-tests rule). Versions are
pinned by monkeypatching the module ``__version__`` for determinism.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from gitbulk import update
from gitbulk.update import (
    DEFAULT_UPDATE_MANIFEST_URL,
    UpdateError,
    UpdateStatus,
    apply_update,
    atomic_replace_bytes,
    atomic_replace_script,
    check_update,
    fetch_bytes,
    load_update_manifest,
    parse_version,
    read_payload,
    resolve_update_target,
    running_as_zipapp,
    sha256_hex,
    suggested_update_command,
    _gh_fetch,
)


@pytest.fixture(autouse=True)
def pin_version(monkeypatch):
    monkeypatch.setattr(update, "__version__", "1.0.0")


def _manifest_file(tmp_path, **fields) -> Path:
    p = tmp_path / "update.json"
    p.write_text(json.dumps({"latest_version": "1.0.0", **fields}), encoding="utf-8")
    return p


class _Result:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── version parsing & manifest loading ────────────────────────────────────────


def test_parse_version():
    assert parse_version("1.2.3") == (1, 2, 3)


def test_load_manifest_from_local_file(tmp_path):
    p = _manifest_file(tmp_path, latest_version="2.0.0")
    assert load_update_manifest(p)["latest_version"] == "2.0.0"


def test_load_manifest_from_https_uses_fetcher():
    data = load_update_manifest("https://x/u.json", fetcher=lambda url: b'{"latest_version":"3.0.0"}')
    assert data["latest_version"] == "3.0.0"


def test_load_manifest_http_rejected():
    # SEC-F5: an http:// manifest URL is refused before any (cleartext) fetch.
    def _never(url):  # pragma: no cover - must not be invoked
        raise AssertionError("fetcher must not be called for http://")

    with pytest.raises(UpdateError, match="non-https"):
        load_update_manifest("http://x/u.json", fetcher=_never)


def test_load_manifest_fetcher_only():
    data = load_update_manifest(None, fetcher=lambda: '{"latest_version":"4.0.0"}')
    assert data["latest_version"] == "4.0.0"


def test_load_manifest_default_is_current_version():
    assert load_update_manifest()["latest_version"] == "1.0.0"


# ── check_update ──────────────────────────────────────────────────────────────


def test_check_update_newer_available(tmp_path):
    p = _manifest_file(tmp_path, latest_version="1.1.0", script_url="x", sha256="y")
    status = check_update(p)
    assert status.update_available is True
    assert status.script_url == "x" and status.sha256 == "y"


def test_check_update_equal_not_available(tmp_path):
    assert check_update(_manifest_file(tmp_path, latest_version="1.0.0")).update_available is False


def test_check_update_older_not_available(tmp_path):
    assert check_update(_manifest_file(tmp_path, latest_version="0.9.0")).update_available is False


# ── payload helpers ───────────────────────────────────────────────────────────


def test_sha256_hex():
    assert sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_read_payload_file_url(tmp_path):
    f = tmp_path / "asset"
    f.write_bytes(b"data")
    assert read_payload(f"file://{f}") == b"data"


def test_read_payload_https_uses_fetcher():
    assert read_payload("https://x/a", fetcher=lambda url: b"net") == b"net"


def test_read_payload_http_rejected():
    # SEC-F5: an http:// payload URL is refused before any (cleartext) fetch,
    # even when a custom fetcher is supplied — the fetcher is never called.
    def _never(url):  # pragma: no cover - must not be invoked
        raise AssertionError("fetcher must not be called for http://")

    with pytest.raises(UpdateError, match="non-https"):
        read_payload("http://x/a", fetcher=_never)


def test_read_payload_plain_path(tmp_path):
    f = tmp_path / "asset"
    f.write_bytes(b"plain")
    assert read_payload(str(f)) == b"plain"


# ── _gh_fetch ─────────────────────────────────────────────────────────────────


def test_gh_fetch_latest_url(monkeypatch):
    seen = {}

    def _run(cmd, capture_output):
        seen["cmd"] = cmd
        return _Result(stdout=b"binary")

    monkeypatch.setattr(update.subprocess, "run", _run)
    out = _gh_fetch(f"https://github.com/{update.REPO}/releases/latest/download/gitbulk")
    assert out == b"binary"
    assert seen["cmd"][:3] == ["gh", "release", "download"]
    assert "--repo" in seen["cmd"] and update.REPO in seen["cmd"]


def test_gh_fetch_tagged_url(monkeypatch):
    seen = {}

    def _run(cmd, capture_output):
        seen["cmd"] = cmd
        return _Result(stdout=b"tagged")

    monkeypatch.setattr(update.subprocess, "run", _run)
    out = _gh_fetch(f"https://github.com/{update.REPO}/releases/download/v1.2.3/gitbulk")
    assert out == b"tagged"
    assert "v1.2.3" in seen["cmd"]


def test_gh_fetch_non_github_url_returns_none():
    assert _gh_fetch("https://example.com/gitbulk") is None


def test_gh_fetch_gh_missing_raises(monkeypatch):
    def _run(cmd, capture_output):
        raise FileNotFoundError

    monkeypatch.setattr(update.subprocess, "run", _run)
    with pytest.raises(UpdateError, match="gh CLI not found"):
        _gh_fetch(f"https://github.com/{update.REPO}/releases/latest/download/gitbulk")


def test_gh_fetch_nonzero_with_stderr(monkeypatch):
    monkeypatch.setattr(update.subprocess, "run", lambda c, capture_output: _Result(returncode=1, stderr=b"boom\nbad"))
    with pytest.raises(UpdateError, match="boom"):
        _gh_fetch(f"https://github.com/{update.REPO}/releases/latest/download/gitbulk")


def test_gh_fetch_nonzero_no_stderr(monkeypatch):
    monkeypatch.setattr(update.subprocess, "run", lambda c, capture_output: _Result(returncode=1, stderr=b""))
    with pytest.raises(UpdateError, match="no output"):
        _gh_fetch(f"https://github.com/{update.REPO}/releases/latest/download/gitbulk")


# ── fetch_bytes ───────────────────────────────────────────────────────────────


def test_fetch_bytes_uses_gh_for_release_url(monkeypatch):
    monkeypatch.setattr(update, "_gh_fetch", lambda url: b"gh-bytes")
    assert fetch_bytes("https://github.com/x/y/releases/latest/download/z") == b"gh-bytes"


def test_fetch_bytes_falls_back_to_urlopen(monkeypatch):
    monkeypatch.setattr(update, "_gh_fetch", lambda url: None)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"url-bytes"

    monkeypatch.setattr(update, "urlopen", lambda url, timeout: _Resp())
    assert fetch_bytes("https://example.com/a") == b"url-bytes"


def test_fetch_bytes_rejects_http(monkeypatch):
    # SEC-F5: a non-release http:// URL falls past _gh_fetch and must be
    # refused before urlopen is ever called (making the S310 claim true).
    monkeypatch.setattr(update, "_gh_fetch", lambda url: None)
    monkeypatch.setattr(
        update,
        "urlopen",
        lambda url, timeout: (_ for _ in ()).throw(
            AssertionError("urlopen must not be called for http://")
        ),
    )
    with pytest.raises(UpdateError, match="non-https"):
        fetch_bytes("http://example.com/a")


# ── atomic_replace_bytes ──────────────────────────────────────────────────────


def test_atomic_replace_new_executable(tmp_path):
    target = tmp_path / "gitbulk"
    atomic_replace_bytes(target, b"new", executable=True)
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode & 0o100


def test_atomic_replace_existing_preserves_mode(tmp_path):
    target = tmp_path / "gitbulk"
    target.write_bytes(b"old")
    target.chmod(0o755)
    atomic_replace_script(target, b"updated")
    assert target.read_bytes() == b"updated"
    assert target.stat().st_mode & 0o100


def test_atomic_replace_non_executable(tmp_path):
    target = tmp_path / "data.json"
    atomic_replace_bytes(target, b"{}", executable=False)
    assert not (target.stat().st_mode & 0o111)


def test_atomic_replace_cleans_temp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "gitbulk"
    target.write_bytes(b"old")

    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(update.os, "replace", _boom)
    with pytest.raises(OSError, match="replace failed"):
        atomic_replace_bytes(target, b"x", executable=True)
    assert not list(tmp_path.glob(".gitbulk.*.tmp")), "temp file must be cleaned up"
    assert target.read_bytes() == b"old"


def test_atomic_replace_swallows_missing_temp_on_cleanup(tmp_path, monkeypatch):
    target = tmp_path / "gitbulk"
    target.write_bytes(b"old")

    def _replace_then_vanish(src, dst):
        os.unlink(src)  # temp already gone before cleanup runs
        raise OSError("replace failed after temp removed")

    monkeypatch.setattr(update.os, "replace", _replace_then_vanish)
    with pytest.raises(OSError, match="after temp removed"):
        atomic_replace_bytes(target, b"x", executable=True)


# ── running_as_zipapp ─────────────────────────────────────────────────────────


def test_running_as_zipapp_true_for_zip(tmp_path):
    import zipfile

    z = tmp_path / "gitbulk"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("__main__.py", "print('hi')")
    assert running_as_zipapp(z) is True


def test_running_as_zipapp_false_for_text(tmp_path):
    f = tmp_path / "gitbulk"
    f.write_text("#!/usr/bin/env python3\nprint(1)\n")
    assert running_as_zipapp(f) is False


def test_running_as_zipapp_false_on_oserror(tmp_path, monkeypatch):
    def _boom(_path):
        raise OSError("io error probing archive")

    monkeypatch.setattr(update.zipfile, "is_zipfile", _boom)
    assert running_as_zipapp(tmp_path / "gitbulk") is False


# ── resolve_update_target ─────────────────────────────────────────────────────


def test_resolve_target_absolute(tmp_path):
    f = tmp_path / "gitbulk"
    f.write_text("x")
    assert resolve_update_target(str(f)) == f.resolve()


def test_resolve_target_bare_name_via_which(tmp_path, monkeypatch):
    f = tmp_path / "gitbulk"
    f.write_text("x")
    monkeypatch.setattr(update.shutil, "which", lambda name: str(f))
    assert resolve_update_target("gitbulk") == f.resolve()


def test_resolve_target_bare_name_not_found(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    assert resolve_update_target("gitbulk") == Path("gitbulk").resolve()


# ── suggested_update_command ──────────────────────────────────────────────────


def test_suggested_command_bare():
    assert suggested_update_command() == "gitbulk update"


def test_suggested_command_with_target():
    assert "--target" in suggested_update_command(target="/p/gitbulk")


def test_suggested_command_custom_manifest():
    assert "--manifest" in suggested_update_command(manifest="/local/u.json")


def test_suggested_command_default_manifest_omitted():
    assert suggested_update_command(manifest=DEFAULT_UPDATE_MANIFEST_URL) == "gitbulk update"


# ── apply_update ──────────────────────────────────────────────────────────────


def test_apply_update_not_available_is_noop(tmp_path):
    target = tmp_path / "gitbulk"
    target.write_bytes(b"current")
    status = apply_update(target=target, manifest_path=_manifest_file(tmp_path, latest_version="1.0.0"))
    assert status.update_available is False
    assert target.read_bytes() == b"current"


def test_apply_update_missing_script_url(tmp_path):
    p = _manifest_file(tmp_path, latest_version="1.1.0", sha256="abc")
    with pytest.raises(UpdateError, match="missing script_url"):
        apply_update(target=tmp_path / "gitbulk", manifest_path=p)


def test_apply_update_missing_sha256(tmp_path):
    p = _manifest_file(tmp_path, latest_version="1.1.0", script_url="file:///x")
    with pytest.raises(UpdateError, match="missing sha256"):
        apply_update(target=tmp_path / "gitbulk", manifest_path=p)


def test_apply_update_sha_mismatch_leaves_target(tmp_path):
    target = tmp_path / "gitbulk"
    target.write_bytes(b"current")
    asset = tmp_path / "new"
    asset.write_bytes(b"new-binary")
    p = _manifest_file(tmp_path, latest_version="1.1.0", script_url=f"file://{asset}", sha256="deadbeef")
    with pytest.raises(UpdateError, match="sha256 does not match"):
        apply_update(target=target, manifest_path=p)
    assert target.read_bytes() == b"current"


def test_apply_update_success_replaces_target(tmp_path):
    target = tmp_path / "gitbulk"
    target.write_bytes(b"current")
    asset = tmp_path / "new"
    asset.write_bytes(b"new-binary")
    p = _manifest_file(
        tmp_path,
        latest_version="1.1.0",
        script_url=f"file://{asset}",
        sha256=sha256_hex(b"new-binary"),
    )
    status = apply_update(target=target, manifest_path=p)
    assert status.update_available is True
    assert target.read_bytes() == b"new-binary"
    assert target.stat().st_mode & 0o100
