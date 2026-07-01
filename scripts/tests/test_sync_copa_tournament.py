"""Tests for scripts/sync_copa_tournament.py.

All HTTP is mocked — no live API-Sports calls are made.
Tests cover pure normalisation (normalize_standings, normalize_knockout)
and the file-writing contract (write_tournament).
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Set the API key env var before any import that touches ApiSportsProvider.__init__.
os.environ.setdefault("APISPORTS_API_KEY", "test_key_copa_fixture")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync_copa_tournament import (
    KNOCKOUT_STAGE_MAP,
    SLUG,
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


# ── TestWriteTournament ───────────────────────────────────────────────────────

class TestWriteTournament(unittest.TestCase):

    def _run(self):
        groups  = normalize_standings(_standings_body())
        matches = normalize_knockout(_fixtures_body())
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tournament-groups" / "copa-america.json"
            write_tournament(groups, matches, out_path=out)
            with open(out, encoding="utf-8") as f:
                return json.load(f)

    def test_output_is_valid_json(self):
        self.assertIsInstance(self._run(), dict)

    def test_required_top_level_keys_present(self):
        data = self._run()
        for key in ("generated_at", "slug", "standings", "matches"):
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


if __name__ == "__main__":
    unittest.main()
