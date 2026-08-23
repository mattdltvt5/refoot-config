"""Tests for scripts/sync_standings.py.

All tests are pure-function / mocked-HTTP — no live FD calls.
"""

import json, sys, os, unittest, tempfile
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sync_standings import current_season, extract_total_table, fetch_standings, write_standings
from sync_standings import had_recent_finish, main_recent


class TestCurrentSeason(unittest.TestCase):
    def test_before_august_returns_previous_year(self):
        # July 2026 → 2025-26 season → FD season key 2025
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        self.assertEqual(current_season(now), 2025)

    def test_in_august_returns_current_year(self):
        # August 2026 → 2026-27 season → FD season key 2026
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        self.assertEqual(current_season(now), 2026)

    def test_after_august_returns_current_year(self):
        now = datetime(2026, 12, 15, tzinfo=timezone.utc)
        self.assertEqual(current_season(now), 2026)

    def test_january_returns_previous_year(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(current_season(now), 2025)


class TestExtractTotalTable(unittest.TestCase):
    def test_returns_only_total_type(self):
        payload = {
            "standings": [
                {"type": "HOME",  "table": [{"position": 1}]},
                {"type": "AWAY",  "table": [{"position": 1}]},
                {"type": "TOTAL", "table": [{"position": 1}, {"position": 2}]},
            ]
        }
        result = extract_total_table(payload)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "TOTAL")
        self.assertEqual(len(result[0]["table"]), 2)

    def test_returns_empty_when_no_total(self):
        payload = {"standings": [{"type": "HOME", "table": []}]}
        self.assertEqual(extract_total_table(payload), [])

    def test_returns_empty_for_missing_standings_key(self):
        self.assertEqual(extract_total_table({}), [])

    def test_multiple_total_groups_for_tournaments(self):
        # WC / Euro have one TOTAL entry per group — all should be returned
        payload = {
            "standings": [
                {"type": "TOTAL", "group": "GROUP_A", "table": [{"position": 1}]},
                {"type": "TOTAL", "group": "GROUP_B", "table": [{"position": 1}]},
                {"type": "HOME",  "group": "GROUP_A", "table": []},
            ]
        }
        result = extract_total_table(payload)
        self.assertEqual(len(result), 2)


class TestWriteStandings(unittest.TestCase):
    _SAMPLE_ROWS = [
        {
            "position": 1,
            "team": {
                "id": 66, "name": "Liverpool FC",
                "shortName": "Liverpool", "tla": "LIV",
                "crest": "https://crests.football-data.org/64.svg",
            },
            "playedGames": 38, "won": 28, "draw": 5, "lost": 5,
            "points": 89, "goalsFor": 94, "goalsAgainst": 41,
            "goalDifference": 53, "form": "WWWWW",
        }
    ]

    def test_output_shape_matches_flutter_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_standings("Premier League", "premier-league",
                                   self._SAMPLE_ROWS, 2025, out_dir=tmp)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

        # Top-level fields
        self.assertIn("generated_at", data)
        self.assertEqual(data["competition"], "Premier League")
        self.assertEqual(data["slug"], "premier-league")
        self.assertEqual(data["season"], 2025)
        self.assertIn("standings", data)
        self.assertEqual(len(data["standings"]), 1)

        # Row fields that GroupStanding.fromJson reads
        row = data["standings"][0]
        self.assertEqual(row["position"], 1)
        self.assertEqual(row["team"]["id"], 66)
        self.assertEqual(row["team"]["tla"], "LIV")
        self.assertEqual(row["team"]["crest"], "https://crests.football-data.org/64.svg")
        self.assertEqual(row["points"], 89)
        self.assertEqual(row["form"], "WWWWW")

    def test_generated_at_ends_with_z(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_standings("Test", "test", [], 2025, out_dir=tmp)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        self.assertTrue(data["generated_at"].endswith("Z"),
                        f"expected UTC 'Z' suffix, got {data['generated_at']!r}")

    def test_file_is_valid_json_after_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_standings("Bundesliga", "bundesliga",
                                   self._SAMPLE_ROWS, 2025, out_dir=tmp)
            with open(path, encoding="utf-8") as f:
                data = json.load(f)  # should not raise
        self.assertIsInstance(data, dict)

    def test_creates_standings_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_standings("Serie A", "serie-a", [], 2025, out_dir=tmp)
            self.assertTrue(
                os.path.isdir(os.path.join(tmp, "standings", "serie-a")),
                "standings/{slug}/ subdirectory must be created if absent",
            )


class TestFetchStandings(unittest.TestCase):
    def _make_mock_response(self, payload):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        mock_resp.__enter__ = lambda self: self
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def test_calls_correct_endpoint(self):
        payload = {"standings": [{"type": "TOTAL", "table": []}]}
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = self._make_mock_response(payload)
            result = fetch_standings(2021, "test_key", base_url="https://mock.api")

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertIn("2021", req.get_full_url())
        self.assertIn("mock.api", req.get_full_url())

    def test_sends_auth_header(self):
        payload = {"standings": []}
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = self._make_mock_response(payload)
            fetch_standings(2021, "secret_key", base_url="https://mock.api")

        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.get_header("X-auth-token"), "secret_key")

    def test_returns_parsed_payload(self):
        payload = {"standings": [{"type": "TOTAL", "table": [{"position": 1}]}]}
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = self._make_mock_response(payload)
            result = fetch_standings(2021, "key", base_url="https://mock.api")

        self.assertEqual(result, payload)

    def test_season_param_included_in_url(self):
        payload = {"standings": []}
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = self._make_mock_response(payload)
            fetch_standings(2021, "key", base_url="https://mock.api", season=2025)

        req = mock_urlopen.call_args[0][0]
        self.assertIn("season=2025", req.get_full_url())

    def test_no_season_param_when_omitted(self):
        payload = {"standings": []}
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = self._make_mock_response(payload)
            fetch_standings(2021, "key", base_url="https://mock.api")

        req = mock_urlopen.call_args[0][0]
        self.assertNotIn("season", req.get_full_url())



