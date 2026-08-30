"""
Tests for the FINISHED-preservation merge guard on the fixtures write path.

Covers:
  - merge_preserve_finished()          — the pairwise score-presence invariant
  - merge_fixtures_preserving_finished() — the list merge, keyed by match_id
  - write_fixtures_artifacts()         — the guard applied on-disk across all leagues

The regression this guards against: football-data.org intermittently re-serves an
already-played match as status="TIMED" with a null score; the ~5-minute fixtures
write must not clobber a good FINISHED+score cache with that scoreless record,
while still allowing genuine forward corrections (postponements) and normal
progression (TIMED → FINISHED).
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from fixture_providers import (
    TERMINAL_UNPLAYED_STATUSES,
    merge_preserve_finished,
    merge_fixtures_preserving_finished,
    _has_resolved_score,
)
from highlights_common import (
    COMPETITION_SLUG_MAP,
    DOMESTIC_LEAGUE_COMPS,
    season_for_competition,
    load_json_file,
    write_json_atomic,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rec(match_id, status, home=None, away=None, matchday=1):
    """Build a GroupMatch-shaped fixture record (as _normalize_artifact emits)."""
    return {
        "match_id":    match_id,
        "group":       "",
        "matchday":    matchday,
        "sourceRound": f"Gameweek {matchday}",
        "homeTeam":    {"id": 1, "name": "Home", "shortName": "Home", "tla": "HOM", "crest": ""},
        "awayTeam":    {"id": 2, "name": "Away", "shortName": "Away", "tla": "AWY", "crest": ""},
        "score":       {"fullTime": {"home": home, "away": away}},
        "status":      status,
        "utcDate":     "2026-08-29T14:00:00Z",
    }


# ── The five required scenarios (pairwise) ────────────────────────────────────

class TestMergePreserveFinished:
    def test_a_finished_scored_cached_vs_incoming_timed_null_keeps_cached(self):
        """[the bug] FINISHED+score must survive a scoreless TIMED payload."""
        cached   = _rec(100, "FINISHED", 2, 2)
        incoming = _rec(100, "TIMED")
        kept = merge_preserve_finished(cached, incoming)
        assert kept is cached
        assert kept["status"] == "FINISHED"
        assert kept["score"]["fullTime"] == {"home": 2, "away": 2}

    def test_b_finished_scored_cached_vs_incoming_finished_scored_keeps_incoming(self):
        """Normal re-write / score correction always wins."""
        cached   = _rec(101, "FINISHED", 2, 2)
        incoming = _rec(101, "FINISHED", 3, 1)
        kept = merge_preserve_finished(cached, incoming)
        assert kept is incoming
        assert kept["score"]["fullTime"] == {"home": 3, "away": 1}

    def test_c_finished_scored_cached_vs_incoming_postponed_keeps_incoming(self):
        """Genuine terminal non-played correction is allowed through."""
        cached   = _rec(102, "FINISHED", 1, 0)
        incoming = _rec(102, "POSTPONED")
        kept = merge_preserve_finished(cached, incoming)
        assert kept is incoming
        assert kept["status"] == "POSTPONED"

    def test_d_timed_cached_vs_incoming_finished_scored_keeps_incoming(self):
        """Normal progression TIMED → FINISHED is never blocked."""
        cached   = _rec(103, "TIMED")
        incoming = _rec(103, "FINISHED", 0, 0)
        kept = merge_preserve_finished(cached, incoming)
        assert kept is incoming
        assert kept["status"] == "FINISHED"

    def test_e_no_cached_record_incoming_timed_keeps_incoming(self):
        """A brand-new fixture with no cache counterpart passes through."""
        incoming = _rec(104, "TIMED")
        kept = merge_preserve_finished(None, incoming)
        assert kept is incoming


# ── Score-presence edge cases ─────────────────────────────────────────────────

class TestScorePresence:
    def test_real_zero_zero_counts_as_resolved(self):
        assert _has_resolved_score(_rec(1, "FINISHED", 0, 0)) is True

    def test_null_score_not_resolved(self):
        assert _has_resolved_score(_rec(1, "TIMED")) is False

    def test_partial_null_not_resolved(self):
        assert _has_resolved_score(_rec(1, "IN_PLAY", 1, None)) is False

    def test_zero_zero_finished_cached_survives_scoreless_incoming(self):
        """A real 0-0 must not be mistaken for an empty/unplayed score."""
        cached   = _rec(200, "FINISHED", 0, 0)
        incoming = _rec(200, "TIMED")
        assert merge_preserve_finished(cached, incoming) is cached

    @pytest.mark.parametrize("status", sorted(TERMINAL_UNPLAYED_STATUSES))
    def test_all_terminal_statuses_overwrite_scored_finished(self, status):
        cached   = _rec(201, "FINISHED", 2, 1)
        incoming = _rec(201, status)
        assert merge_preserve_finished(cached, incoming) is incoming

    def test_terminal_set_is_exactly_the_agreed_four(self):
        assert TERMINAL_UNPLAYED_STATUSES == frozenset(
            {"POSTPONED", "CANCELLED", "SUSPENDED", "AWARDED"}
        )


# ── List-level merge ──────────────────────────────────────────────────────────

class TestMergeFixturesList:
    def test_preserves_order_and_length(self):
        cached   = [_rec(1, "FINISHED", 1, 0), _rec(2, "FINISHED", 2, 2)]
        incoming = [_rec(2, "TIMED"), _rec(1, "TIMED")]
        merged   = merge_fixtures_preserving_finished(cached, incoming)
        assert [r["match_id"] for r in merged] == [2, 1]  # incoming order kept
        assert all(r["status"] == "FINISHED" for r in merged)  # both preserved

    def test_new_match_id_passes_through(self):
        cached   = [_rec(1, "FINISHED", 1, 0)]
        incoming = [_rec(1, "TIMED"), _rec(9, "TIMED")]
        merged   = merge_fixtures_preserving_finished(cached, incoming)
        by_id    = {r["match_id"]: r for r in merged}
        assert by_id[1]["status"] == "FINISHED"   # preserved
        assert by_id[9]["status"] == "TIMED"      # new fixture written as-is

    def test_pairs_strictly_by_match_id_not_position(self):
        cached   = [_rec(1, "FINISHED", 1, 0)]
        # Same list position but a different match_id → no preservation applies.
        incoming = [_rec(2, "TIMED")]
        merged   = merge_fixtures_preserving_finished(cached, incoming)
        assert merged[0]["match_id"] == 2
        assert merged[0]["status"] == "TIMED"

    def test_empty_cache_returns_incoming_unchanged(self):
        incoming = [_rec(1, "TIMED"), _rec(2, "FINISHED", 3, 0)]
        merged   = merge_fixtures_preserving_finished([], incoming)
        assert merged == incoming

    def test_none_match_id_incoming_passes_through(self):
        cached   = [_rec(1, "FINISHED", 1, 0)]
        incoming = [_rec(None, "TIMED")]
        merged   = merge_fixtures_preserving_finished(cached, incoming)
        assert merged[0]["match_id"] is None


# ── Write-path integration: guard applied on-disk, across all five leagues ─────

class TestWriteFixturesArtifactsGuard:
    def test_regression_blocked_on_disk_for_all_five_leagues(self, tmp_path, monkeypatch):
        # Lazy import: keep fetch_highlights out of sys.modules at collection time
        # so the find_channel_candidates pipeline-isolation guard stays green.
        import fetch_highlights
        monkeypatch.setattr(fetch_highlights, "FIXTURES_DIR", tmp_path)

        artifacts = {}
        for comp in DOMESTIC_LEAGUE_COMPS:
            slug   = COMPETITION_SLUG_MAP[comp]
            season = season_for_competition(comp)
            path   = tmp_path / slug / f"{season}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Seed a good, scored FINISHED cache.
            write_json_atomic(path, {
                "competition": comp, "season": season,
                "generated_at": "2026-08-30T06:11:00Z",
                "fixtures": [_rec(500, "FINISHED", 2, 1)],
            })
            # Incoming payload regresses that match to TIMED/null.
            artifacts[comp] = [_rec(500, "TIMED")]

        fetch_highlights.write_fixtures_artifacts(artifacts)

        for comp in DOMESTIC_LEAGUE_COMPS:
            slug   = COMPETITION_SLUG_MAP[comp]
            season = season_for_competition(comp)
            data   = load_json_file(tmp_path / slug / f"{season}.json")
            rec    = data["fixtures"][0]
            assert rec["status"] == "FINISHED", f"{comp} lost its FINISHED status"
            assert rec["score"]["fullTime"] == {"home": 2, "away": 1}, comp

    def test_forward_correction_and_new_fixture_still_written(self, tmp_path, monkeypatch):
        import fetch_highlights
        monkeypatch.setattr(fetch_highlights, "FIXTURES_DIR", tmp_path)
        comp   = "Premier League"
        slug   = COMPETITION_SLUG_MAP[comp]
        season = season_for_competition(comp)
        path   = tmp_path / slug / f"{season}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {
            "competition": comp, "season": season,
            "generated_at": "2026-08-30T06:11:00Z",
            "fixtures": [_rec(600, "FINISHED", 1, 0)],
        })
        # 600 legitimately postponed; 601 is a brand-new scheduled fixture.
        fetch_highlights.write_fixtures_artifacts({
            comp: [_rec(600, "POSTPONED"), _rec(601, "TIMED")],
        })
        data  = load_json_file(path)
        by_id = {r["match_id"]: r for r in data["fixtures"]}
        assert by_id[600]["status"] == "POSTPONED"   # forward correction applied
        assert by_id[601]["status"] == "TIMED"       # new fixture written
