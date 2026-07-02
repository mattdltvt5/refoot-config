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


def test_team_lists_entries_have_required_fields_when_object_format(sources):
    """When teamLists entries are dicts (new format), they must carry name/id/tla/crestUrl.

    String entries (pre-migration format) are accepted during the rollout window
    before sync-teams.yml has run with the new code.  Once it runs, all entries
    become dicts and this test enforces their shape.
    """
    for comp, entries in sources.get("teamLists", {}).items():
        for entry in entries:
            if not isinstance(entry, dict):
                continue  # old string-list format — still valid during rollout
            for field in ("name", "id", "tla", "crestUrl"):
                assert field in entry, (
                    f"teamLists[{comp!r}] entry missing field {field!r}: {entry!r}"
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


# ── Playlist ID format ────────────────────────────────────────────────────────

def test_playlist_ids_are_valid_format(sources):
    """All PL-prefixed playlist IDs in sources.json must match the valid YouTube format.

    Valid: PL followed by at least 20 alphanumeric/underscore/dash characters
    (total length >= 22).  Short IDs like PLXHZm5xDlEdQ (13 chars) are truncated
    and will be silently dropped by load_sources() at pipeline time.

    Fix: find the correct full-length playlist ID and replace the entry in
    sources.json.  If unavailable, set the broadcaster list to [] and leave a
    note in the README rather than committing a placeholder.
    """
    import re
    VALID_PL = re.compile(r"PL[A-Za-z0-9_\-]{20,}")
    errors = []
    for comp, broadcasters in sources.get("playlists", {}).items():
        if not isinstance(broadcasters, dict):
            continue
        for bcast, ids in broadcasters.items():
            items = ids if isinstance(ids, list) else [ids]
            for pid in items:
                if isinstance(pid, str) and pid.startswith("PL"):
                    if not VALID_PL.fullmatch(pid):
                        errors.append(
                            f"{comp}/{bcast}: {pid!r} (len={len(pid)}, "
                            f"need PL + ≥20 chars)"
                        )
    assert not errors, (
        "Truncated or malformed PL playlist IDs found — these will be silently "
        "skipped by load_sources().  Fix each entry or set its list to [] and "
        f"document as a TODO in README.md:\n  " + "\n  ".join(errors)
    )
