"""Tests for util/businessdays.py (this.i node gmw3npk7)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from gitbulk.util import businessdays as bd


# Anchor dates we use repeatedly. May 2026:
#   M  T  W  Th F  Sa Su
#                  1  2  3
#   4  5  6  7  8  9  10
#  11 12 13 14 15 16 17
#  18 19 20 21 22 23 24
#  25 26 27 28 29 30 31
MONDAY     = datetime(2026, 5, 4,  12, 0, 0, tzinfo=timezone.utc)
TUESDAY    = datetime(2026, 5, 5,  12, 0, 0, tzinfo=timezone.utc)
WEDNESDAY  = datetime(2026, 5, 6,  12, 0, 0, tzinfo=timezone.utc)
THURSDAY   = datetime(2026, 5, 7,  12, 0, 0, tzinfo=timezone.utc)
FRIDAY     = datetime(2026, 5, 8,  12, 0, 0, tzinfo=timezone.utc)
SATURDAY   = datetime(2026, 5, 9,  12, 0, 0, tzinfo=timezone.utc)
SUNDAY     = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


# ─── is_business_day ────────────────────────────────────────────────────────


@pytest.mark.parametrize("dt", [MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY])
def test_is_business_day_true_for_weekdays(dt):
    assert bd.is_business_day(dt) is True


@pytest.mark.parametrize("dt", [SATURDAY, SUNDAY])
def test_is_business_day_false_for_weekends(dt):
    assert bd.is_business_day(dt) is False


# ─── add_business_days: identity and rejection ─────────────────────────────


def test_add_zero_is_identity_on_weekday():
    assert bd.add_business_days(WEDNESDAY, 0) == WEDNESDAY


def test_add_zero_is_identity_on_weekend():
    # n=0 returns start unchanged even if start is a weekend
    assert bd.add_business_days(SATURDAY, 0) == SATURDAY


def test_negative_n_raises():
    with pytest.raises(ValueError, match="n must be >= 0"):
        bd.add_business_days(WEDNESDAY, -1)


# ─── add_business_days: simple weekday advances ────────────────────────────


def test_monday_plus_one_is_tuesday():
    assert bd.add_business_days(MONDAY, 1) == TUESDAY


def test_monday_plus_four_is_friday():
    assert bd.add_business_days(MONDAY, 4) == FRIDAY


def test_monday_plus_five_skips_weekend_to_next_monday():
    next_monday = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    assert bd.add_business_days(MONDAY, 5) == next_monday


# ─── add_business_days: weekend wrap behavior ──────────────────────────────


def test_friday_plus_one_is_monday():
    next_monday = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    assert bd.add_business_days(FRIDAY, 1) == next_monday


def test_friday_plus_three_is_wednesday():
    next_wednesday = datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc)
    assert bd.add_business_days(FRIDAY, 3) == next_wednesday


def test_saturday_plus_one_is_monday():
    next_monday = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    assert bd.add_business_days(SATURDAY, 1) == next_monday


def test_sunday_plus_one_is_monday():
    next_monday = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)
    assert bd.add_business_days(SUNDAY, 1) == next_monday


# ─── add_business_days: time-of-day preservation ───────────────────────────


def test_preserves_time_of_day():
    start = datetime(2026, 5, 8, 17, 30, 45, 123456, tzinfo=timezone.utc)  # Fri 17:30:45.123456
    expected = datetime(2026, 5, 11, 17, 30, 45, 123456, tzinfo=timezone.utc)  # Mon
    assert bd.add_business_days(start, 1) == expected


def test_preserves_tzinfo():
    pst = timezone(timedelta(hours=-8))
    start = datetime(2026, 5, 8, 9, 0, 0, tzinfo=pst)  # Friday in PST
    result = bd.add_business_days(start, 1)
    assert result.tzinfo == pst
    assert result == datetime(2026, 5, 11, 9, 0, 0, tzinfo=pst)  # Monday in PST


def test_naive_datetime_works_unchanged():
    # Module is TZ-agnostic per (e) in node gmw3npk7 — naive dt is passed through.
    naive = datetime(2026, 5, 8, 12, 0, 0)  # Friday, no tzinfo
    result = bd.add_business_days(naive, 1)
    assert result == datetime(2026, 5, 11, 12, 0, 0)  # Monday, naive
    assert result.tzinfo is None


# ─── add_business_days: boundary crossings ─────────────────────────────────


def test_crosses_month_boundary():
    # Wednesday May 27 + 3 business days = Monday June 1
    start = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)
    expected = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert bd.add_business_days(start, 3) == expected


def test_crosses_year_boundary():
    # Wednesday Dec 30 2026 + 3 business days = Monday Jan 4 2027
    start = datetime(2026, 12, 30, 12, 0, 0, tzinfo=timezone.utc)
    expected = datetime(2027, 1, 4, 12, 0, 0, tzinfo=timezone.utc)
    assert bd.add_business_days(start, 3) == expected


def test_large_n_skips_two_weekends():
    # Monday + 10 business days = Monday two weeks later
    expected = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc)
    assert bd.add_business_days(MONDAY, 10) == expected