class TestSmartSkip(unittest.TestCase):
    """--if-recent-finish: refresh a league's standings only when one of its
    fixtures finished within RECENT_FINISH_HOURS; skip (0 FD calls) otherwise."""

    _now = datetime(2026, 8, 23, 21, 0, tzinfo=timezone.utc)

    def _write_fixtures(self, d, slug, fixtures):
        os.makedirs(os.path.join(d, 'fixtures', slug), exist_ok=True)
        with open(os.path.join(d, 'fixtures', slug, '2025.json'), 'w', encoding='utf-8') as f:
            json.dump({'fixtures': fixtures}, f)

    def test_recent_finish_is_eligible(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_fixtures(d, 'premier-league',
                [{'status': 'FINISHED', 'utcDate': '2026-08-23T19:00:00Z'}])  # ~2h ago
            self.assertTrue(had_recent_finish('premier-league', 2025, d, self._now))

    def test_old_finish_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_fixtures(d, 'laliga',
                [{'status': 'FINISHED', 'utcDate': '2026-08-21T19:00:00Z'}])  # 2 days ago
            self.assertFalse(had_recent_finish('laliga', 2025, d, self._now))

    def test_live_only_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_fixtures(d, 'serie-a',
                [{'status': 'IN_PLAY', 'utcDate': '2026-08-23T20:00:00Z'}])
            self.assertFalse(had_recent_finish('serie-a', 2025, d, self._now))

    def test_missing_fixtures_file_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(had_recent_finish('bundesliga', 2025, d, self._now))

    def test_unparseable_date_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_fixtures(d, 'ligue-1',
                [{'status': 'FINISHED', 'utcDate': 'not-a-date'}])
            self.assertFalse(had_recent_finish('ligue-1', 2025, d, self._now))

    @patch('sync_standings.current_season', return_value=2025)
    @patch('sync_standings.fetch_standings')
    def test_main_recent_refreshes_only_eligible(self, mock_fetch, _season):
        mock_fetch.return_value = {'standings': [{'type': 'TOTAL', 'table': [{'position': 1}]}]}
        with tempfile.TemporaryDirectory() as d:
            self._write_fixtures(d, 'premier-league',
                [{'status': 'FINISHED', 'utcDate': '2026-08-23T19:00:00Z'}])   # eligible
            self._write_fixtures(d, 'laliga',
                [{'status': 'FINISHED', 'utcDate': '2026-08-20T19:00:00Z'}])   # too old
            main_recent('fake-key', out_dir=d, now=self._now)
            called_ids = [c.args[0] for c in mock_fetch.call_args_list]
            self.assertEqual(called_ids, [2021])  # Premier League only
            self.assertTrue(os.path.exists(os.path.join(d, 'standings', 'premier-league', '2025.json')))
            self.assertFalse(os.path.exists(os.path.join(d, 'standings', 'laliga', '2025.json')))

    @patch('sync_standings.current_season', return_value=2025)
    @patch('sync_standings.fetch_standings')
    def test_main_recent_no_eligible_makes_no_fd_calls(self, mock_fetch, _season):
        with tempfile.TemporaryDirectory() as d:
            self._write_fixtures(d, 'premier-league',
                [{'status': 'FINISHED', 'utcDate': '2026-08-01T19:00:00Z'}])   # old
            main_recent('fake-key', out_dir=d, now=self._now)
            mock_fetch.assert_not_called()

if __name__ == "__main__":
    unittest.main()
