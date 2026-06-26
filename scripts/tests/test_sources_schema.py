"""Config schema validation tests for sources.json.

Invariant 13: every competition in any lookup section must appear in the
canonical `competitions` key.  A partial add — adding a competition to
`playlists` without also adding it to `competitions` — must fail here.

Invariant 16: every competition in `competitions` must have a corresponding
entry in `playlists` (even an empty dict {}), so callers that iterate
`sources["playlists"]` see the competition at all.

IMPORTANT: these tests read the live sources.json.  If a real defect is
found (a competition present in one section but missing from another), pytest
reports it as FAILED.  Do NOT silently fix sources.json inside this test
file — report the defect and fix it in a separate commit.
"""

import json
from pathlib import Path

import pytest

from highlights_common import SOURCES_JSON as _SOURCES_JSON_PATH

SOURCES_PATH = Path(_SOURCES_JSON_PATH)

REQUIRED_TOP_LEVEL_KEYS = {
    "competitions",
    "teams",
    "playlists",
    "teamPlaylists",
    "teamLists",
}


@pytest.fixture(scope="module")
def sources() -> dict:
    """Load sources.json once for the entire module."""
    with open(SOURCES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ── Structural integrity ──────────────────────────────────────────────────────

def test_sources_json_has_required_keys(sources):
    """sources.json must contain all required top-level keys."""
    for key in sorted(REQUIRED_TOP_LEVEL_KEYS):
        assert key in sources, f"Required key {key!r} missing from sources.json"


def test_competitions_is_non_empty(sources):
    """At least one competition must be registered in sources.json."""
    assert len(sources["competitions"]) > 0, "sources['competitions'] must not be empty"


# ── Invariant 13: no orphan competitions ─────────────────────────────────────

def test_playlists_keys_subset_of_competitions(sources):
    """Every competition in sources['playlists'] must appear in sources['competitions'].

    A partial add — adding a broadcaster playlist for a new competition without
    registering it in competitions — must fail this test.
    """
    known = set(sources["competitions"])
    for comp in sources.get("playlists", {}):
        assert comp in known, (
            f"Competition {comp!r} found in playlists but not in competitions — "
            "partial add detected; add it to competitions first"
        )


def test_team_playlists_keys_subset_of_competitions(sources):
    """Every competition in sources['teamPlaylists'] must appear in sources['competitions']."""
    known = set(sources["competitions"])
    for comp in sources.get("teamPlaylists", {}):
        assert comp in known, (
            f"Competition {comp!r} found in teamPlaylists but not in competitions"
        )


def test_team_lists_keys_subset_of_competitions(sources):
    """Every competition in sources['teamLists'] must appear in sources['competitions']."""
    known = set(sources["competitions"])
    for comp in sources.get("teamLists", {}):
        assert comp in known, (
            f"Competition {comp!r} found in teamLists but not in competitions"
        )


# ── Invariant 16: every competition has a playlists entry ────────────────────

def test_every_competition_has_playlists_entry(sources):
    """Every competition in sources['competitions'] must have a key in sources['playlists'].

    Even an empty dict {} is fine — the key must exist so the pipeline's
    per-broadcaster iteration doesn't silently skip the competition.
    A partial add in the reverse direction (competition registered but no
    playlists entry) must fail this test.
    """
    playlist_comps = set(sources.get("playlists", {}))
    for comp in sources["competitions"]:
        assert comp in playlist_comps, (
            f"Competition {comp!r} is in competitions but has no entry in playlists. "
            "Add an empty dict to sources['playlists'][{comp!r}] = {{}}"
        )
