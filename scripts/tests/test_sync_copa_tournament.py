"""Tests for scripts/sync_copa_tournament.py.

All HTTP is mocked — no live API-Sports calls are made.
Tests cover pure normalisation (normalize_standings, normalize_knockout)
and the file-writing contract (write_tournament).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Set the API key env var before any import that touches ApiSportsProvider.__init__.
os.environ.setdefault("APISPORTS_API_KEY", "test_key_copa_fixture")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync_copa_tournament import (
    KNOCKOUT_STAGE_MAP,
    SLUG,
    _build_team_group_map,
    _lookup_copa_video_id,
    _parse_matchday,
    normalize_group,
    normalize_knockout,
    normalize_standings,
    write_tournament,
)


# ── Sample API-Sports response bodies ─────────────────────────────────────────

def _standings_body():
    """Realistic API-Sports /standings response for Copa 2024 — two groups."""
    return {
        "response": [{
            "league": {
                "standings": [
                    [  # Group A
                        {
                            "rank": 1,
                            "team": {
                                "id":   26,
                                "name": "Argentina",
                                "logo": "https://media.api-sports.io/football/teams/26.png",
                            },
                            "points":    9,
                            "goalsDiff": 6,
                            "group":     "Group A",
                            "form":      "WWW",
                            "all": {
                                "played": 3, "win": 3, "draw": 0, "lose": 0,
                                "goals": {"for": 7, "against": 1},
                            },
                        },
                        {
                            "rank": 2,
                            "team": {
                                "id":   5529,
                                "name": "Canada",
                                "logo": "https://media.api-sports.io/football/teams/5529.png",
                            },
                            "points":    6,
                            "goalsDiff": 1,
                            "group":     "Group A",
                            "form":      "WWL",
                            "all": {
                                "played": 3, "win": 2, "draw": 0, "lose": 1,
                                "goals": {"for": 2, "against": 1},
                            },
                        },
                    ],
                    [  # Group B
                        {
                            "rank": 1,
                            "team": {
                                "id":   16,
                                "name": "Mexico",
                                "logo": "https://media.api-sports.io/football/teams/16.png",
                            },
                            "points":    4,
                            "goalsDiff": 1,
                            "group":     "Group B",
                            "form":      "WDL",
                            "all": {
                                "played": 3, "win": 1, "draw": 1, "lose": 1,
                                "goals": {"for": 3, "against": 2},
                            },
                        },
                    ],
                ]
            }
        }]
    }


def _fixtures_body():
    """API-Sports /fixtures response — group stage (skip), QF (pens), Final (no pens)."""
    return {
        "response": [
            {  # Group stage — must be skipped
                "fixture": {"id": 1001},
                "league":  {"round": "Group Stage - 1"},
                "teams": {
                    "home": {"id": 26,   "name": "Argentina",
                             "logo": "https://media.api-sports.io/football/teams/26.png"},
                    "away": {"id": 5529, "name": "Canada",
                             "logo": "https://media.api-sports.io/football/teams/5529.png"},
                },
                "score": {
                    "fulltime": {"home": 2, "away": 0},
                    "penalty":  {"home": None, "away": None},
                },
            },
            {  # Quarter-final with penalty shootout
                "fixture": {"id": 1010},
                "league":  {"round": "Quarter-finals"},
                "teams": {
                    "home": {"id": 26,   "name": "Argentina",
                             "logo": "https://media.api-sports.io/football/teams/26.png"},
                    "away": {"id": 2382, "name": "Ecuador",
                             "logo": "https://media.api-sports.io/football/teams/2382.png"},
                },
                "score": {
                    "fulltime": {"home": 1, "away": 1},
                    "penalty":  {"home": 4, "away": 2},
                },
            },
            {  # Final — no penalties
                "fixture": {"id": 1020},
                "league":  {"round": "Final"},
                "teams": {
                    "home": {"id": 26, "name": "Argentina",
                             "logo": "https://media.api-sports.io/football/teams/26.png"},
                    "away": {"id": 8,  "name": "Colombia",
                             "logo": "https://media.api-sports.io/football/teams/8.png"},
                },
                "score": {
                    "fulltime": {"home": 1, "away": 0},
                    "penalty":  {"home": None, "away": None},
                },
            },
        ]
    }


def _fixtures_body_with_groups():
    """Extended fixtures body: two group-stage matches + QF + Final."""
    base = _fixtures_body()
    extra_group = {
        "fixture": {"id": 1002, "status": {"short": "FT"}},
        "league":  {"round": "Group Stage - 2"},
        "teams": {
            "home": {"id": 16,   "name": "Mexico",
                     "logo": "https://media.api-sports.io/football/teams/16.png"},
            "away": {"id": 26,   "name": "Argentina",
                     "logo": "https://media.api-sports.io/football/teams/26.png"},
        },
        "score": {
            "fulltime": {"home": 0, "away": 2},
            "penalty":  {"home": None, "away": None},
        },
    }
    return {"response": [base["response"][0], extra_group] + base["response"][1:]}


# ── TestNormalizeStandings ────────────────────────────────────────────────────

class TestNormalizeStandings(unittest.TestCase):

    def test_returns_one_entry_per_group(self):
        groups = normalize_standings(_standings_body())
        self.assertEqual(len(groups), 2)

    def test_group_key_normalised_to_uppercase_underscore(self):
        groups = normalize_standings(_standings_body())
        self.assertEqual(groups[0]["group"], "GROUP_A")
        self.assertEqual(groups[1]["group"], "GROUP_B")

    def test_type_is_total(self):
        for g in normalize_standings(_standings_body()):
            self.assertEqual(g["type"], "TOTAL")

    def test_row_fields_match_GroupStanding_fromJson(self):
        row = normalize_standings(_standings_body())[0]["table"][0]  # Argentina rank 1

        self.assertEqual(row["position"],       1)
        self.assertEqual(row["team"]["id"],      26)
        self.assertEqual(row["team"]["name"],    "Argentina")
        self.assertEqual(row["team"]["tla"],     "")
        self.assertIn("26.png",                  row["team"]["crest"])
        self.assertIn("media.api-sports.io",     row["team"]["crest"])
        self.assertEqual(row["playedGames"],     3)
        self.assertEqual(row["won"],             3)
        self.assertEqual(row["draw"],            0)
        self.assertEqual(row["lost"],            0)
        self.assertEqual(row["goalsFor"],        7)
        self.assertEqual(row["goalsAgainst"],    1)
        self.assertEqual(row["goalDifference"],  6)
        self.assertEqual(row["points"],          9)
        self.assertEqual(row["form"],            "WWW")

    def test_empty_response_returns_empty_list(self):
        self.assertEqual(normalize_standings({}), [])
        self.assertEqual(normalize_standings({"response": []}), [])


# ── TestNormalizeKnockout ─────────────────────────────────────────────────────

class TestNormalizeKnockout(unittest.TestCase):

    def test_group_stage_rounds_are_skipped(self):
        matches = normalize_knockout(_fixtures_body())
        stages = {m["stage"] for m in matches}
        self.assertNotIn("GROUP_STAGE", stages,
                         "group-stage rounds must be filtered out")

    def test_returns_only_knockout_matches(self):
        matches = normalize_knockout(_fixtures_body())
        self.assertEqual(len(matches), 2,  # QF + Final; group stage skipped
                         "only QF and Final should survive the stage filter")

    def test_stage_labels_mapped_correctly(self):
        matches = normalize_knockout(_fixtures_body())
        self.assertEqual(matches[0]["stage"], "QUARTER_FINALS")
        self.assertEqual(matches[1]["stage"], "FINAL")

    def test_score_key_is_fullTime_camelCase(self):
        for m in normalize_knockout(_fixtures_body()):
            self.assertIn("fullTime",  m["score"],
                          "must use FD-compatible camelCase 'fullTime'")
            self.assertNotIn("fulltime", m["score"])

    def test_penalty_key_is_penalties_plural(self):
        for m in normalize_knockout(_fixtures_body()):
            self.assertIn("penalties", m["score"],
                          "must use FD-compatible plural 'penalties'")
            self.assertNotIn("penalty", m["score"])

    def test_penalty_shootout_preserved(self):
        matches = normalize_knockout(_fixtures_body())
        qf = next(m for m in matches if m["stage"] == "QUARTER_FINALS")
        self.assertEqual(qf["score"]["penalties"], {"home": 4, "away": 2})

    def test_no_penalty_is_null(self):
        matches = normalize_knockout(_fixtures_body())
        final = next(m for m in matches if m["stage"] == "FINAL")
        self.assertIsNone(final["score"]["penalties"])

    def test_crest_url_built_from_team_id(self):
        matches = normalize_knockout(_fixtures_body())
        qf = next(m for m in matches if m["stage"] == "QUARTER_FINALS")
        self.assertIn("26.png",              qf["homeTeam"]["crest"])
        self.assertIn("2382.png",            qf["awayTeam"]["crest"])
        self.assertIn("media.api-sports.io", qf["homeTeam"]["crest"])

    def test_team_names_present(self):
        matches = normalize_knockout(_fixtures_body())
        qf = next(m for m in matches if m["stage"] == "QUARTER_FINALS")
        self.assertEqual(qf["homeTeam"]["name"], "Argentina")
        self.assertEqual(qf["awayTeam"]["name"], "Ecuador")

    def test_empty_response_returns_empty_list(self):
        self.assertEqual(normalize_knockout({}), [])
        self.assertEqual(normalize_knockout({"response": []}), [])


# ── TestParseMatchday ─────────────────────────────────────────────────────────

class TestParseMatchday(unittest.TestCase):

    def test_standard_format(self):
        self.assertEqual(_parse_matchday("Group Stage - 1"), 1)
        self.assertEqual(_parse_matchday("Group Stage - 3"), 3)

    def test_no_space_around_dash(self):
        self.assertEqual(_parse_matchday("Group Stage-2"), 2)

    def test_case_insensitive(self):
        self.assertEqual(_parse_matchday("group stage - 1"), 1)

    def test_unknown_round_returns_none(self):
        self.assertIsNone(_parse_matchday("Quarter-finals"))
        self.assertIsNone(_parse_matchday(""))
        self.assertIsNone(_parse_matchday("Group A"))


# ── TestBuildTeamGroupMap ─────────────────────────────────────────────────────

class TestBuildTeamGroupMap(unittest.TestCase):

    def test_maps_team_ids_to_group_keys(self):
        groups  = normalize_standings(_standings_body())
        mapping = _build_team_group_map(groups)
        self.assertEqual(mapping[26],   "GROUP_A")   # Argentina
        self.assertEqual(mapping[5529], "GROUP_A")   # Canada
        self.assertEqual(mapping[16],   "GROUP_B")   # Mexico

    def test_empty_standings_returns_empty_map(self):
        self.assertEqual(_build_team_group_map([]), {})


# ── TestNormalizeGroup ────────────────────────────────────────────────────────

class TestNormalizeGroup(unittest.TestCase):

    def _map(self):
        return _build_team_group_map(normalize_standings(_standings_body()))

    def test_group_stage_matches_are_included(self):
        matches = normalize_group(_fixtures_body(), self._map())
        self.assertEqual(len(matches), 1,
                         "only the Group Stage - 1 fixture survives the filter")

    def test_knockout_rounds_are_excluded(self):
        matches = normalize_group(_fixtures_body(), self._map())
        stages = {m.get("stage") for m in matches}
        self.assertNotIn("QUARTER_FINALS", stages)
        self.assertNotIn("FINAL", stages)

    def test_normalized_integer_matchday(self):
        matches = normalize_group(_fixtures_body(), self._map())
        self.assertEqual(matches[0]["matchday"], 1)
        self.assertIsInstance(matches[0]["matchday"], int)

    def test_source_round_preserved_verbatim(self):
        matches = normalize_group(_fixtures_body(), self._map())
        self.assertEqual(matches[0]["sourceRound"], "Group Stage - 1")

    def test_group_derived_from_standings_map(self):
        matches = normalize_group(_fixtures_body(), self._map())
        self.assertEqual(matches[0]["group"], "GROUP_A")

    def test_scores_correct(self):
        matches = normalize_group(_fixtures_body(), self._map())
        m = matches[0]
        self.assertEqual(m["score"]["fullTime"]["home"], 2)
        self.assertEqual(m["score"]["fullTime"]["away"], 0)

    def test_team_fields_match_knockout_shape(self):
        matches = normalize_group(_fixtures_body(), self._map())
        m = matches[0]
        for team_key in ("homeTeam", "awayTeam"):
            for field in ("id", "name", "tla", "crest"):
                self.assertIn(field, m[team_key],
                              f"{team_key} missing field {field!r}")

    def test_crest_url_built_from_team_id(self):
        matches = normalize_group(_fixtures_body(), self._map())
        m = matches[0]
        self.assertIn("26.png",              m["homeTeam"]["crest"])
        self.assertIn("media.api-sports.io", m["homeTeam"]["crest"])

    def test_multiple_matchdays(self):
        matches = normalize_group(_fixtures_body_with_groups(), self._map())
        matchdays = {m["matchday"] for m in matches}
        self.assertIn(1, matchdays)
        self.assertIn(2, matchdays)

    def test_unknown_round_skipped_gracefully(self):
        body = {"response": [{
            "fixture": {"id": 9999, "status": {"short": "NS"}},
            "league":  {"round": "Something Unknown"},
            "teams": {
                "home": {"id": 26,   "name": "Argentina",
                         "logo": "https://media.api-sports.io/football/teams/26.png"},
                "away": {"id": 5529, "name": "Canada",
                         "logo": "https://media.api-sports.io/football/teams/5529.png"},
            },
            "score": {"fulltime": {"home": 0, "away": 0},
                      "penalty":  {"home": None, "away": None}},
        }]}
        self.assertEqual(normalize_group(body, self._map()), [])

    def test_empty_response_returns_empty_list(self):
        self.assertEqual(normalize_group({}, {}), [])
        self.assertEqual(normalize_group({"response": []}, {}), [])


# ── TestWriteTournament ───────────────────────────────────────────────────────

class TestWriteTournament(unittest.TestCase):

    def _run(self):
        groups        = normalize_standings(_standings_body())
        matches       = normalize_knockout(_fixtures_body())
        team_group_map = _build_team_group_map(groups)
        group_matches  = normalize_group(_fixtures_body(), team_group_map)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tournament-groups" / "copa-america.json"
            write_tournament(groups, matches, group_matches, out_path=out)
            with open(out, encoding="utf-8") as f:
                return json.load(f)

    def test_output_is_valid_json(self):
        self.assertIsInstance(self._run(), dict)

    def test_required_top_level_keys_present(self):
        data = self._run()
        for key in ("generated_at", "slug", "standings", "matches", "groupMatches"):
            self.assertIn(key, data, f"missing top-level key: {key!r}")

    def test_slug_is_copa_america(self):
        self.assertEqual(self._run()["slug"], SLUG)

    def test_generated_at_ends_with_z(self):
        ts = self._run()["generated_at"]
        self.assertTrue(ts.endswith("Z"),
                        f"expected UTC 'Z' suffix, got {ts!r}")

    def test_standings_contain_correct_group_count(self):
        self.assertEqual(len(self._run()["standings"]), 2)

    def test_standings_rows_have_required_fields(self):
        row = self._run()["standings"][0]["table"][0]
        for field in ("position", "team", "playedGames", "won", "draw", "lost",
                      "goalsFor", "goalsAgainst", "goalDifference", "points", "form"):
            self.assertIn(field, row, f"row missing required field: {field!r}")
        for field in ("id", "name", "tla", "crest"):
            self.assertIn(field, row["team"],
                          f"team object missing field: {field!r}")

    def test_knockout_matches_have_required_fields(self):
        match = self._run()["matches"][0]
        for field in ("stage", "homeTeam", "awayTeam", "score"):
            self.assertIn(field, match, f"match missing field: {field!r}")
        self.assertIn("fullTime",  match["score"])
        self.assertIn("penalties", match["score"])
        for team_key in ("homeTeam", "awayTeam"):
            for field in ("id", "name", "tla", "crest"):
                self.assertIn(field, match[team_key])

    def test_crest_urls_non_empty_in_output(self):
        data = self._run()
        for group in data["standings"]:
            for row in group["table"]:
                self.assertTrue(row["team"]["crest"],
                                f"empty crest for {row['team']['name']!r}")
        for match in data["matches"]:
            self.assertTrue(match["homeTeam"]["crest"],
                            f"empty home crest in {match['stage']!r}")
            self.assertTrue(match["awayTeam"]["crest"],
                            f"empty away crest in {match['stage']!r}")

    def test_group_matches_array_present(self):
        data = self._run()
        self.assertIn("groupMatches", data)
        self.assertIsInstance(data["groupMatches"], list)

    def test_group_matches_populated(self):
        data = self._run()
        self.assertGreater(len(data["groupMatches"]), 0,
                           "groupMatches must not be empty for Copa 2024")

    def test_group_matches_have_required_fields(self):
        match = self._run()["groupMatches"][0]
        for field in ("match_id", "video_id", "group", "matchday", "sourceRound",
                      "homeTeam", "awayTeam", "score", "status"):
            self.assertIn(field, match, f"groupMatch missing field {field!r}")
        self.assertIn("fullTime", match["score"])
        for team_key in ("homeTeam", "awayTeam"):
            for field in ("id", "name", "tla", "crest"):
                self.assertIn(field, match[team_key])

    def test_group_matches_matchday_is_integer(self):
        for m in self._run()["groupMatches"]:
            self.assertIsInstance(m["matchday"], int,
                                  "matchday must be an integer, not a string")

    def test_knockout_matches_array_unchanged(self):
        data = self._run()
        self.assertEqual(len(data["matches"]), 2,
                         "knockout matches array must still be QF + Final only")
        stages = {m["stage"] for m in data["matches"]}
        self.assertIn("QUARTER_FINALS", stages)
        self.assertIn("FINAL", stages)
        self.assertNotIn("GROUP_STAGE", stages)


# ── TestLookupCopaVideoId ─────────────────────────────────────────────────────

class TestLookupCopaVideoId(unittest.TestCase):
    """Tests for _lookup_copa_video_id().

    The function reads real files from REPO_ROOT/highlights/copa-america/.
    We patch REPO_ROOT to a temp directory so tests are hermetic.
    """

    # Copa América 2024 — the season _lookup_copa_video_id resolves via cycle formula.
    _SEASON = 2024

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        hl_dir = self.tmp / "highlights" / SLUG / str(self._SEASON)
        hl_dir.mkdir(parents=True)
        self.hl_dir = hl_dir

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _write(self, stem, data):
        (self.hl_dir / f"{stem}.json").write_text(
            json.dumps(data), encoding="utf-8"
        )

    # ── Basic guards ──────────────────────────────────────────────────────────

    def test_none_match_id_returns_none_without_touching_files(self):
        self.assertIsNone(_lookup_copa_video_id(None))

    def test_file_absent_returns_none(self):
        with patch("sync_copa_tournament.REPO_ROOT", self.tmp):
            self.assertIsNone(_lookup_copa_video_id(99999))

    # ── Dict format (current pipeline output: {"matches": [...]}) ─────────────

    def test_dict_format_match_found_returns_first_video_id(self):
        self._write("quarter-final", {"competition": "Copa America", "matches": [
            {"match_id": 1010, "home_team": "Argentina", "away_team": "Ecuador",
             "videos": [{"video_id": "abc123", "title": "...",
                         "published_at": "2024-07-05", "tier_used": 4}]},
        ]})
        with patch("sync_copa_tournament.REPO_ROOT", self.tmp):
            self.assertEqual(_lookup_copa_video_id(1010), "abc123")

    def test_dict_format_no_matching_id_returns_none(self):
        self._write("quarter-final", {"matches": [
            {"match_id": 1010, "videos": [{"video_id": "abc123"}]},
        ]})
        with patch("sync_copa_tournament.REPO_ROOT", self.tmp):
            self.assertIsNone(_lookup_copa_video_id(9999))

    def test_dict_format_empty_videos_list_returns_none(self):
        self._write("quarter-final", {"matches": [
            {"match_id": 1010, "videos": []},
        ]})
        with patch("sync_copa_tournament.REPO_ROOT", self.tmp):
            self.assertIsNone(_lookup_copa_video_id(1010))

    # ── List format (legacy flat-list format) ────────────────────────────────

    def test_list_format_match_found_returns_video_id(self):
        self._write("final", [
            {"match_id": 1020, "home_team": "Argentina", "away_team": "Colombia",
             "videos": [{"video_id": "xyz789", "title": "...",
                         "published_at": "2024-07-15", "tier_used": 4}]},
        ])
        with patch("sync_copa_tournament.REPO_ROOT", self.tmp):
            self.assertEqual(_lookup_copa_video_id(1020), "xyz789")

    # ── Multiple matches / multiple videos ───────────────────────────────────

    def test_first_video_returned_when_multiple_present(self):
        self._write("semi-final", {"matches": [
            {"match_id": 1015, "videos": [
                {"video_id": "first111"},
                {"video_id": "second22"},
            ]},
        ]})
        with patch("sync_copa_tournament.REPO_ROOT", self.tmp):
            self.assertEqual(_lookup_copa_video_id(1015), "first111")

    def test_match_found_in_second_stem_when_first_stem_absent(self):
        """Only the final.json file exists; quarter-final.json is absent."""
        self._write("final", {"matches": [
            {"match_id": 1020, "videos": [{"video_id": "final_vid"}]},
        ]})
        with patch("sync_copa_tournament.REPO_ROOT", self.tmp):
            self.assertEqual(_lookup_copa_video_id(1020), "final_vid")

    # ── Error resilience ─────────────────────────────────────────────────────

    def test_malformed_json_silently_returns_none(self):
        (self.hl_dir / "quarter-final.json").write_text(
            "not valid json", encoding="utf-8"
        )
        with patch("sync_copa_tournament.REPO_ROOT", self.tmp):
            self.assertIsNone(_lookup_copa_video_id(1010))


# ── TestNormalizeGroupMatchId ────────────────────────────────────────────────


class TestNormalizeGroupMatchId(unittest.TestCase):
    """normalize_group() must carry the API-Sports fixture ID as match_id."""

    def _map(self):
        return _build_team_group_map(normalize_standings(_standings_body()))

    def test_group_match_has_match_id(self):
        matches = normalize_group(_fixtures_body(), self._map())
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["match_id"], 1001)

    def test_group_match_has_video_id_null(self):
        matches = normalize_group(_fixtures_body(), self._map())
        self.assertIn("video_id", matches[0])
        self.assertIsNone(matches[0]["video_id"])

    def test_multiple_matchdays_each_carry_match_id(self):
        matches = normalize_group(_fixtures_body_with_groups(), self._map())
        ids = {m["match_id"] for m in matches}
        self.assertIn(1001, ids)
        self.assertIn(1002, ids)


if __name__ == "__main__":
    unittest.main()
