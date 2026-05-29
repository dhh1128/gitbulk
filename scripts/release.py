#!/usr/bin/env python3
"""Cut a gitbulk release: bump version, commit, push, tag, push tag.

This is a HUMAN-run script (this.i node reldst7q). It is intentionally not
under src/gitbulk/ and not covered by the test gate. AGENTS.md reserves
pushes to main and tags for humans; AI agents must not run this.

Usage:
    python scripts/release.py                       # patch bump, default message
    python scripts/release.py -m "add foo feature"  # patch bump, custom message
    python scripts/release.py --minor -m "new API"  # minor bump, custom message
    python scripts/release.py --major -m "rewrite"  # major bump, custom message
    python scripts/release.py --set 0.5.0 -m "..."  # set an explicit version
                                                    #   (e.g. a starting point;
                                                    #    must be > current, and
                                                    #    may not jump the major
                                                    #    by >1 without
                                                    #    --allow-major-jump)

After the tag reaches GitHub, .github/workflows/release.yml builds the
single-file bundle + update.json and publishes them as release assets.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def run(cmd, *, capture=False, check=True):
    return subprocess.run(cmd, capture_output=capture, text=True, check=check, cwd=REPO_ROOT)


def get(cmd):
    return run(cmd, capture=True).stdout.strip()


def current_version():
    m = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT.read_text(), re.MULTILINE)
    if not m:
        sys.exit("Could not find version in pyproject.toml")
    return m.group(1)


def bump(version, part):
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def parse_explicit_version(value, current, *, allow_major_jump=False):
    """Validate an explicit --set version.

    Guards (release.yml only checks tag==version, not sanity, so the sanity
    lives here):
      * shape X.Y.Z;
      * strictly greater than current (no downgrade);
      * the major component rises by at most one step — a jump of two or more
        is almost always a typo (e.g. ``--set 5.0.0`` for ``0.5.0``), so it is
        refused unless ``--allow-major-jump`` is passed.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        sys.exit(f"--set expects X.Y.Z (got {value!r}).")
    as_tuple = lambda v: tuple(int(p) for p in v.split("."))  # noqa: E731
    new, cur = as_tuple(value), as_tuple(current)
    if new <= cur:
        sys.exit(f"--set {value} is not greater than current {current}; refusing to downgrade.")
    if new[0] - cur[0] > 1 and not allow_major_jump:
        sys.exit(
            f"--set {value} raises the major version from {cur[0]} to {new[0]} "
            f"(more than one step) — almost always a typo. "
            f"If it is intentional, re-run with --allow-major-jump."
        )
    return value


def check_branch():
    branch = get(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if branch != "main":
        sys.exit(f"Must be on main branch (currently on {branch!r}).")


def check_clean():
    if run(["git", "status", "--porcelain"], capture=True).stdout.strip():
        sys.exit("Working tree is not clean. Commit or stash changes first.")


def check_in_sync():
    run(["git", "fetch", "--quiet"])
    if get(["git", "rev-parse", "HEAD"]) != get(["git", "rev-parse", "origin/main"]):
        ahead = get(["git", "rev-list", "--count", "origin/main..HEAD"])
        behind = get(["git", "rev-list", "--count", "HEAD..origin/main"])
        sys.exit(
            f"Local main is not in sync with origin/main "
            f"({ahead} ahead, {behind} behind). Push or pull first."
        )


def run_tests():
    """Run the same gate CI enforces: full suite at 100% branch coverage."""
    print("Running tests (100% branch coverage gate)...")
    run([
        "uv", "run", "--extra", "test", "pytest", "-q",
        "--cov=src/gitbulk", "--cov-branch", "--cov-fail-under=100",
    ])


def set_version(new_version):
    text = PYPROJECT.read_text()
    updated = re.sub(
        r'^(version\s*=\s*)"[^"]+"',
        f'\\g<1>"{new_version}"',
        text,
        flags=re.MULTILINE,
    )
    if updated == text:
        sys.exit("Version substitution in pyproject.toml had no effect.")
    PYPROJECT.write_text(updated)


def prompt_message(part):
    if not sys.stdin.isatty():
        sys.exit(f"--{part} release requires a commit message; pass -m '<message>'.")
    try:
        msg = input(f"Commit message for {part} release: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nAborted.")
    if not msg:
        sys.exit("Commit message cannot be empty.")
    return msg


def main():
    parser = argparse.ArgumentParser(
        description="Cut a release. Defaults to --patch if no bump flag is given.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--major", dest="part", action="store_const", const="major")
    group.add_argument("--minor", dest="part", action="store_const", const="minor")
    group.add_argument("--patch", dest="part", action="store_const", const="patch")
    group.add_argument(
        "--set", dest="explicit", metavar="X.Y.Z", default=None,
        help="set an explicit version (e.g. a starting point) instead of bumping; must be > current",
    )
    parser.add_argument(
        "--allow-major-jump", action="store_true",
        help="permit --set to raise the major version by more than one step "
             "(default: refused as a likely typo)",
    )
    parser.add_argument("-m", dest="message", default=None, help="commit message")
    args = parser.parse_args()

    old = current_version()
    if args.explicit:
        new = parse_explicit_version(args.explicit, old, allow_major_jump=args.allow_major_jump)
        label = "set"
    else:
        label = args.part or "patch"
        new = bump(old, label)

    if args.message:
        message = args.message
    elif label == "patch":
        message = "misc fixes/enhancements"
    else:
        message = prompt_message(label)

    check_branch()
    check_clean()
    check_in_sync()
    run_tests()

    tag = f"v{new}"
    verb = "Setting" if args.explicit else "Bumping"
    print(f"{verb} {old} -> {new}")
    set_version(new)

    run(["git", "add", str(PYPROJECT.relative_to(REPO_ROOT))])
    # DCO sign-off (the user works in DCO-enforced repos and signs every commit).
    run(["git", "commit", "-s", "-m", f"Release {tag}: {message}"])
    run(["git", "push", "origin", "main"])
    run(["git", "tag", "-a", tag, "-m", f"Release {tag}: {message}"])
    run(["git", "push", "origin", tag])

    print(f"Tagged and pushed {tag}. The release GHA will build and publish the assets.")


if __name__ == "__main__":
    main()
