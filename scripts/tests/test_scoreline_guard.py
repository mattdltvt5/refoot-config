"""Scoreline guard: a highlight whose title states a score that contradicts the
match's final result is a different game (archive "Classic", women's / academy
side, earlier meeting) and must be rejected — but ONLY on a contradiction.

Covers extract_title_scorelines / acceptable_scores / scoreline_contradicts,
the search_playlist wiring (every tier goes through it), the existing-record
re-check and the tournament video_id graft clearing a removed video.
"""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

import highlights_common as H
from highlights_common import (
    acceptable_scores,
    apply_existing_record_guards,
    extract_title_scorelines,
    is_gameweek_complete,
    scoreline_contradicts,
    search_playlist,
)
from fixture_providers import _apisports_score, _apisports_status


def _fix(home, away, status="FINISHED", **score):
    """Fixture with a simple football-data score (league / UCL group match)."""
    s = {"fullTime": {"home": home, "away": away}}
    s.update(score)
    return {"match_id": 1, "status": status, "score": s}


@pytest.fixture(autouse=True)
def _clear_rejections():
    H.GUARD_REJECTIONS.clear()
    yield
    H.GUARD_REJECTIONS.clear()


# ── The four wrong highlights found in the cache ──────────────────────────────

@pytest.mark.parametrize("title, home, away", [
    # PL GW9 Spurs v Palace, finished 2-0 (Palace uploads, 2025 archive)
    ("Stunning Sarr Brace 😍| Spurs 1-3 Crystal Palace | Classic Premier League Highlights", 2, 0),
    # PL GW4 Liverpool v Fulham, finished 0-0 (archive "Classic")
    ("Classic Highlights: Liverpool 4-0 Fulham | Luis Suarez Double!", 0, 0),
    # LaLiga GW9 Valencia v Athletic, finished 0-0 (Liga F women's game)
    ("Highlights | Athletic Club 1-0 Valencia CF | Liga F 2026/27 J7", 0, 0),
    # UCL MD1 Man Utd v Sabah, finished 4-0 (academy side)
    ("Man Utd 5-0 Sabah | Academy Highlights", 4, 0),
])
def test_real_wrong_titles_contradict(title, home, away):
    assert scoreline_contradicts(title, _fix(home, away)) is True


# ── Correct titles pass ───────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Spurs 2-0 Crystal Palace | Premier League Highlights",
    "Crystal Palace 0-2 Spurs | Premier League Highlights",   # away side first
    "HIGHLIGHTS | Tottenham 2 – 0 Palace",
    "Spurs 2 - 0 Palace",
])
def test_correct_titles_in_both_orders_pass(title):
    assert scoreline_contradicts(title, _fix(2, 0)) is False


def test_title_without_scoreline_passes():
    assert scoreline_contradicts("Spurs v Crystal Palace | Highlights", _fix(2, 0)) is False


def test_not_finished_never_rejects():
    assert scoreline_contradicts("Spurs 1-3 Palace", _fix(2, 0, status="IN_PLAY")) is False
    assert acceptable_scores(_fix(2, 0, status="TIMED")) is None


def test_no_score_never_rejects():
    assert scoreline_contradicts("Spurs 1-3 Palace", _fix(None, None)) is False
    assert scoreline_contradicts("Spurs 1-3 Palace", {"status": "FINISHED"}) is False
    assert acceptable_scores({"status": "FINISHED", "score": {}}) is None


# ── Parsing bounds ────────────────────────────────────────────────────────────

def test_liga_f_season_range_is_not_a_scoreline():
    assert extract_title_scorelines(
        "Highlights | Athletic Club 1-0 Valencia CF | Liga F 2026/27 J7"
    ) == [(1, 0)]
    assert extract_title_scorelines("Valencia CF vs Athletic | Liga F 2026/27") == []
    assert extract_title_scorelines("LaLiga 2025-26 | Jornada 9") == []
    assert extract_title_scorelines("Premier League 25/26 highlights") == []


def test_penalty_shootout_tally_ignored():
    title = "Inter 1-1 Milan (4-2 pens)"
    assert extract_title_scorelines(title) == [(1, 1)]
    assert scoreline_contradicts(title, _fix(1, 1)) is False


@pytest.mark.parametrize("title", [
    "Inter 1-1 Milan (4-2 on penalties)",
    "Inter 1-1 Milan [4-2 p.]",
    "Inter 1-1 Milan 4-2 pens",
    "Inter 1-1 Milan (4-2 dcr)",
    "Bayern 1:1 Real (4:2 i.E.)",
    "PSG 1-1 Lyon (4-2 t.a.b.)",
    "Betis 1-1 Sevilla (4-2 penaltis)",
])
def test_penalty_markers_in_several_languages(title):
    assert extract_title_scorelines(title) == [(1, 1)]


