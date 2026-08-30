"""
Tests for the durable results ledger (self-healing overlay).

The ledger is an FD-independent record of every FINISHED result the pipeline has
observed. Once recorded, a result is re-asserted on every run, so an upstream
FINISHED->TIMED/null reversion self-heals without manual intervention.

Covers:
  - update_results_ledger()  — recording, corrections, terminal clears, first_finished_at
  - apply_results_ledger()   — the self-healing overlay
  - write_fixtures_artifacts() — ledger persisted + overlay applied on-disk, all leagues
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from fixture_providers import (
    update_results_ledger,
    apply_results_ledger,
)
from highlights_common import (
    COMPETITION_SLUG_MAP,
    DOMESTIC_LEAGUE_COMPS,
    season_for_competition,
    load_json_file,
    write_json_atomic,
)


def _rec(match_id, status, home=None, away=None, matchday=1, crest="x.png"):
    return {
        "match_id":    match_id,
        "group":       "",
        "matchday":    matchday,
        "sourceRound": f"Gameweek {matchday}",
        "homeTeam":    {"id": 1, "name": "Home", "shortName": "Home", "tla": "HOM", "crest": crest},
        "awayTeam":    {"id": 2, "name": "Away", "shortName": "Away", "tla": "AWY", "crest": ""},
        "score":       {"fullTime": {"home": home, "away": away}},
        "status":      status,
        "utcDate":     "2026-08-29T14:00:00Z",
    }


# ── update_results_ledger ─────────────────────────────────────────────────────

class TestUpdateLedger:
    def test_records_finished_with_score(self):
        led = update_results_ledger({}, [_rec(1, "FINISHED", 2, 1)])
        assert led["1"]["status"] == "FINISHED"
        assert led["1"]["score"]["fullTime"] == {"home": 2, "away": 1}
        assert led["1"]["first_finished_at"]

    def test_records_real_zero_zero(self):
        led = update_results_ledger({}, [_rec(1, "FINISHED", 0, 0)])
        assert led["1"]["score"]["fullTime"] == {"home": 0, "away": 0}

    def test_timed_not_recorded(self):
        assert update_results_ledger({}, [_rec(1, "TIMED")]) == {}

    def test_scoreless_finished_not_recorded(self):
        # A FINISHED with null score is not a usable result.
        assert update_results_ledger({}, [_rec(1, "FINISHED", None, None)]) == {}

    def test_score_correction_updates(self):
        led = update_results_ledger({}, [_rec(1, "FINISHED", 2, 1)])
        led = update_results_ledger(led, [_rec(1, "FINISHED", 3, 1)])
        assert led["1"]["score"]["fullTime"] == {"home": 3, "away": 1}

    def test_first_finished_at_preserved_across_updates(self):
        led = update_results_ledger({}, [_rec(1, "FINISHED", 2, 1)])
        stamp = led["1"]["first_finished_at"]
        led = update_results_ledger(led, [_rec(1, "FINISHED", 3, 2)])
        assert led["1"]["first_finished_at"] == stamp

    @pytest.mark.parametrize("status", ["POSTPONED", "CANCELLED", "SUSPENDED", "AWARDED"])
    def test_terminal_status_clears_entry(self, status):
        led = update_results_ledger({}, [_rec(1, "FINISHED", 2, 1)])
        led = update_results_ledger(led, [_rec(1, status)])
        assert "1" not in led

    def test_timed_does_not_clear_recorded_finished(self):
        led = update_results_ledger({}, [_rec(1, "FINISHED", 2, 1)])
        led = update_results_ledger(led, [_rec(1, "TIMED")])
        assert led["1"]["status"] == "FINISHED"

    def test_none_match_id_ignored(self):
        assert update_results_ledger({}, [_rec(None, "FINISHED", 1, 0)]) == {}

    def test_input_ledger_not_mutated(self):
        orig = {}
        update_results_ledger(orig, [_rec(1, "FINISHED", 1, 0)])
        assert orig == {}


# ── apply_results_ledger (self-healing overlay) ───────────────────────────────

class TestApplyLedger:
    def _ledger(self):
        return update_results_ledger({}, [_rec(1, "FINISHED", 2, 2)])

    def test_reasserts_finished_over_timed_null(self):
        healed = apply_results_ledger([_rec(1, "TIMED")], self._ledger())
        assert healed[0]["status"] == "FINISHED"
        assert healed[0]["score"]["fullTime"] == {"home": 2, "away": 2}

    def test_reasserts_over_paused_null(self):
        # The live flicker we actually saw (Tottenham showed PAUSED with null score).
        healed = apply_results_ledger([_rec(1, "PAUSED")], self._ledger())
        assert healed[0]["status"] == "FINISHED"

    def test_incoming_with_real_score_wins(self):
        # A genuine correction (incoming carries its own resolved score) is kept.
        healed = apply_results_ledger([_rec(1, "FINISHED", 3, 1)], self._ledger())
        assert healed[0]["score"]["fullTime"] == {"home": 3, "away": 1}

    def test_incoming_terminal_status_wins(self):
        healed = apply_results_ledger([_rec(1, "POSTPONED")], self._ledger())
        assert healed[0]["status"] == "POSTPONED"

    def test_match_not_in_ledger_passes_through(self):
        healed = apply_results_ledger([_rec(9, "TIMED")], self._ledger())
        assert healed[0]["status"] == "TIMED"

    def test_preserves_other_fields_when_healing(self):
        # Fresh crest from the current payload must survive the heal.
        healed = apply_results_ledger([_rec(1, "TIMED", crest="new.png")], self._ledger())
        assert healed[0]["homeTeam"]["crest"] == "new.png"
        assert healed[0]["status"] == "FINISHED"

    def test_preserves_order(self):
        led = update_results_ledger({}, [_rec(1, "FINISHED", 1, 0), _rec(2, "FINISHED", 2, 0)])
        healed = apply_results_ledger([_rec(2, "TIMED"), _rec(1, "TIMED")], led)
        assert [r["match_id"] for r in healed] == [2, 1]
        assert all(r["status"] == "FINISHED" for r in healed)

    def test_empty_ledger_is_noop(self):
        fx = [_rec(1, "TIMED")]
        assert apply_results_ledger(fx, {}) == fx


# ── Write-path integration: ledger persisted + overlay heals, all leagues ─────

class TestWriteFixturesLedgerIntegration:
    def test_lost_result_reasserted_on_disk_all_leagues(self, tmp_path, monkeypatch):
        import fetch_highlights
        fdir = tmp_path / "fixtures"; rdir = tmp_path / "results"
        monkeypatch.setattr(fetch_highlights, "FIXTURES_DIR", fdir)
        monkeypatch.setattr(fetch_highlights, "RESULTS_DIR", rdir)

        artifacts = {}
        for comp in DOMESTIC_LEAGUE_COMPS:
            slug = COMPETITION_SLUG_MAP[comp]; season = season_for_competition(comp)
            # Pre-seed the durable ledger with a FINISHED result (as the seed script would).
            (rdir / slug).mkdir(parents=True, exist_ok=True)
            write_json_atomic(rdir / slug / f"{season}.json", {
                "competition": comp, "season": season, "generated_at": "seed",
                "results": {"700": {"status": "FINISHED",
                                    "score": {"fullTime": {"home": 2, "away": 1}},
                                    "utcDate": "2026-08-29T14:00:00Z",
                                    "first_finished_at": "seed"}},
            })
            # Incoming FD payload has that match as TIMED/null (the regression).
            artifacts[comp] = [_rec(700, "TIMED")]

        fetch_highlights.write_fixtures_artifacts(artifacts)

        for comp in DOMESTIC_LEAGUE_COMPS:
            slug = COMPETITION_SLUG_MAP[comp]; season = season_for_competition(comp)
            fx = load_json_file(fdir / slug / f"{season}.json")["fixtures"]
            rec = next(m for m in fx if m["match_id"] == 700)
            assert rec["status"] == "FINISHED", f"{comp} not healed"
            assert rec["score"]["fullTime"] == {"home": 2, "away": 1}, comp

    def test_new_finished_result_gets_ledgered(self, tmp_path, monkeypatch):
        import fetch_highlights
        fdir = tmp_path / "fixtures"; rdir = tmp_path / "results"
        monkeypatch.setattr(fetch_highlights, "FIXTURES_DIR", fdir)
        monkeypatch.setattr(fetch_highlights, "RESULTS_DIR", rdir)
        comp = "Premier League"; slug = COMPETITION_SLUG_MAP[comp]; season = season_for_competition(comp)

        fetch_highlights.write_fixtures_artifacts({comp: [_rec(701, "FINISHED", 1, 0)]})
        led = load_json_file(rdir / slug / f"{season}.json")["results"]
        assert led["701"]["status"] == "FINISHED"
        assert led["701"]["score"]["fullTime"] == {"home": 1, "away": 0}

        # Next run: FD regresses 701 to TIMED — it must survive via the ledger.
        fetch_highlights.write_fixtures_artifacts({comp: [_rec(701, "TIMED")]})
        fx = load_json_file(fdir / slug / f"{season}.json")["fixtures"]
        assert next(m for m in fx if m["match_id"] == 701)["status"] == "FINISHED"
