"""Grace-window guard: a just-finished fixture reads as 'awaiting', not 'missing'.

Covers _within_highlight_grace(), the pure predicate summary.json uses to tag a
not-yet-covered fixture as `pending` for HIGHLIGHT_GRACE_HOURS after estimated
full-time.
"""
from datetime import datetime, timezone, timedelta

from highlights_common import (
    _within_highlight_grace, HIGHLIGHT_GRACE_HOURS, EST_MATCH_DURATION_HOURS,
)

_NOW = datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)
_WINDOW = EST_MATCH_DURATION_HOURS + HIGHLIGHT_GRACE_HOURS  # 14h


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_recently_finished_is_pending():
    # kicked off 3h ago -> finished ~1h ago -> well within the window
    assert _within_highlight_grace(_iso(_NOW - timedelta(hours=3)), _NOW) is True


def test_long_finished_is_not_pending():
    # 20h since kickoff -> past the 14h window -> a real gap
    assert _within_highlight_grace(_iso(_NOW - timedelta(hours=20)), _NOW) is False


def test_window_boundary():
    assert _within_highlight_grace(_iso(_NOW - timedelta(hours=_WINDOW - 1)), _NOW) is True
    assert _within_highlight_grace(_iso(_NOW - timedelta(hours=_WINDOW + 1)), _NOW) is False


def test_bad_or_missing_kickoff_is_not_pending():
    for bad in ("", None, "not-a-date"):
        assert _within_highlight_grace(bad, _NOW) is False


def test_naive_kickoff_treated_as_utc():
    # ISO without 'Z' / offset must not raise and must compare as UTC
    naive = (_NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    assert _within_highlight_grace(naive, _NOW) is True
