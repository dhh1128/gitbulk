"""Self-update for the ``gitbulk`` zipapp (this.i nodes ``updnc5kr``,
``updtg6qn``, ``shano4kp``).

``gitbulk update`` fetches ``update.json`` from the latest GitHub release,
compares versions, downloads the new binary (over authenticated ``gh`` when
the URL is a GitHub release asset), verifies its sha256, and atomically
replaces the running binary in place. Distinctive choices vs agentprep:

* **sha256 only** — no HMAC manifest signature (node ``shano4kp``).
* **refuse to clobber a pip install** — :func:`running_as_zipapp` lets the
  CLI redirect pip/pipx users to ``pip install -U`` instead of overwriting
  a venv entry-point with a downloaded zipapp (node ``updtg6qn``).
* the periodic *notice* is never an auto-apply and is TTY-gated in the CLI
  (node ``updnc5kr``); this module only provides the check + the apply.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

from . import __version__

REPO = "dhh1128/gitbulk"
DEFAULT_UPDATE_MANIFEST_URL = (
    f"https://github.com/{REPO}/releases/latest/download/update.json"
)
_RELEASES_URL = f"https://github.com/{REPO}/releases"


@dataclass(frozen=True)
class UpdateStatus:
    current_version: str
    latest_version: str
    update_available: bool
    script_url: str | None = None
    sha256: str | None = None


class UpdateError(RuntimeError):
    pass


def parse_version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def load_update_manifest(manifest_path: Path | str | None = None, fetcher=None) -> dict:
    if manifest_path is not None:
        source = str(manifest_path)
        if source.startswith("http://"):
            # https-only at the network boundary (SEC-F5): never fetch a
            # manifest over cleartext http.
            raise UpdateError(
                f"refusing to fetch update manifest over a non-https URL: "
                f"{source!r} (only https:// is allowed for network downloads)"
            )
        if source.startswith("https://"):
            raw = (fetcher or fetch_bytes)(source)
            return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return json.loads(Path(source).read_text(encoding="utf-8"))
    if fetcher is not None:
        return json.loads(fetcher())
    return {"latest_version": __version__}


def check_update(manifest_path: Path | str | None = None, fetcher=None) -> UpdateStatus:
    data = load_update_manifest(manifest_path, fetcher)
    latest = data["latest_version"]
    return UpdateStatus(
        current_version=__version__,
        latest_version=latest,
        update_available=parse_version(latest) > parse_version(__version__),
        script_url=data.get("script_url"),
        sha256=data.get("sha256"),
    )


_GH_LATEST_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/]+/[^/]+)/releases/latest/download/(?P<filename>[^/?]+)"
)
_GH_TAG_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/]+/[^/]+)/releases/download/(?P<tag>[^/]+)/(?P<filename>[^/?]+)"
)


def _gh_fetch(url: str) -> bytes | None:
    """Fetch a GitHub release asset via ``gh``. None if not a release URL.

    Routing release downloads through ``gh`` means the same authenticated
    path works for a private repo (node ``bootp4mq``).
    """
    # verified non-deprecated against gh CLI 2026-05-29 (gh 2.92.0):
    # `gh release download --repo --pattern --output -` carries no warning.
    m = _GH_LATEST_RE.match(url)
    if m:
        cmd = ["gh", "release", "download",
               "--repo", m.group("repo"),
               "--pattern", m.group("filename"),
               "--output", "-"]
    else:
        m = _GH_TAG_RE.match(url)
        if not m:
            return None
        cmd = ["gh", "release", "download", m.group("tag"),
               "--repo", m.group("repo"),
               "--pattern", m.group("filename"),
               "--output", "-"]
    try:
        result = subprocess.run(cmd, capture_output=True)
    except FileNotFoundError:
        raise UpdateError("gh CLI not found; install it from https://cli.github.com/")
    if result.returncode != 0:
        raw = result.stderr.decode().strip() or "no output"
        raise UpdateError(
            f"gh release download failed while fetching:\n  {url}\n"
            f"  gh output:\n"
            + "".join(f"    {line}\n" for line in raw.splitlines())
            + f"  Download manually from: {_RELEASES_URL}"
        )
    return result.stdout


def fetch_bytes(url: str, timeout: float = 10.0) -> bytes:
    payload = _gh_fetch(url)
    if payload is not None:
        return payload
    # https-only at the network boundary (SEC-F5): an http:// script_url /
    # manifest URL would otherwise be fetched in cleartext, with only the
    # sha256 gate protecting integrity. Reject it here so the urlopen below
    # genuinely only ever sees https — making the S310 claim true rather than
    # aspirational. (_gh_fetch above only matches https GitHub release URLs, so
    # anything reaching this point is a raw urlopen target.)
    if not url.startswith("https://"):
        raise UpdateError(
            f"refusing to fetch update over a non-https URL: {url!r} "
            "(only https:// is allowed for network downloads)"
        )
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 (https only by construction)
        return response.read()


def read_payload(source: str, fetcher=None) -> bytes:
    if source.startswith("file://"):
        return Path(source[7:]).read_bytes()
    if source.startswith("http://"):
        # https-only at the network boundary (SEC-F5): an http:// script_url
        # would be fetched in cleartext (integrity is only sha256-gated).
        raise UpdateError(
            f"refusing to fetch update payload over a non-https URL: "
            f"{source!r} (only https:// is allowed for network downloads)"
        )
    if source.startswith("https://"):
        return (fetcher or fetch_bytes)(source)
    return Path(source).read_bytes()


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_replace_bytes(target: Path, payload: bytes, *, executable: bool) -> None:
    """Write ``payload`` to ``target`` atomically (tempfile -> fsync -> replace).

    An interrupted update can never leave a half-written binary (node
    ``updnc5kr``): the rename is atomic and the temp file is cleaned up on
    any failure.
    """
    target = target.resolve()
    target_dir = target.parent
    mode = target.stat().st_mode if target.exists() else (0o755 if executable else 0o644)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target_dir)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as temp:
            temp.write(payload)
            temp.flush()
            os.fsync(temp.fileno())
        if executable:
            mode = mode | stat.S_IXUSR
        temp_path.chmod(mode)
        os.replace(temp_path, target)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_replace_script(target: Path, payload: bytes) -> None:
    atomic_replace_bytes(target, payload, executable=True)


def running_as_zipapp(target: Path) -> bool:
    """True if ``target`` is the zipapp bundle (a zip archive).

    A pip/pipx console-script or ``python -m gitbulk`` resolves to a text
    shim or a source file, which is not a zip — so this distinguishes the
    self-replaceable zipapp from a pip install (node ``updtg6qn``).
    """
    try:
        return zipfile.is_zipfile(target)
    except OSError:
        return False


def resolve_update_target(argv0: str) -> Path:
    candidate = Path(argv0)
    if candidate.parent != Path(".") or candidate.is_absolute():
        return candidate.resolve()
    found = shutil.which(argv0)
    if found:
        return Path(found).resolve()
    return candidate.resolve()


def suggested_update_command(*, target: Path | str | None = None, manifest: Path | str | None = None) -> str:
    parts = ["gitbulk", "update"]
    if target is not None:
        parts.extend(["--target", str(target)])
    if manifest is not None and str(manifest) != DEFAULT_UPDATE_MANIFEST_URL:
        parts.extend(["--manifest", str(manifest)])
    return " ".join(shlex.quote(part) for part in parts)


def apply_update(
    *,
    target: Path,
    manifest_path: Path | str | None = None,
    manifest_fetcher=None,
    payload_fetcher=None,
) -> UpdateStatus:
    status = check_update(manifest_path, manifest_fetcher)
    if not status.update_available:
        return status
    if not status.script_url:
        raise UpdateError("update manifest is missing script_url")
    if not status.sha256:
        raise UpdateError("update manifest is missing sha256")
    payload = read_payload(status.script_url, payload_fetcher)
    actual_hash = sha256_hex(payload)
    if not hmac.compare_digest(actual_hash, status.sha256):
        raise UpdateError("downloaded binary sha256 does not match manifest")
    atomic_replace_script(target, payload)
    return status
