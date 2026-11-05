"""A status football-data.org doesn't document is unreadable data, not a result.

2026-11-04: Rayo Vallecano v Athletic Club (564698) - abandoned at 0-0 on
10 Oct, re-scheduled for 9 Dec - arrived with status "2026-10-10 12:00:00Z"
(later "2026-12-09 19:00:00Z") and its old 0-0, which the app showed as a
played 0-0. The pipeline publishes such a record as SCHEDULED with no score.
"""
from fixture_providers import (
    FD_STATUSES,
    FootballDataProvider,
    merge_preserve_finished,
    sanitize_status,
)
from sync_tournaments import build_group_matches, build_tournament_data


def _fd_match(status, home=0, away=0, mid=564698, **extra):
    return {
        "id": mid, "matchday": 9, "utcDate": "2026-12-09T19:00:00Z", "status": status,
        "homeTeam": {"id": 87, "name": "Rayo Vallecano de Madrid"},
        "awayTeam": {"id": 77, "name": "Athletic Club"},
        "score": {"fullTime": {"home": home, "away": away}},
        "lastUpdated": "2026-11-04T09:14:19Z",
        **extra,
    }


def test_unknown_status_becomes_scheduled_without_a_score():
    for bad in ("2026-10-10 12:00:00Z", "", None, "LIVE", "finished"):
        assert sanitize_status(bad, {"home": 0, "away": 0}) == (
            "SCHEDULED", {"home": None, "away": None})


def test_documented_statuses_pass_unchanged_with_their_score():
    for ok in FD_STATUSES:
        assert sanitize_status(ok, {"home": 2, "away": 1}) == (ok, {"home": 2, "away": 1})


def test_league_fixtures_artifact_sanitizes():
    out = FootballDataProvider.__new__(FootballDataProvider)._normalize_artifact(
        [_fd_match("2026-10-10 12:00:00Z"), _fd_match("FINISHED", 2, 1, mid=1)],
        "LaLiga", 2026)
    by_id = {r["match_id"]: r for r in out}
    assert by_id[564698]["status"] == "SCHEDULED"
    assert by_id[564698]["score"]["fullTime"] == {"home": None, "away": None}
    assert by_id[1]["status"] == "FINISHED"
    assert by_id[1]["score"]["fullTime"] == {"home": 2, "away": 1}


def test_a_cached_phantom_is_replaced_by_the_sanitized_record():
    cached = FootballDataProvider.__new__(FootballDataProvider)._normalize_artifact(
        [_fd_match("CANCELLED")], "LaLiga", 2026)[0]
    cached = {**cached, "status": "2026-10-10 12:00:00Z"}  # what is on disk today
    incoming = FootballDataProvider.__new__(FootballDataProvider)._normalize_artifact(
        [_fd_match("2026-10-10 12:00:00Z")], "LaLiga", 2026)[0]
    merged = merge_preserve_finished(cached, incoming)
    assert merged["status"] == "SCHEDULED"
    assert merged["score"]["fullTime"] == {"home": None, "away": None}


def test_tournament_group_matches_and_knockouts_sanitize():
    gm = _fd_match("2026-10-10 12:00:00Z", stage="LEAGUE_STAGE", group=None)
    ko = _fd_match("1999-01-01", mid=7, stage="LAST_16", group=None)
    groups = build_group_matches({"matches": [gm]})
    assert groups[0]["status"] == "SCHEDULED"
    assert groups[0]["score"]["fullTime"] == {"home": None, "away": None}
    data = build_tournament_data("ucl", {}, {"matches": [gm, ko]})
    knock = data["matches"][0]
    assert knock["status"] == "SCHEDULED"
    assert knock["score"]["fullTime"] == {"home": None, "away": None}
    # A normal knockout passes through verbatim.
    fine = build_tournament_data("ucl", {}, {"matches": [
        _fd_match("FINISHED", 3, 1, mid=8, stage="LAST_16", group=None)]})["matches"][0]
    assert fine["status"] == "FINISHED" and fine["score"]["fullTime"] == {"home": 3, "away": 1}
