"""Business-day arithmetic for gitbulk's merge-readiness clock.

A business day is Monday through Friday. No holiday awareness — see
this.i node gmw3npk7 for the API conventions and bg4pqn7m for the
underlying merge-age policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Python's date.weekday(): Monday=0 ... Sunday=6
_WEEKEND_DAYS = {5, 6}


def is_business_day(when: datetime) -> bool:
    return when.weekday() not in _WEEKEND_DAYS


def add_business_days(start: datetime, n: int) -> datetime:
    """Advance `start` by `n` business days, preserving time-of-day.

    Semantics:
      - n == 0 → returns `start` unchanged (identity, even if weekend).
      - n < 0  → raises ValueError; gitbulk never subtracts business days.
      - If `start` is itself a weekend, the count begins from the next
        business day: add_business_days(Saturday 17:00, 1) == Monday 17:00.
      - Time-of-day is preserved across the advance.
      - tzinfo (or its absence) on `start` is preserved; this module
        does not normalize timezone.
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if n == 0:
        return start
    current = start
    days_advanced = 0
    while days_advanced < n:
        current = current + timedelta(days=1)
        if is_business_day(current):
            days_advanced += 1
    return current
