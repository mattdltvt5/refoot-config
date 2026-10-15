"""sync_tournaments keeps FINISHED results the API briefly re-serves unplayed.

Regression for 2026-10-15 00:46 / 01:16 UTC: every UCL game of 13-14 Oct came
back from /competitions/CL as TIMED with a null score, so ucl.json and the Home
index showed them as not started until the next run.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sync_tournaments import preserve_finished  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
