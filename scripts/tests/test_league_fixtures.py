"""
Tests for the league fixtures artifact pipeline.

Covers:
  - _normalize_artifact() shape (GroupMatch-compatible)
  - _normalize() (highlights path) skips non-FINISHED statuses
  - No double-fetch: get_fixtures() + get_full_season() share one HTTP call
  - DOMESTIC_LEAGUE_COMPS membership
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from unittest.mock import MagicMock, patch

import pytest

from fixture_providers import FootballDataProvider
from highlights_common import DOMESTIC_LEAGUE_COMPS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_resp(matches: list) -> MagicMock:
    resp = MagicMock()
    resp.ok          = True
    resp.status_code = 200
    resp.json.return_value = {"matches": matches}
    return resp


def _make_match(
    match_id:   int,
    status:     str,
    home:       str = "Home FC",
    away:       str = "Away FC",
    matchday:   int = 1,
    ft_home:    int | None = None,
    ft_away:    int | None = None,
    home_crest: str = "https://crests.football-data.org/home.png",
    away_crest: str = "https://crests.football-data.org/away.png",
    stage:      str = "REGULAR_SEASON",
) -> dict:
    return {
        "id":       match_id,
        "status":   status,
        "utcDate":  "2024-10-05T14:00:00Z",
        "stage":    stage,
        "matchday": matchday,
        "homeTeam": {
            "id": 100, "name": home, "shortName": home,
            "tla": "HOM", "crest": home_crest,
        },
        "awayTeam": {
            "id": 200, "name": away, "shortName": away,
            "tla": "AWY", "crest": away_crest,
        },
        "score": {
            "fullTime": {"home": ft_home, "away": ft_away},
        },
    }


# ── _normalize_artifact shape tests ──────────────────────────────────────────

class TestNormalizeArtifact:
    """Verify the GroupMatch-compatible shape produced by _normalize_artifact."""

    def setup_method(self):
        self.provider = FootballDataProvider("dummy-key")

    def test_finished_match_has_score(self):
        matches = [_make_match(1, "FINISHED", ft_home=2, ft_away=1)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert len(result) == 1
        ft = result[0]["score"]["fullTime"]
        assert ft["home"] == 2
        assert ft["away"] == 1

    def test_scheduled_match_has_null_score(self):
        matches = [_make_match(2, "SCHEDULED")]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert len(result) == 1
        ft = result[0]["score"]["fullTime"]
        assert ft["home"] is None
        assert ft["away"] is None

    def test_in_play_match_score_passed_through(self):
        matches = [_make_match(3, "IN_PLAY", ft_home=1, ft_away=0)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert len(result) == 1
        ft = result[0]["score"]["fullTime"]
        assert ft["home"] == 1
        assert ft["away"] == 0

    def test_crest_url_passed_verbatim(self):
        crest_url = "https://crests.football-data.org/66.png"
        matches   = [_make_match(4, "FINISHED", ft_home=1, ft_away=0,
                                 home_crest=crest_url)]
        result    = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert result[0]["homeTeam"]["crest"] == crest_url

    def test_away_crest_url_passed_verbatim(self):
        crest_url = "https://crests.football-data.org/57.png"
        matches   = [_make_match(5, "FINISHED", ft_home=0, ft_away=1,
                                 away_crest=crest_url)]
        result    = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert result[0]["awayTeam"]["crest"] == crest_url

    def test_source_round_format(self):
        matches = [_make_match(6, "FINISHED", matchday=12, ft_home=1, ft_away=0)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert result[0]["sourceRound"] == "Gameweek 12"

    def test_group_is_empty_string(self):
        matches = [_make_match(7, "FINISHED", ft_home=1, ft_away=0)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert result[0]["group"] == ""

    def test_matchday_preserved(self):
        matches = [_make_match(8, "FINISHED", matchday=5, ft_home=3, ft_away=2)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert result[0]["matchday"] == 5

    def test_status_preserved(self):
        for status in ("FINISHED", "SCHEDULED", "IN_PLAY", "POSTPONED", "TIMED"):
            matches = [_make_match(9, status)]
            result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
            assert result[0]["status"] == status

    def test_utc_date_preserved(self):
        matches = [_make_match(10, "FINISHED", ft_home=1, ft_away=0)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert result[0]["utcDate"] == "2024-10-05T14:00:00Z"

    def test_home_team_fields_present(self):
        matches = [_make_match(11, "FINISHED", home="Arsenal", ft_home=2, ft_away=0)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        ht = result[0]["homeTeam"]
        assert set(ht.keys()) >= {"id", "name", "shortName", "tla", "crest"}
        assert ht["name"] == "Arsenal"

    def test_away_team_fields_present(self):
        matches = [_make_match(12, "FINISHED", away="Chelsea", ft_home=0, ft_away=1)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        at = result[0]["awayTeam"]
        assert set(at.keys()) >= {"id", "name", "shortName", "tla", "crest"}
        assert at["name"] == "Chelsea"

    def test_score_nested_structure(self):
        matches = [_make_match(13, "FINISHED", ft_home=1, ft_away=1)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2024)
        score = result[0]["score"]
        assert "fullTime" in score
        assert "home" in score["fullTime"]
        assert "away" in score["fullTime"]

    def test_skips_match_without_matchday(self):
        m = _make_match(14, "FINISHED", ft_home=1, ft_away=0)
        m["matchday"] = None
        result = self.provider._normalize_artifact([m], "Premier League", 2024)
        assert result == []

    def test_sorted_by_matchday_then_utcdate(self):
        m1 = _make_match(20, "FINISHED", matchday=2, ft_home=1, ft_away=0)
        m2 = _make_match(21, "FINISHED", matchday=1, ft_home=0, ft_away=1)
        m1["utcDate"] = "2024-10-12T15:00:00Z"
        m2["utcDate"] = "2024-10-05T15:00:00Z"
        result = self.provider._normalize_artifact([m1, m2], "Premier League", 2024)
        assert result[0]["matchday"] == 1
        assert result[1]["matchday"] == 2

    def test_mixed_statuses_all_included(self):
        matches = [
            _make_match(30, "FINISHED",  ft_home=1, ft_away=0),
            _make_match(31, "SCHEDULED"),
            _make_match(32, "IN_PLAY",   ft_home=1, ft_away=1),
            _make_match(33, "POSTPONED"),
            _make_match(34, "TIMED"),
        ]
        result = self.provider._normalize_artifact(matches, "Premier League", 2024)
        assert len(result) == 5

    def test_match_id_equals_fd_id(self):
        matches = [_make_match(537785, "FINISHED", ft_home=4, ft_away=2)]
        result  = self.provider._normalize_artifact(matches, "Premier League", 2025)
        assert result[0]["match_id"] == 537785

    def test_match_id_same_value_as_highlights_path(self):
        # Both _normalize_artifact and _normalize (highlights path) read m["id"] from
        # the same raw FD match dict — verify they produce the same integer for a
        # FINISHED match so the app's integer join is guaranteed to work.
        matches  = [_make_match(999, "FINISHED", ft_home=1, ft_away=0)]
        artifact = self.provider._normalize_artifact(matches, "Premier League", 2025)
        hl       = self.provider._normalize(matches, "Premier League")
        hl_ids   = [entry["match_id"] for entries in hl.values() for entry in entries]
        assert artifact[0]["match_id"] == hl_ids[0]

    def test_missing_id_emits_null_not_crash(self):
        m = _make_match(100, "FINISHED", ft_home=1, ft_away=0)
        del m["id"]
        result = self.provider._normalize_artifact([m], "Premier League", 2025)
        assert len(result) == 1
        assert result[0]["match_id"] is None


# ── _normalize (highlights path) tests ───────────────────────────────────────

class TestNormalizeHighlights:
    """_normalize only keeps FINISHED matches for highlights matching."""

    def setup_method(self):
        self.provider = FootballDataProvider("dummy-key")

    def test_finished_match_included(self):
        matches = [_make_match(40, "FINISHED", ft_home=1, ft_away=0)]
        result  = self.provider._normalize(matches, "Premier League")
        total   = sum(len(v) for v in result.values())
        assert total == 1

    def test_scheduled_match_excluded(self):
        matches = [_make_match(41, "SCHEDULED")]
        result  = self.provider._normalize(matches, "Premier League")
        assert result == {}

    def test_in_play_match_excluded(self):
        matches = [_make_match(42, "IN_PLAY", ft_home=1, ft_away=0)]
        result  = self.provider._normalize(matches, "Premier League")
        assert result == {}

    def test_postponed_match_excluded(self):
        matches = [_make_match(43, "POSTPONED")]
        result  = self.provider._normalize(matches, "Premier League")
        assert result == {}

    def test_timed_match_excluded(self):
        matches = [_make_match(44, "TIMED")]
        result  = self.provider._normalize(matches, "Premier League")
        assert result == {}

    def test_only_finished_survive_mixed_input(self):
        matches = [
            _make_match(50, "FINISHED",  ft_home=2, ft_away=1),
            _make_match(51, "SCHEDULED"),
            _make_match(52, "IN_PLAY",   ft_home=0, ft_away=0),
            _make_match(53, "FINISHED",  matchday=1, ft_home=1, ft_away=0),
        ]
        result = self.provider._normalize(matches, "Premier League")
        total  = sum(len(v) for v in result.values())
        assert total == 2


# ── No-double-fetch (cache) tests ─────────────────────────────────────────────

class TestRawCache:
    """get_fixtures() and get_full_season() must share one HTTP call."""

    def test_no_double_fetch_on_shared_comp_season(self):
        matches = [_make_match(60, "FINISHED", ft_home=1, ft_away=0)]
        mock_resp = _make_resp(matches)

        provider = FootballDataProvider("dummy-key")
        with patch("fixture_providers.fd_get", return_value=mock_resp) as mock_fd:
            provider.get_fixtures("PL", "Premier League", 2024)
            provider.get_full_season("PL", "Premier League", 2024)
            assert mock_fd.call_count == 1, (
                "Expected exactly one FD HTTP call; _raw_cache should reuse the first response"
            )

    def test_separate_comps_each_get_own_request(self):
        matches = [_make_match(70, "FINISHED", ft_home=1, ft_away=0)]
        mock_resp = _make_resp(matches)

        provider = FootballDataProvider("dummy-key")
        with patch("fixture_providers.fd_get", return_value=mock_resp) as mock_fd:
            provider.get_fixtures("PL", "Premier League",  2024)
            provider.get_fixtures("PD", "LaLiga",          2024)
            assert mock_fd.call_count == 2

    def test_same_comp_different_seasons_each_get_own_request(self):
        matches   = [_make_match(80, "FINISHED", ft_home=1, ft_away=0)]
        mock_resp = _make_resp(matches)

        provider = FootballDataProvider("dummy-key")
        with patch("fixture_providers.fd_get", return_value=mock_resp) as mock_fd:
            provider.get_fixtures("PL", "Premier League", 2023)
            provider.get_fixtures("PL", "Premier League", 2024)
            assert mock_fd.call_count == 2

    def test_cache_survives_full_season_call_first(self):
        matches   = [_make_match(90, "FINISHED", ft_home=1, ft_away=0)]
        mock_resp = _make_resp(matches)

        provider = FootballDataProvider("dummy-key")
        with patch("fixture_providers.fd_get", return_value=mock_resp) as mock_fd:
            provider.get_full_season("PL", "Premier League", 2024)
            provider.get_fixtures("PL",   "Premier League", 2024)
            assert mock_fd.call_count == 1


# ── DOMESTIC_LEAGUE_COMPS membership ─────────────────────────────────────────

class TestDomesticLeagueComps:
    def test_contains_exactly_five_leagues(self):
        assert len(DOMESTIC_LEAGUE_COMPS) == 5

    def test_contains_all_five_leagues(self):
        expected = {"Premier League", "LaLiga", "Serie A", "Bundesliga", "Ligue 1"}
        assert DOMESTIC_LEAGUE_COMPS == expected

    def test_excludes_ucl(self):
        assert "Champions League" not in DOMESTIC_LEAGUE_COMPS

    def test_excludes_uel(self):
        assert "Europa League" not in DOMESTIC_LEAGUE_COMPS

    def test_excludes_world_cup(self):
        assert "World Cup" not in DOMESTIC_LEAGUE_COMPS

    def test_excludes_euro_cup(self):
        assert "Euro Cup" not in DOMESTIC_LEAGUE_COMPS

    def test_excludes_copa_america(self):
        assert "Copa America" not in DOMESTIC_LEAGUE_COMPS
