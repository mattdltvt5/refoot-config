"""QuotaTracker resets on YouTube's quota day (midnight US Pacific), not UTC.

Pins the 07:00/08:00 UTC boundary, both DST switches, and the migration from
tracker files written with a UTC date.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import highlights_common as hc  # noqa: E402
from highlights_common import (  # noqa: E402
    QUOTA_DAY_BASIS, QuotaTracker, pacific_utc_offset, youtube_quota_day,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ── Pacific day ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("instant, day", [
    # Winter (PST, -8): the day turns at 08:00 UTC.
    (utc(2026, 11, 22, 3, 56), "2026-11-21"),   # the 403 run on 2026-11-22
    (utc(2026, 11, 22, 7, 59, 59), "2026-11-21"),
    (utc(2026, 11, 22, 8, 0), "2026-11-22"),
    (utc(2026, 1, 1, 7, 59), "2025-12-31"),     # year boundary
    (utc(2026, 1, 1, 8, 0), "2026-01-01"),
    # Summer (PDT, -7): the day turns at 07:00 UTC.
    (utc(2026, 7, 4, 6, 59, 59), "2026-07-03"),
    (utc(2026, 7, 4, 7, 0), "2026-07-04"),
])
def test_quota_day_turns_at_pacific_midnight(instant, day):
    assert youtube_quota_day(instant) == day


@pytest.mark.parametrize("instant, hours", [
    # 2026: DST from Sun 8 Mar 10:00 UTC to Sun 1 Nov 09:00 UTC.
    (utc(2026, 3, 8, 9, 59, 59), -8),
    (utc(2026, 3, 8, 10, 0), -7),
    (utc(2026, 11, 1, 8, 59, 59), -7),
    (utc(2026, 11, 1, 9, 0), -8),
    # 2027: Sun 14 Mar / Sun 7 Nov.
    (utc(2027, 3, 14, 9, 59), -8),
    (utc(2027, 3, 14, 10, 0), -7),
    (utc(2027, 11, 7, 8, 59), -7),
    (utc(2027, 11, 7, 9, 0), -8),
])
def test_dst_switches(instant, hours):
    assert pacific_utc_offset(instant) == timedelta(hours=hours)


def test_dst_transition_days_still_turn_at_local_midnight():
    # Spring-forward day: midnight is still PST (08:00 UTC).
    assert youtube_quota_day(utc(2026, 3, 8, 7, 59)) == "2026-03-07"
    assert youtube_quota_day(utc(2026, 3, 8, 8, 0)) == "2026-03-08"
    # Day after: midnight is PDT (07:00 UTC).
    assert youtube_quota_day(utc(2026, 3, 9, 6, 59)) == "2026-03-08"
    assert youtube_quota_day(utc(2026, 3, 9, 7, 0)) == "2026-03-09"
    # Fall-back day: midnight is still PDT (07:00 UTC); the next is PST.
    assert youtube_quota_day(utc(2026, 11, 1, 6, 59)) == "2026-10-31"
    assert youtube_quota_day(utc(2026, 11, 1, 7, 0)) == "2026-11-01"
    assert youtube_quota_day(utc(2026, 11, 2, 7, 59)) == "2026-11-01"
    assert youtube_quota_day(utc(2026, 11, 2, 8, 0)) == "2026-11-02"


def test_matches_tz_database_when_available():
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        la = zoneinfo.ZoneInfo("America/Los_Angeles")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no tz database on this machine")
    t = utc(2025, 1, 1)
    while t < utc(2029, 1, 1):
        assert youtube_quota_day(t) == t.astimezone(la).strftime("%Y-%m-%d"), t
        t += timedelta(minutes=97)  # walks across every hour of the day


# ── Tracker ───────────────────────────────────────────────────────────────────

@pytest.fixture
def tracker_file(tmp_path, monkeypatch):
    path = tmp_path / "quota-tracker.json"
    monkeypatch.setattr(hc, "QUOTA_TRACKER_PATH", path)
    return path


def write(path, **data):
    path.write_text(json.dumps(data), encoding="utf-8")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_new_file_records_pacific_day_and_basis(tracker_file):
    q = QuotaTracker(now=utc(2026, 11, 22, 3, 56))
    assert (q.date, q.units_used) == ("2026-11-21", 0)
    saved = read(tracker_file)
    assert saved["date"] == "2026-11-21"
    assert saved["day_basis"] == QUOTA_DAY_BASIS == "America/Los_Angeles"


def test_same_pacific_day_keeps_units_across_utc_midnight(tracker_file):
    write(tracker_file, date="2026-11-21", day_basis=QUOTA_DAY_BASIS,
          units_used=6100, last_updated="2026-11-21T23:55:00Z")
    q = QuotaTracker(now=utc(2026, 11, 22, 0, 5))  # 16:05 PST, same YouTube day
    assert q.units_used == 6100
    assert q.over_incremental_cap is False


def test_cap_holds_through_the_old_utc_reset(tracker_file):
    write(tracker_file, date="2026-11-21", day_basis=QUOTA_DAY_BASIS,
          units_used=8000, last_updated="2026-11-21T23:59:00Z")
    q = QuotaTracker(now=utc(2026, 11, 22, 4, 0))
    assert q.over_incremental_cap is True  # previously reset to 0 at 00:00 UTC


def test_resets_at_pacific_midnight(tracker_file):
    write(tracker_file, date="2026-11-21", day_basis=QUOTA_DAY_BASIS,
          units_used=8000, last_updated="2026-11-22T07:59:00Z")
    q = QuotaTracker(now=utc(2026, 11, 22, 8, 0))
    assert (q.date, q.units_used) == ("2026-11-22", 0)


def test_legacy_utc_file_written_this_pacific_day_keeps_units(tracker_file):
    # The real file at the 403: UTC date, written 03:56 UTC = 19:56 PST on 21st.
    write(tracker_file, date="2026-11-22", units_used=1940,
          last_updated="2026-11-22T03:56:07Z")
    q = QuotaTracker(now=utc(2026, 11, 22, 4, 0))
    assert (q.date, q.units_used) == ("2026-11-21", 1940)
    assert read(tracker_file)["day_basis"] == QUOTA_DAY_BASIS


def test_legacy_utc_file_from_previous_pacific_day_resets(tracker_file):
    # Same UTC date as "today", but last written before the Pacific reset.
    write(tracker_file, date="2026-11-22", units_used=1940,
          last_updated="2026-11-22T03:56:07Z")
    q = QuotaTracker(now=utc(2026, 11, 22, 9, 0))
    assert (q.date, q.units_used) == ("2026-11-22", 0)


@pytest.mark.parametrize("data", [
    {}, {"date": "2026-11-22", "units_used": 5},
    {"date": "2026-11-22", "units_used": 5, "last_updated": "garbage"},
])
def test_unreadable_legacy_file_resets(tracker_file, data):
    write(tracker_file, **data)
    q = QuotaTracker(now=utc(2026, 11, 22, 4, 0))
    assert q.units_used == 0


def test_naive_last_updated_is_read_as_utc(tracker_file):
    write(tracker_file, date="2026-11-22", units_used=12,
          last_updated="2026-11-22T03:00:00")
    assert QuotaTracker(now=utc(2026, 11, 22, 4, 0)).units_used == 12


def test_increment_persists_with_basis(tracker_file):
    q = QuotaTracker(now=utc(2026, 11, 22, 12, 0))
    q.increment(cap=10)
    saved = read(tracker_file)
    assert (saved["units_used"], saved["date"], saved["day_basis"]) == (
        1, "2026-11-22", QUOTA_DAY_BASIS)
