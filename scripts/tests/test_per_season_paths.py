"""Tests for per-season file paths across fixtures, standings, and highlights.

Verifies:
- Each pipeline writes to {type}/{slug}/{season}.json
- Writing season B does not touch season A's file
- Season embedded in JSON matches the directory/filename season
- gw_path() returns the correct per-season path
"""

import json
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest
from datetime import datetime, timezone
from pathlib import Path

from highlights_common import gw_path, season_for_competition
from season_utils import current_season


# ── gw_path ──────────────────────────────────────────────────────────────────

class TestGwPath:
    def test_domestic_league_path(self):
        p = gw_path("Premier League", "gameweek-1", 2025)
        assert p.parts[-3] == "premier-league"
        assert p.parts[-2] == "2025"
        assert p.name == "gameweek-1.json"

    def test_different_season_different_file(self):
        p2025 = gw_path("Premier League", "gameweek-1", 2025)
        p2026 = gw_path("Premier League", "gameweek-1", 2026)
        assert p2025 != p2026

    def test_ucl_path(self):
        p = gw_path("Champions League", "matchday-1", 2025)
        assert p.parts[-3] == "ucl"
        assert p.parts[-2] == "2025"

    def test_world_cup_path(self):
        p = gw_path("World Cup", "matchday-1", 2026)
        assert p.parts[-3] == "world-cup"
        assert p.parts[-2] == "2026"

    def test_euro_cup_path(self):
        p = gw_path("Euro Cup", "final", 2024)
        assert p.parts[-3] == "euro-cup"
        assert p.parts[-2] == "2024"


# ── write_fixtures_artifacts ─────────────────────────────────────────────────

class TestWriteFixturesArtifacts:
    """write_fixtures_artifacts() must write to fixtures/{slug}/{season}.json."""

    def test_writes_per_season_path(self, tmp_path, monkeypatch):
        import fetch_highlights as fh
        monkeypatch.setattr(fh, "FIXTURES_DIR", tmp_path / "fixtures")
        monkeypatch.setattr(fh, "RESULTS_DIR", tmp_path / "results")

        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(
            fh, "season_for_competition",
            lambda comp_name, n=None: current_season(now),
        )

        fh.write_fixtures_artifacts({"Premier League": [{"id": 1}]})

        expected = tmp_path / "fixtures" / "premier-league" / "2025.json"
        assert expected.exists(), f"Expected {expected} to exist"

    def test_json_season_matches_path(self, tmp_path, monkeypatch):
        import fetch_highlights as fh
        monkeypatch.setattr(fh, "FIXTURES_DIR", tmp_path / "fixtures")
        monkeypatch.setattr(fh, "RESULTS_DIR", tmp_path / "results")
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(
            fh, "season_for_competition",
            lambda comp_name, n=None: current_season(now),
        )

        fh.write_fixtures_artifacts({"Premier League": []})

        data = json.loads((tmp_path / "fixtures" / "premier-league" / "2025.json").read_text())
        assert data["season"] == 2025

    def test_prior_season_file_untouched(self, tmp_path, monkeypatch):
        """Writing 2026-27 fixtures must NOT overwrite 2025-26 file."""
        import fetch_highlights as fh
        monkeypatch.setattr(fh, "FIXTURES_DIR", tmp_path / "fixtures")
        monkeypatch.setattr(fh, "RESULTS_DIR", tmp_path / "results")

        prior = tmp_path / "fixtures" / "premier-league" / "2025.json"
        prior.parent.mkdir(parents=True)
        prior.write_text('{"season":2025,"sentinel":"keep-me"}')

        now_new = datetime(2026, 8, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(
            fh, "season_for_competition",
            lambda comp_name, n=None: current_season(now_new),
        )

        fh.write_fixtures_artifacts({"Premier League": []})

        assert json.loads(prior.read_text())["sentinel"] == "keep-me"
        assert (tmp_path / "fixtures" / "premier-league" / "2026.json").exists()


# ── write_standings ───────────────────────────────────────────────────────────

class TestWriteStandings:
    """write_standings() must write to standings/{slug}/{season}.json."""

    def test_writes_per_season_path(self, tmp_path):
        from sync_standings import write_standings
        path = write_standings("Premier League", "premier-league", [], 2025, str(tmp_path))
        expected = tmp_path / "standings" / "premier-league" / "2025.json"
        assert expected.exists()
        assert path == str(expected)

    def test_json_season_matches_path(self, tmp_path):
        from sync_standings import write_standings
        write_standings("Premier League", "premier-league", [], 2025, str(tmp_path))
        data = json.loads((tmp_path / "standings" / "premier-league" / "2025.json").read_text())
        assert data["season"] == 2025

    def test_prior_season_file_untouched(self, tmp_path):
        from sync_standings import write_standings
        prior = tmp_path / "standings" / "premier-league" / "2025.json"
        prior.parent.mkdir(parents=True)
        prior.write_text('{"season":2025,"sentinel":"keep-me"}')

        write_standings("Premier League", "premier-league", [], 2026, str(tmp_path))

        assert json.loads(prior.read_text())["sentinel"] == "keep-me"
        assert (tmp_path / "standings" / "premier-league" / "2026.json").exists()

    def test_fixtures_and_standings_agree_on_season(self, tmp_path, monkeypatch):
        """Both pipelines must embed the same season for the same date."""
        import fetch_highlights as fh
        from sync_standings import write_standings, current_season as standings_season

        monkeypatch.setattr(fh, "FIXTURES_DIR", tmp_path / "fixtures")
        monkeypatch.setattr(fh, "RESULTS_DIR", tmp_path / "results")
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        monkeypatch.setattr(
            fh, "season_for_competition",
            lambda comp_name, n=None: current_season(now),
        )

        fh.write_fixtures_artifacts({"Premier League": []})
        write_standings("Premier League", "premier-league", [], standings_season(now), str(tmp_path))

        fix_data = json.loads(
            (tmp_path / "fixtures" / "premier-league" / "2025.json").read_text()
        )
        std_data = json.loads(
            (tmp_path / "standings" / "premier-league" / "2025.json").read_text()
        )
        assert fix_data["season"] == std_data["season"] == 2025