def test_aggregate_only_match_score_checked():
    title = "Real Madrid 3-1 Bayern (4-3 agg)"
    assert extract_title_scorelines(title) == [(3, 1)]
    assert scoreline_contradicts(title, _fix(3, 1)) is False
    assert scoreline_contradicts(title, _fix(4, 3)) is True
    assert extract_title_scorelines("Real Madrid 3-1 Bayern | agg. 4-3") == [(3, 1)]
    assert extract_title_scorelines("Real Madrid 3-1 Bayern, 4-3 on aggregate") == [(3, 1)]


def test_kick_off_time_is_not_a_scoreline():
    assert extract_title_scorelines("Spurs v Palace | Kick-off 17:30") == []
    assert extract_title_scorelines("Spurs v Palace 3:00 pm") == []
    assert extract_title_scorelines("Spurs v Palace 8:00 CET") == []
    assert extract_title_scorelines("Spurs v Palace 15:00h") == []
    # Only the suffix marks these as clock times.
    assert extract_title_scorelines("Spurs v Palace 8:15 pm") == []
    assert extract_title_scorelines("Spurs v Palace 9:15 UTC") == []


@pytest.mark.parametrize("title", [
    # Real Bundesliga titles for correct videos (finals 2-3, 6-3, 2-2).
    "From 3-0 To Chaos: Augsburg Strike Back | FC AUGSBURG - FC BAYERN | Highlights",
    "LEVERKUSEN Win After 1-3 Down! | BAYER LEVERKUSEN - VFL WOLFSBURG | Highlights",
    "Cologne Come Back From 0-2! | EINTRACHT FRANKFURT - 1. FC KÖLN | Highlights",
])
def test_comeback_intermediate_scores_skipped(title):
    assert extract_title_scorelines(title) == []


def test_colon_scoreline_reads():
    assert extract_title_scorelines("Bayern 2:0 Dortmund") == [(2, 0)]
    assert scoreline_contradicts("Bayern 2:0 Dortmund", _fix(2, 0)) is False


@pytest.mark.parametrize("title", [
    "Highlights 31-10-2026",          # date
    "Matchday 1 - 2 goals",           # counter
    "Top 10 - 3 games",               # counter
    "Season 10-11 classic",           # season
    "Spurs 100-0 Palace",             # 3+ digits
    "Spurs 16-0 Palace",              # above the goal bound
    "Spurs 02-00 Palace",             # zero-padded
])
def test_non_scorelines_skipped(title):
    assert extract_title_scorelines(title) == []


# ── Score field semantics ─────────────────────────────────────────────────────

def test_extra_time_both_90_minute_and_aet_scores_pass():
    # football-data knockout decided in extra time: 1-1 at 90', 2-1 a.e.t.
    fix = _fix(2, 1,
               duration="EXTRA_TIME",
               regularTime={"home": 1, "away": 1},
               extraTime={"home": 1, "away": 0})
    assert scoreline_contradicts("France 1-1 Spain", fix) is False
    assert scoreline_contradicts("France 2-1 Spain (aet)", fix) is False
    assert scoreline_contradicts("France 3-1 Spain", fix) is True


def test_football_data_shootout_full_time_includes_penalties():
    # football-data stores fullTime = 1-1 + pens 3-4 = 4-5 for a shootout.
    fix = _fix(4, 5,
               duration="PENALTY_SHOOTOUT",
               regularTime={"home": 1, "away": 1},
               extraTime={"home": 0, "away": 0},
               penalties={"home": 3, "away": 4})
    assert acceptable_scores(fix) == {(1, 1)}
    assert scoreline_contradicts("Spain 1-1 Italy (3-4 pens)", fix) is False
    assert scoreline_contradicts("Spain 1-1 Italy", fix) is False
    # The shootout tally is never accepted as the match score.
    assert scoreline_contradicts("Spain 3-4 Italy", fix) is True


def test_api_sports_shootout_full_time_excludes_penalties():
    # Copa (API-Sports, cached): fullTime 1-1 with penalties 4-2, no duration.
    fix = _fix(1, 1, penalties={"home": 4, "away": 2})
    assert scoreline_contradicts("Argentina 1-1 Ecuador", fix) is False


def test_apisports_score_normalised_to_football_data_shape():
    raw = {
        "fixture": {"status": {"short": "PEN"}},
        "goals": {"home": 1, "away": 1},
        "score": {
            "fulltime":  {"home": 1, "away": 1},
            "extratime": {"home": 0, "away": 0},
            "penalty":   {"home": 4, "away": 2},
        },
    }
    fix = {"status": _apisports_status(raw), "score": _apisports_score(raw)}
    assert fix["status"] == "FINISHED"
    assert scoreline_contradicts("Argentina 1-1 Ecuador (4-2 pens)", fix) is False
    assert scoreline_contradicts("Argentina 4-2 Ecuador", fix) is True


# ── search_playlist wiring (all tiers) ────────────────────────────────────────

class _Quota:
    def increment(self, cap):
        pass


