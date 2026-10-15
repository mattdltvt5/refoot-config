"""Tournament writers keep FINISHED results the API briefly re-serves unplayed,
and finalize matches left live long after kickoff (like the league fixtures).

Regression for 2026-10-15 00:46 / 01:16 UTC: every UCL game of 13-14 Oct came
back from /competitions/CL as TIMED with a null score, so ucl.json and the Home
index showed them as not started until the next run.
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("APISPORTS_API_KEY", "test_key_copa_fixture")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixture_providers import preserve_finished  # noqa: E402
import sync_copa_tournament  # noqa: E402
from sync_tournaments import finalize_stale_live  # noqa: E402


def _gm(mid, status, home, away, video=None):
    return {"match_id": mid, "status": status, "video_id": video,
            "score": {"fullTime": {"home": home, "away": away}}}


def _ko(mid, status, home, away):
    return {"id": mid, "status": status, "stage": "FINAL", "video_id": None,
            "score": {"fullTime": {"home": home, "away": away}}}


class PreserveFinishedTest(unittest.TestCase):
    def test_flicker_to_timed_keeps_the_result(self):
        prev = {"groupMatches": [_gm(1, "FINISHED", 3, 6, "vid")], "matches": []}
        data = {"groupMatches": [_gm(1, "TIMED", None, None, "vid")], "matches": []}
        self.assertEqual(preserve_finished(data, prev), 1)
        gm = data["groupMatches"][0]
        self.assertEqual(gm["status"], "FINISHED")
        self.assertEqual(gm["score"]["fullTime"], {"home": 3, "away": 6})
        self.assertEqual(gm["video_id"], "vid")

    def test_a_newer_video_id_is_kept_with_the_cached_result(self):
        prev = {"groupMatches": [_gm(1, "FINISHED", 1, 0)]}
        data = {"groupMatches": [_gm(1, "TIMED", None, None, "new")]}
        preserve_finished(data, prev)
        self.assertEqual(data["groupMatches"][0]["video_id"], "new")

    def test_knockout_matches_are_guarded_too(self):
        prev = {"matches": [_ko(9, "FINISHED", 2, 1)]}
        data = {"matches": [_ko(9, "IN_PLAY", 2, 1)]}
        self.assertEqual(preserve_finished(data, prev), 1)
        self.assertEqual(data["matches"][0]["status"], "FINISHED")

    def test_normal_progress_and_corrections_pass_through(self):
        prev = {"groupMatches": [_gm(1, "TIMED", None, None), _gm(2, "FINISHED", 1, 1),
                                 _gm(3, "FINISHED", 0, 0)]}
        data = {"groupMatches": [_gm(1, "FINISHED", 2, 0), _gm(2, "FINISHED", 2, 1),
                                 _gm(3, "POSTPONED", None, None), _gm(4, "TIMED", None, None)]}
        self.assertEqual(preserve_finished(data, prev), 0)
        self.assertEqual([g["status"] for g in data["groupMatches"]],
                         ["FINISHED", "FINISHED", "POSTPONED", "TIMED"])
        self.assertEqual(data["groupMatches"][1]["score"]["fullTime"], {"home": 2, "away": 1})

    def test_no_file_on_disk(self):
        data = {"groupMatches": [_gm(1, "TIMED", None, None)], "matches": []}
        self.assertEqual(preserve_finished(data, {}), 0)



class FinalizeStaleLiveTest(unittest.TestCase):
    NOW = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)

    def _live(self, mid, kickoff, home=1, away=0, idf="match_id"):
        return {idf: mid, "status": "IN_PLAY", "utcDate": kickoff,
                "score": {"fullTime": {"home": home, "away": away}}}

    def test_scored_match_live_4h_after_kickoff_is_finished(self):
        data = {"groupMatches": [self._live(1, "2026-10-14T19:00:00Z")],
                "matches": [self._live(2, "2026-10-14T19:00:00Z", idf="id")]}
        self.assertEqual(finalize_stale_live(data, self.NOW), 2)
        self.assertEqual(data["groupMatches"][0]["status"], "FINISHED")
        self.assertEqual(data["matches"][0]["status"], "FINISHED")
        self.assertEqual(data["groupMatches"][0]["score"]["fullTime"], {"home": 1, "away": 0})

    def test_a_game_still_on_or_scoreless_is_left_alone(self):
        data = {"groupMatches": [self._live(1, "2026-10-15T00:30:00Z"),
                                 self._live(2, "2026-10-14T19:00:00Z", None, None)],
                "matches": []}
        self.assertEqual(finalize_stale_live(data, self.NOW), 0)
        self.assertEqual([g["status"] for g in data["groupMatches"]], ["IN_PLAY", "IN_PLAY"])


class CopaKeepsFinishedTest(unittest.TestCase):
    def test_copa_write_keeps_a_finished_result_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "copa-america.json"
            out.write_text(json.dumps({"matches": [], "groupMatches": [
                _gm(7, "FINISHED", 2, 0, "vid")]}), encoding="utf-8")
            written = {}
            with patch.object(sync_copa_tournament, "OUT_PATH", out),                  patch.object(sync_copa_tournament, "ApisportsQuotaTracker"),                  patch.object(sync_copa_tournament, "ApiSportsProvider"),                  patch.object(sync_copa_tournament, "fetch_standings", return_value=[{"g": 1}]),                  patch.object(sync_copa_tournament, "_build_team_group_map", return_value={}),                  patch.object(sync_copa_tournament, "normalize_knockout", return_value=[]),                  patch.object(sync_copa_tournament, "normalize_group",
                              return_value=[_gm(7, "NS", None, None)]),                  patch.object(sync_copa_tournament, "write_tournament",
                              side_effect=lambda g, m, gm: written.update(gm=gm)):
                sync_copa_tournament.main()
            self.assertEqual(written["gm"][0]["status"], "FINISHED")
            self.assertEqual(written["gm"][0]["score"]["fullTime"], {"home": 2, "away": 0})


if __name__ == "__main__":
    unittest.main()
