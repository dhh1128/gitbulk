#!/usr/bin/env python3
"""Build the release assets — the single-file zipapp and ``update.json``.

Shared by ``.github/workflows/release.yml`` (on a tag) and the CI
release-asset validation job (every push/PR), so the asset-construction
logic is exercised before any tag exists and the two paths cannot drift
(this.i node ``cidvp4kr``; the pipeline itself is ``reldst7q``).

Usage:
    build_release_assets.py <version> <outdir> [--repo OWNER/REPO]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Allow running from a source checkout that has not been pip-installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gitbulk.bundle import build_single_file  # noqa: E402 (after sys.path setup)

DEFAULT_REPO = "dhh1128/gitbulk"


def build_assets(version: str, outdir: Path, repo: str) -> tuple[Path, Path]:
    """Build ``outdir/gitbulk`` and ``outdir/update.json``; return their paths.

    ``script_url`` points at the ``releases/latest`` asset, matching
    ``update.DEFAULT_UPDATE_MANIFEST_URL`` so a future ``gitbulk update``
    downloads exactly the binary whose sha256 this manifest records.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    binary = build_single_file(outdir / "gitbulk", version=version)
    sha256 = hashlib.sha256(binary.read_bytes()).hexdigest()
    manifest_path = outdir / "update.json"
    manifest_path.write_text(
        json.dumps(
            {
                "latest_version": version,
                "script_url": f"https://github.com/{repo}/releases/latest/download/gitbulk",
                "sha256": sha256,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return binary, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="version string to bake/record (e.g. 1.2.3)")
    parser.add_argument("outdir", type=Path, help="directory to write gitbulk + update.json into")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"OWNER/REPO (default: {DEFAULT_REPO})")
    args = parser.parse_args()
    binary, manifest = build_assets(args.version, args.outdir, args.repo)
    print(f"built {binary}")
    print(f"wrote {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
