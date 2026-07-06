"""Canonical season-year helper, shared by sync_standings.py and fetch_highlights.py.

Stdlib-only — no external dependencies — so sync_standings.py (which runs in a
minimal workflow without requests installed) can import this safely.

The August 1 boundary is the canonical rule for the whole pipeline:
  month < 8  → previous season (e.g. July 2026 → 2025, i.e. the 2025-26 season)
  month >= 8 → new season     (e.g. August 2026 → 2026, i.e. the 2026-27 season)

The app's Dart SeasonDateCalculator uses the same August threshold — see
lib/services/season_date_calculator.dart.  Any change here MUST be mirrored there.
"""

from datetime import datetime, timezone


def current_season(now=None) -> int:
    """Return the FD season start year for the domestic football calendar.

    Domestic leagues (and UCL/UEL) use an August–July season convention.
    FD identifies each season by its start year: 2025 = the 2025-26 season.

    Before August 1 (UTC) the previous season is still current; on/after August 1
    the new season begins.  This avoids querying a "registered but not started"
    season (the bug that caused the fixtures pipeline to return 380 SCHEDULED
    matches from 2026-27 instead of the completed 2025-26 season in July 2026).

    now is injectable for unit tests; defaults to UTC today.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return now.year - 1 if now.month < 8 else now.year
