import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from highlights_common import override_crest, CREST_OVERRIDES  # noqa: E402
from fixture_providers import FootballDataProvider  # noqa: E402
from sync_standings import extract_total_table  # noqa: E402

_LE_MANS_ID = 535
_LE_MANS_PNG = CREST_OVERRIDES[_LE_MANS_ID]
_WIKI_SVG = "https://upload.wikimedia.org/wikipedia/en/5/57/Le_Mans_FC_logo.svg"


class TestOverrideCrest(unittest.TestCase):
    def test_le_mans_id_maps_to_png(self):
        self.assertTrue(_LE_MANS_PNG.endswith(".png"))
        self.assertEqual(override_crest(_LE_MANS_ID, _WIKI_SVG), _LE_MANS_PNG)

    def test_unmapped_team_keeps_crest(self):
        url = "https://crests.football-data.org/764.svg"
        self.assertEqual(override_crest(764, url), url)

    def test_override_is_only_le_mans(self):
        # Guard against an accidental global sweep — only known-broken ids.
        self.assertEqual(set(CREST_OVERRIDES), {_LE_MANS_ID})


class TestFixturesApplyOverride(unittest.TestCase):
    def test_le_mans_fixture_crest_replaced_others_untouched(self):
        matches = [{
            "id": 1, "matchday": 5, "utcDate": "2026-09-01T18:00:00Z",
            "status": "TIMED",
            "homeTeam": {"id": _LE_MANS_ID, "name": "Le Mans FC", "tla": "LMF",
                         "crest": _WIKI_SVG},
            "awayTeam": {"id": 524, "name": "Paris SG", "tla": "PSG",
                         "crest": "https://crests.football-data.org/524.png"},
            "score": {"fullTime": {"home": None, "away": None}},
        }]
        out = FootballDataProvider("k")._normalize_artifact(matches, "Ligue 1", 2026)
        self.assertEqual(out[0]["homeTeam"]["crest"], _LE_MANS_PNG)          # swapped
        self.assertEqual(out[0]["awayTeam"]["crest"],
                         "https://crests.football-data.org/524.png")          # untouched


class TestStandingsApplyOverride(unittest.TestCase):
    def test_le_mans_standings_crest_replaced(self):
        payload = {"standings": [{"type": "TOTAL", "table": [
            {"position": 1, "team": {"id": _LE_MANS_ID, "name": "Le Mans FC",
                                     "crest": _WIKI_SVG}},
            {"position": 2, "team": {"id": 524, "name": "Paris SG",
                                     "crest": "https://crests.football-data.org/524.png"}},
        ]}]}
        groups = extract_total_table(payload)
        rows = groups[0]["table"]
        self.assertEqual(rows[0]["team"]["crest"], _LE_MANS_PNG)             # swapped
        self.assertEqual(rows[1]["team"]["crest"],
                         "https://crests.football-data.org/524.png")          # untouched


if __name__ == "__main__":
    unittest.main()
