"""Tests for the Copa America offline repair and the sync fixes it relies on."""

import copy
import os
import sys
import unittest

os.environ.setdefault("APISPORTS_API_KEY", "test-key")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import repair_copa_tournament as repair  # noqa: E402
from sync_copa_tournament import normalize_group, normalize_knockout  # noqa: E402


def _fix(round_, home, away, ft, et=None, short="FT"):
    return {
        "fixture": {"id": 1, "status": {"short": short}},
        "league": {"round": round_},
        "teams": {"home": {"id": 26, "name": home}, "away": {"id": 1137, "name": away}},
        "score": {"fulltime": {"home": ft[0], "away": ft[1]},
                  "extratime": {"home": et[0], "away": et[1]} if et else
                               {"home": None, "away": None},
                  "penalty": {"home": None, "away": None}},
    }


class TestSyncFixes(unittest.TestCase):
    def test_extra_time_goals_count_in_the_final_score(self):
        # The 2024 Final: 0-0 after 90 minutes, 1-0 after extra time.
        body = {"response": [_fix("Final", "Argentina", "Colombia", (0, 0), et=(1, 0), short="AET")]}
        m = normalize_knockout(body)[0]
        self.assertEqual(m["score"]["fullTime"], {"home": 1, "away": 0})

    def test_no_extra_time_keeps_the_90_minute_score(self):
        body = {"response": [_fix("Semi-finals", "Argentina", "Canada", (2, 0))]}
        self.assertEqual(normalize_knockout(body)[0]["score"]["fullTime"],
                         {"home": 2, "away": 0})

    def test_finished_matches_are_marked_FINISHED(self):
        for short in ("FT", "AET", "PEN"):
            body = {"response": [_fix("Final", "A", "B", (1, 1), short=short)]}
            self.assertEqual(normalize_knockout(body)[0]["status"], "FINISHED", short)
        body = {"response": [_fix("Group Stage - 1", "A", "B", (1, 0))]}
        self.assertEqual(normalize_group(body, {})[0]["status"], "FINISHED")

    def test_unfinished_statuses_pass_through(self):
        body = {"response": [_fix("Final", "A", "B", (None, None), short="NS")]}
        self.assertEqual(normalize_knockout(body)[0]["status"], "NS")


class TestRepairOnCommittedData(unittest.TestCase):
    """Runs the real repair against the committed files (no writes)."""

    def test_committed_data_passes_every_check(self):
        data = repair.build("2026-10-09T00:00:00Z")
        self.assertEqual(len(data["standings"]), 4)
        self.assertEqual(len(data["groupMatches"]), 24)
        self.assertEqual(len(data["matches"]), 8)

    def test_every_match_is_finished_with_an_id_and_a_score(self):
        data = repair.build("2026-10-09T00:00:00Z")
        for m in data["groupMatches"]:
            self.assertEqual(m["status"], "FINISHED")
            self.assertIsNotNone(m["match_id"])
            self.assertIsNotNone(m["score"]["fullTime"]["home"])
        for m in data["matches"]:
            self.assertEqual(m["status"], "FINISHED")
            self.assertIsNotNone(m["id"])

    def test_the_final_has_its_extra_time_score(self):
        data = repair.build("2026-10-09T00:00:00Z")
        final = [m for m in data["matches"] if m["stage"] == "FINAL"][0]
        self.assertEqual((final["homeTeam"]["name"], final["awayTeam"]["name"]),
                         ("Argentina", "Colombia"))
        self.assertEqual(final["score"]["fullTime"], {"home": 1, "away": 0})
        self.assertIsNotNone(final["video_id"])

    def test_highlights_are_attached(self):
        data = repair.build("2026-10-09T00:00:00Z")
        with_video = [m for m in data["groupMatches"] + data["matches"] if m.get("video_id")]
        self.assertEqual(len(with_video), 31, "31 of 32 matches have a clip")

    def test_group_teams_carry_their_api_ids(self):
        data = repair.build("2026-10-09T00:00:00Z")
        arg = [m for m in data["groupMatches"] if m["homeTeam"]["name"] == "Argentina"][0]
        self.assertEqual(arg["homeTeam"]["id"], 26)


class TestRepairRefusesBadData(unittest.TestCase):
    def setUp(self):
        self.current = repair._load(repair.TOURNAMENT)
        self.results = repair._load(repair.RESULTS)["group_stage"]

    def test_a_wrong_score_is_caught_by_the_tables(self):
        bad = copy.deepcopy(self.results)
        bad[0]["score"] = [3, 0]  # Argentina 3-0 Canada (really 2-0)
        with self.assertRaises(repair.RepairError) as ctx:
            repair.check_tables(self.current["standings"], bad)
        self.assertIn("Argentina", str(ctx.exception))

    def test_a_result_without_a_highlight_entry_is_refused(self):
        bad = copy.deepcopy(self.results)
        bad[0]["away"] = "Atlantis"
        with self.assertRaises(repair.RepairError):
            repair.build_group_matches(bad, {1: [], 2: [], 3: []})

    def test_recompute_tables(self):
        t = repair.recompute_tables([{"home": "A", "away": "B", "score": [2, 1]}])
        self.assertEqual(t["A"], dict(p=1, w=1, d=0, l=0, gf=2, ga=1, pts=3))
        self.assertEqual(t["B"], dict(p=1, w=0, d=0, l=1, gf=1, ga=2, pts=0))


if __name__ == "__main__":
    unittest.main()
