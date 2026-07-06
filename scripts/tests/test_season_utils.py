"""Tests for the canonical season-selection helpers in season_utils.py and
season_for_competition() in highlights_common.py.

These tests are the authoritative spec for the August boundary rule.  Any change
to the threshold here must also be reflected in the app's SeasonDateCalculator
(lib/services/season_date_calculator.dart).
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest
from datetime import datetime, timezone

from season_utils import current_season
from highlights_common import season_for_competition


# ── current_season() boundary tests ──────────────────────────────────────────

class TestCurrentSeason:
    """current_season() must return the previous year before August, current year from August."""

    def test_july_returns_previous_year(self):
        # July 2026 → still in 2025-26 season → FD key 2025
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        assert current_season(now) == 2025

    def test_july_last_day_returns_previous_year(self):
        # July 31 is the last day before the switch — must still return previous year
        now = datetime(2026, 7, 31, tzinfo=timezone.utc)
        assert current_season(now) == 2025

    def test_august_first_returns_new_year(self):
        # August 1 is the exact boundary — new season starts
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        assert current_season(now) == 2026

    def test_august_mid_returns_new_year(self):
        now = datetime(2026, 8, 15, tzinfo=timezone.utc)
        assert current_season(now) == 2026

    def test_december_returns_new_year(self):
        now = datetime(2026, 12, 31, tzinfo=timezone.utc)
        assert current_season(now) == 2026

    def test_january_returns_previous_year(self):
        # January 2027 is mid-season of 2026-27 → FD key 2026
        now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        assert current_season(now) == 2026

    def test_june_returns_previous_year(self):
        now = datetime(2026, 6, 30, tzinfo=timezone.utc)
        assert current_season(now) == 2025

    def test_march_returns_previous_year(self):
        # Mid-season (March 2026) is still within 2025-26 → FD key 2025
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        assert current_season(now) == 2025

    def test_default_arg_returns_int(self):
        # Smoke test: calling without now uses live UTC clock, must return an int
        result = current_season()
        assert isinstance(result, int)
        assert result >= 2020


# ── season_for_competition() — domestic leagues use the August boundary ───────

class TestSeasonForCompetitionDomestic:
    """Domestic leagues must use current_season() — the August boundary rule."""

    _DOMESTIC = ["Premier League", "LaLiga", "Serie A", "Bundesliga", "Ligue 1"]

    @pytest.mark.parametrize("comp_name", _DOMESTIC)
    def test_july_returns_previous_season(self, comp_name):
        # This was the bug: July returned 2026 (upcoming) instead of 2025 (previous)
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        assert season_for_competition(comp_name, now) == 2025

    @pytest.mark.parametrize("comp_name", _DOMESTIC)
    def test_august_returns_new_season(self, comp_name):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        assert season_for_competition(comp_name, now) == 2026

    def test_pl_july_previous_season(self):
        # Explicit regression test: the documented bug date
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        assert season_for_competition("Premier League", now) == 2025

    def test_pl_august_new_season(self):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        assert season_for_competition("Premier League", now) == 2026

    def test_ucl_july_previous_season(self):
        # UCL/UEL also follow the domestic calendar
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        assert season_for_competition("Champions League", now) == 2025

    def test_uel_july_previous_season(self):
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        assert season_for_competition("Europa League", now) == 2025


# ── season_for_competition() — summer tournaments are unaffected ──────────────

class TestSeasonForCompetitionSummer:
    """Summer tournament logic is unchanged — these tests protect against regression."""

    def test_world_cup_2026_in_june(self):
        # World Cup 2026 runs in June/July 2026; anchor=2022, period=4 → 2026
        now = datetime(2026, 6, 15, tzinfo=timezone.utc)
        assert season_for_competition("World Cup", now) == 2026

    def test_world_cup_2022_before_2026(self):
        # In 2025 (between editions) → most recent completed = 2022
        now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        assert season_for_competition("World Cup", now) == 2022

    def test_euro_cup_2024_in_2024(self):
        # Euro Cup 2024; anchor=2024, period=4
        now = datetime(2024, 7, 1, tzinfo=timezone.utc)
        assert season_for_competition("Euro Cup", now) == 2024

    def test_euro_cup_2024_in_2025(self):
        # 2025 is between editions → most recent = 2024
        now = datetime(2025, 3, 1, tzinfo=timezone.utc)
        assert season_for_competition("Euro Cup", now) == 2024


# ── Ensure sync_standings.current_season is the same function ─────────────────

class TestConsolidation:
    """Verify that sync_standings re-exports the canonical function unchanged."""

    def test_sync_standings_uses_canonical_function(self):
        # Import from sync_standings — it must be the same object as season_utils.current_season
        from sync_standings import current_season as standings_fn
        assert standings_fn is current_season, (
            "sync_standings.current_season must be the same function as "
            "season_utils.current_season — it must not have its own definition"
        )

    def test_standings_and_fixtures_agree_in_july(self):
        # Both pipelines must return the same season for the same date
        from sync_standings import current_season as standings_fn
        now = datetime(2026, 7, 6, tzinfo=timezone.utc)
        assert standings_fn(now) == season_for_competition("Premier League", now) == 2025

    def test_standings_and_fixtures_agree_in_august(self):
        from sync_standings import current_season as standings_fn
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        assert standings_fn(now) == season_for_competition("Premier League", now) == 2026