def _search(items, fixture):
    resp = MagicMock(status_code=200, ok=True)
    resp.json.return_value = {"items": items, "nextPageToken": None}
    with patch("highlights_common.requests.get", return_value=resp):
        return search_playlist("PLx", "key", fixture, "Premier League", _Quota(), 10**6)


def _item(video_id, title, date="2026-10-31"):
    return {"snippet": {"title": title, "publishedAt": f"{date}T12:00:00Z",
                        "resourceId": {"videoId": video_id}}}


_SPURS_PALACE = {
    "match_id": 560627, "date": "2026-10-31",
    "home_team": "Tottenham Hotspur FC", "home_short": "Tottenham", "home_tla": "TOT",
    "away_team": "Crystal Palace FC", "away_short": "Crystal Palace", "away_tla": "CRY",
    "status": "FINISHED", "score": {"fullTime": {"home": 2, "away": 0}},
}


def test_search_playlist_rejects_contradicting_candidate_and_logs_it():
    got = _search([
        _item("VG1bL1gqnnI", "Spurs 1-3 Crystal Palace | Classic Premier League Highlights"),
        _item("GOOD", "Spurs 2-0 Crystal Palace | Premier League Highlights"),
    ], _SPURS_PALACE)
    assert [v["video_id"] for v in got] == ["GOOD"]
    assert len(H.GUARD_REJECTIONS) == 1
    rej = H.GUARD_REJECTIONS[0]
    assert rej["reason"] == "scoreline_mismatch"
    assert rej["match_id"] == 560627 and rej["video_id"] == "VG1bL1gqnnI"
    assert rej["title_scorelines"] == [[1, 3]]
    assert rej["cached_score"] == {"home": 2, "away": 0}


def test_search_playlist_without_score_is_unchanged():
    fixture = {k: v for k, v in _SPURS_PALACE.items() if k not in ("status", "score")}
    got = _search([_item("X", "Spurs 1-3 Crystal Palace | Highlights")], fixture)
    assert [v["video_id"] for v in got] == ["X"]
    assert H.GUARD_REJECTIONS == []


# ── Existing-record re-check ──────────────────────────────────────────────────

def test_existing_record_guard_removes_and_reopens_match():
    existing = {"matches": [
        {"match_id": 560627, "videos": [
            {"video_id": "VG1bL1gqnnI", "title": "Spurs 1-3 Crystal Palace | Classic Highlights"},
        ]},
        {"match_id": 2, "videos": [
            {"video_id": "OK", "title": "Arsenal 1-0 Chelsea | Highlights"},
        ]},
    ]}
    fixtures = [_SPURS_PALACE, {"match_id": 2, "status": "FINISHED",
                                "score": {"fullTime": {"home": 1, "away": 0}}}]
    assert is_gameweek_complete(existing, fixtures) is True

    removed = apply_existing_record_guards(existing, fixtures)

    assert [r["video_id"] for r in removed] == ["VG1bL1gqnnI"]
    assert removed[0]["action"] == "removed_cached"
    assert existing["matches"][0]["videos"] == []
    assert existing["matches"][1]["videos"][0]["video_id"] == "OK"
    # Emptied match is no longer "covered", so the normal flow re-searches it.
    assert is_gameweek_complete(existing, fixtures) is False


def test_existing_record_guard_is_noop_without_contradiction():
    existing = {"matches": [{"match_id": 560627, "videos": [
        {"video_id": "A", "title": "Spurs v Palace | Highlights"},
        {"video_id": "B", "title": "Spurs 2-0 Crystal Palace"},
    ]}]}
    before = json.dumps(existing)
    assert apply_existing_record_guards(existing, [_SPURS_PALACE]) == []
    assert apply_existing_record_guards(None, [_SPURS_PALACE]) == []
    assert json.dumps(existing) == before


# ── Tournament video_id graft drops a removed video ───────────────────────────

def test_graft_clears_video_id_when_highlights_record_emptied():
    from sync_tournaments import graft_video_ids
    from highlights_common import season_for_competition

    season = season_for_competition("Champions League")
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "tournament-groups"))
        tg_path = os.path.join(tmp, "tournament-groups", "ucl.json")
        with open(tg_path, "w", encoding="utf-8") as f:
            json.dump({"matches": [], "groupMatches": [
                {"match_id": 575338, "matchday": 1, "video_id": "RQ5cGGjVzR0"},
                {"match_id": 999, "matchday": 1, "video_id": "KEEP"},  # not in highlights
            ]}, f)
        hl_dir = os.path.join(tmp, "highlights", "ucl", str(season))
        os.makedirs(hl_dir)
        with open(os.path.join(hl_dir, "matchday-1.json"), "w", encoding="utf-8") as f:
            json.dump({"matches": [{"match_id": 575338, "videos": []}]}, f)

        graft_video_ids(out_dir=tmp)

        with open(tg_path, encoding="utf-8") as f:
            gms = {g["match_id"]: g for g in json.load(f)["groupMatches"]}
        assert gms[575338]["video_id"] is None
        assert gms[999]["video_id"] == "KEEP"
