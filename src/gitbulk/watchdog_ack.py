"""Persistent ack cache for the post-merge CD watchdog.

Once ``gitbulk report`` has observed a merge commit's check-runs in a
"clean and complete" state — every check in ``status=completed`` with
``conclusion`` in {success, skipped, neutral} — that merge can be
crossed off the worry list permanently. Subsequent report runs skip
re-fetching it.

Decision recorded in this.i as ``yhwagcvw``: prefer ack-on-first-clean
over perpetual re-checking. The tradeoff (a delayed scheduled check_run
that lands later and fails would be missed) is documented there.

File: ``~/.cache/gitbulk/watchdog-acked.yaml``
Schema::

    version: 1
    acked:
      - slug: owner/repo
        sha: <40-char>
        acked_at: <ISO-8601 UTC>

Pruning: entries older than 7 days are dropped at every ``record_ack``
call. The 24h scan window in ``_check_recent_merges`` already prevents
older merges from appearing as candidates, so the 7-day buffer is just
housekeeping headroom.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from gitbulk import paths

#: Schema version stamped into the cache file.
_SCHEMA_VERSION = 1

#: Entries older than this are pruned at write time.
_RETENTION = timedelta(days=7)


def _ack_file() -> Path:
    return paths.cache_dir() / "watchdog-acked.yaml"


def load_acked() -> set[tuple[str, str]]:
    """Return the set of (slug, sha) pairs that have been ack'd.

    Missing file, unparseable YAML, or a schema mismatch all return the
    empty set — the watchdog falls back to re-fetching, which is safe
    and self-healing (correctness > perf when the cache is suspect).
    """
    path = _ack_file()
    if not path.exists():
        return set()
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return set()
    if not isinstance(doc, dict):
        return set()
    if doc.get("version") != _SCHEMA_VERSION:
        return set()
    entries = doc.get("acked") or []
    if not isinstance(entries, list):
        return set()
    out: set[tuple[str, str]] = set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        slug = e.get("slug")
        sha = e.get("sha")
        if isinstance(slug, str) and isinstance(sha, str):
            out.add((slug, sha))
    return out


def record_ack(slug: str, sha: str, now: datetime) -> None:
    """Add (slug, sha) to the ack cache; prune entries older than 7 days.

    Idempotent: re-ack'ing an existing pair just refreshes its
    ``acked_at`` timestamp.
    """
    path = _ack_file()
    cutoff = now - _RETENTION
    # Load and parse existing entries; preserve unknown keys defensively
    # (a future schema version might add fields we don't recognize).
    existing: list[dict] = []
    if path.exists():
        try:
            doc = yaml.safe_load(path.read_text())
            if isinstance(doc, dict) and doc.get("version") == _SCHEMA_VERSION:
                raw = doc.get("acked")
                if isinstance(raw, list):
                    existing = [e for e in raw if isinstance(e, dict)]
        except yaml.YAMLError:
            existing = []
    # Drop the pair we're about to re-record (idempotent re-ack) and
    # drop anything older than the retention window.
    kept: list[dict] = []
    for e in existing:
        if e.get("slug") == slug and e.get("sha") == sha:
            continue  # replaced below
        at_raw = e.get("acked_at")
        if isinstance(at_raw, str):
            try:
                at = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
            except ValueError:
                # Unparseable timestamp → drop conservatively.
                continue
            if at < cutoff:
                continue
        kept.append(e)
    kept.append(
        {
            "slug": slug,
            "sha": sha,
            "acked_at": now.astimezone(timezone.utc).isoformat(),
        }
    )
    paths.cache_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"version": _SCHEMA_VERSION, "acked": kept})
    )


__all__ = ["load_acked", "record_ack"]
