"""A game under way is never sent back to "not started", and an older copy of a
match never replaces a newer one.

Regression for 2026-10-24: football-data.org alternated IN_PLAY-with-score and
TIMED-with-no-score copies of the same match every few minutes, and left
Valencia v Villarreal, Rayo v Elche, Atletico v Deportivo and Napoli v Roma at
TIMED/null for up to 10 h after they ended (it finalised them at 00:20Z), so the
app showed live games as not started.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fixture_providers import (  # noqa: E402
    HOLD_LIVE_FOR,
    merge_fixtures_preserving_finished,
    merge_preserve_finished,
    preserve_finished,
)
from sync_tournaments import finalize_stale_live  # noqa: E402

KO = "2026-10-24T16:30:00Z"
NOW = datetime(2026, 10, 24, 18, 40, tzinfo=timezone.utc)


def fx(status, home=None, away=None, *, mid=564722, ko=KO, updated=None):
    r = {"match_id": mid, "status": status, "utcDate": ko,
         "score": {"fullTime": {"home": home, "away": away}}}
    if updated:
        r["lastUpdated"] = updated
    return r


class LiveHoldTest(unittest.TestCase):
    def test_live_with_score_is_not_sent_back_to_not_started(self):
        kept = merge_preserve_finished(fx("IN_PLAY", 1, 1), fx("TIMED"), NOW)
        self.assertEqual((kept["status"], kept["score"]["fullTime"]),
                         ("IN_PLAY", {"home": 1, "away": 1}))

    def test_real_changes_still_flow(self):
        live = fx("IN_PLAY", 1, 0)
        for incoming in (fx("IN_PLAY", 0, 0),          # a disallowed goal
                         fx("PAUSED", 1, 0),           # half-time
                         fx("FINISHED", 1, 2),         # full time
                         fx("POSTPONED"),              # called off
                         fx("TIMED", ko="2026-10-31T16:30:00Z")):  # moved later
            self.assertIs(merge_preserve_finished(live, incoming, NOW), incoming,
                          incoming["status"])

    def test_no_hold_before_kickoff_or_without_a_score(self):
        early = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(merge_preserve_finished(fx("IN_PLAY", 0, 0), fx("TIMED"), early)["status"],
                         "TIMED")
        self.assertEqual(merge_preserve_finished(fx("IN_PLAY"), fx("TIMED"), NOW)["status"],
                         "TIMED")

    def test_hold_ends_after_hold_live_for(self):
        late = datetime(2026, 10, 24, 16, 30, tzinfo=timezone.utc) + HOLD_LIVE_FOR
        self.assertEqual(merge_preserve_finished(fx("IN_PLAY", 1, 1), fx("TIMED"), late)["status"],
                         "TIMED")


class LastUpdatedTest(unittest.TestCase):
    def test_an_older_copy_never_replaces_a_newer_one(self):
        cached = fx("IN_PLAY", 2, 1, updated="2026-10-24T17:36:00Z")
        older = fx("IN_PLAY", 1, 0, updated="2026-10-24T17:20:00Z")
        self.assertIs(merge_preserve_finished(cached, older, NOW), cached)

    def test_a_newer_copy_wins(self):
        cached = fx("IN_PLAY", 1, 0, updated="2026-10-24T17:20:00Z")
        newer = fx("IN_PLAY", 2, 1, updated="2026-10-24T17:36:00Z")
        self.assertIs(merge_preserve_finished(cached, newer, NOW), newer)

    def test_finished_records_ignore_an_older_snapshot(self):
        # 2026-10-26: two upstream snapshots of the same finished match in turn.
        newer = fx("FINISHED", 1, 2, updated="2026-10-26T10:20:30Z")
        older = fx("FINISHED", 1, 2, updated="2026-10-25T00:20:24Z")
        self.assertIs(merge_preserve_finished(newer, older, NOW), newer)
        self.assertIs(merge_preserve_finished(older, newer, NOW), newer)
        # ...and a newer score correction still lands.
        corrected = fx("FINISHED", 2, 2, updated="2026-10-26T11:00:00Z")
        self.assertIs(merge_preserve_finished(newer, corrected, NOW), corrected)

    def test_alternating_snapshots_settle_on_the_newest(self):
        a = fx("FINISHED", 0, 3, updated="2026-10-26T10:20:30Z")
        b = fx("FINISHED", 0, 3, updated="2026-10-25T00:20:24Z")
        cached = b
        for incoming in (a, b, a, b, b, a):
            cached = merge_preserve_finished(cached, incoming, NOW)
            if cached is a:
                break
        for incoming in (b, a, b, b):
            cached = merge_preserve_finished(cached, incoming, NOW)
            self.assertIs(cached, a)

    def test_missing_timestamps_fall_back_to_the_other_rules(self):
        self.assertEqual(merge_preserve_finished(fx("TIMED"), fx("IN_PLAY", 0, 0), NOW)["status"],
                         "IN_PLAY")


class Oct24ReplayTest(unittest.TestCase):
    """The cached copy for each 5-minute run, as published on 2026-10-24."""

    def replay(self, sequence, ko):
        shown = []
        cached = None
        for t, status, home, away in sequence:
            now = datetime.fromisoformat(t.replace("Z", "+00:00"))
            incoming = fx(status, home, away, ko=ko)
            cached = merge_preserve_finished(cached, incoming, now) if cached else incoming
            shown.append((cached["status"], cached["score"]["fullTime"]["home"],
                          cached["score"]["fullTime"]["away"]))
        return shown

    def test_valencia_villarreal(self):
        seq = [("2026-10-24T08:01:00Z", "TIMED", None, None),
               ("2026-10-24T17:36:00Z", "IN_PLAY", 1, 0),
               ("2026-10-24T17:41:00Z", "TIMED", None, None),
               ("2026-10-24T17:56:00Z", "IN_PLAY", 1, 0),
               ("2026-10-24T18:01:00Z", "TIMED", None, None),
               ("2026-10-24T18:31:00Z", "IN_PLAY", 1, 1),
               ("2026-10-24T18:36:00Z", "TIMED", None, None),
               ("2026-10-25T00:26:00Z", "FINISHED", 1, 2)]
        shown = self.replay(seq, "2026-10-24T16:30:00Z")
        # Once live, never "not started" again; the real final still lands.
        self.assertNotIn(("TIMED", None, None), shown[1:])
        self.assertEqual(shown[6], ("IN_PLAY", 1, 1))
        self.assertEqual(shown[-1], ("FINISHED", 1, 2))

    def test_rayo_elche_scoreless_live_copies_do_not_wipe_the_score(self):
        seq = [("2026-10-24T13:56:00Z", "IN_PLAY", 0, 3),
               ("2026-10-24T14:01:00Z", "TIMED", None, None),
               ("2026-10-24T22:46:00Z", "IN_PLAY", None, None),
               ("2026-10-24T22:56:00Z", "TIMED", None, None),
               ("2026-10-25T00:26:00Z", "FINISHED", 0, 3)]
        shown = self.replay(seq, "2026-10-24T12:00:00Z")
        self.assertEqual(shown[1], ("IN_PLAY", 0, 3))
        self.assertEqual(shown[-1], ("FINISHED", 0, 3))


class HeldLiveIsNeverFinalisedTest(unittest.TestCase):
    """A held copy may be out of date (Valencia's last live copy was 1-1, the
    final 1-2): only what upstream ITSELF still serves live is finalised."""

    def test_league_write_path(self):
        # Imported here and unloaded after: find_channel_candidates' guard test
        # requires fetch_highlights to be absent from sys.modules.
        import fetch_highlights
        self.addCleanup(sys.modules.pop, "fetch_highlights", None)
        stale_now = datetime(2026, 10, 24, 21, 0, tzinfo=timezone.utc)  # KO + 4.5 h
        with tempfile.TemporaryDirectory() as tmp:
            fixtures_dir, results_dir = Path(tmp, "fixtures"), Path(tmp, "results")
            (fixtures_dir / "laliga").mkdir(parents=True)
            (fixtures_dir / "laliga" / "2026.json").write_text(
                json.dumps({"fixtures": [fx("IN_PLAY", 1, 1)]}), encoding="utf-8")
            real_coerce = fetch_highlights.coerce_stale_live_to_finished
            with patch.object(fetch_highlights, "FIXTURES_DIR", fixtures_dir), \
                 patch.object(fetch_highlights, "RESULTS_DIR", results_dir), \
                 patch.object(fetch_highlights, "season_for_competition", return_value=2026), \
                 patch.object(fetch_highlights, "coerce_stale_live_to_finished",
                              side_effect=lambda f: real_coerce(f, stale_now)), \
                 patch("fixture_providers.datetime") as dt:
                dt.now.return_value = stale_now
                dt.fromisoformat = datetime.fromisoformat
                fetch_highlights.write_fixtures_artifacts({"LaLiga": [fx("TIMED")]})
            written = json.loads((fixtures_dir / "laliga" / "2026.json").read_text(encoding="utf-8"))
        rec = written["fixtures"][0]
        self.assertEqual((rec["status"], rec["score"]["fullTime"]),
                         ("IN_PLAY", {"home": 1, "away": 1}),
                         "held live, not turned into a 1-1 final")

    def test_tournament_order_finalises_incoming_only(self):
        stale_now = datetime(2026, 10, 24, 21, 0, tzinfo=timezone.utc)
        data = {"matches": [], "groupMatches": [fx("TIMED")]}
        prev = {"matches": [], "groupMatches": [fx("IN_PLAY", 1, 1)]}
        self.assertEqual(finalize_stale_live(data, stale_now), 0)
        preserve_finished(data, prev, stale_now)
        self.assertEqual(data["groupMatches"][0]["status"], "IN_PLAY")

    def test_upstream_stuck_live_is_still_finalised(self):
        stale_now = datetime(2026, 10, 24, 21, 0, tzinfo=timezone.utc)
        data = {"matches": [], "groupMatches": [fx("IN_PLAY", 2, 0)]}
        self.assertEqual(finalize_stale_live(data, stale_now), 1)
        self.assertEqual(data["groupMatches"][0]["status"], "FINISHED")


class MergeListTest(unittest.TestCase):
    def test_list_merge_holds_by_match_id(self):
        cached = [fx("IN_PLAY", 1, 1, mid=1), fx("TIMED", mid=2)]
        incoming = [fx("TIMED", mid=1), fx("IN_PLAY", 0, 0, mid=2)]
        out = merge_fixtures_preserving_finished(cached, incoming, NOW)
        self.assertEqual([r["status"] for r in out], ["IN_PLAY", "IN_PLAY"])


if __name__ == "__main__":
    unittest.main()
