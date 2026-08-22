"""CROSS-REPO CONTRACT TEST — keep in sync with refoot_flutter.

These cases pin the canonical August-1 UTC season-boundary rule as explicit
UTC-instant -> season-integer pairs. THE IDENTICAL TABLE EXISTS IN THE OTHER
REPO at test/season_boundary_contract_test.dart. Changing one table without the
other is exactly the cross-repo drift this guard exists to catch: if the Python
current_season() and the Dart SeasonDateCalculator ever disagree on the
boundary, one repo's CI goes red here.

Rule: season integer = football-data start-year; the boundary is Aug-1 00:00 UTC,
inclusive (month >= 8 -> new season), evaluated in UTC. The cases are UTC BY
DEFINITION — do not rewrite them in local time, or the guard becomes
machine-dependent and stops catching UTC-vs-local regressions.
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest
from datetime import datetime, timedelta

from season_utils import current_season


# Canonical contract cases (all UTC). Must match the Dart table in intent:
# same instants, same expected integers.
CONTRACT_CASES = [
    ("2026-07-31T23:59:00Z", 2025),
    ("2026-08-01T00:00:00Z", 2026),
    ("2026-08-01T12:00:00Z", 2026),
    ("2026-08-02T00:00:00Z", 2026),
    ("2026-12-15T12:00:00Z", 2026),
    # Cross-year sanity pair — locks the rule generally, not just for 2026.
    ("2025-07-31T23:59:00Z", 2024),
    ("2025-08-01T00:00:00Z", 2025),
]


@pytest.mark.parametrize(
    "iso,expected", CONTRACT_CASES, ids=[c[0] for c in CONTRACT_CASES]
)
def test_season_boundary_contract(iso, expected):
    instant = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    assert instant.utcoffset() == timedelta(0), f"contract cases must be UTC: {iso}"
    assert current_season(instant) == expected, (
        f"boundary case {iso} must map to season {expected}"
    )
