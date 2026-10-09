"""Tests for scripts/push_events.py - push-notification event detection (log-only)."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import push_events  # noqa: E402
from push_events import FINAL_SETTLE, flatten, months_around, update_ledger  # noqa: E402


def detect(prev, curr, now, ledger):
    """Events only (the marks are covered by the replay tests)."""
    return push_events.detect(prev, curr, now, ledger)[0]

KO = datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc)


def _m(status, h=None, a=None, video=None, mid=1, ko=KO):
    m = {
        "match_id": mid,
        "homeTeam": {"id": 62, "name": "Everton FC"},
        "awayTeam": {"id": 397, "name": "Brighton & Hove Albion FC"},
        "homeScore": h,
        "awayScore": a,
        "status": status,
        "utcDate": ko.isoformat().replace("+00:00", "Z"),
        "competition": "premier-league",
    }
    if video:
        m["videoId"] = video
    return m


def _at(minutes):
    return KO + timedelta(minutes=minutes)


def _types(events):
    return [(e["type"], tuple(e["score"])) for e in events]


class TestDetect(unittest.TestCase):
    def _replay(self, states):
        ledger, prev, seen = {}, {}, []
        for minute, state in states:
            now = _at(minute)
            evs, marks = push_events.detect(prev, {1: state}, now, ledger)
            ledger = update_ledger(ledger, evs, now, marks)
            seen += [(minute, *t) for t in _types(evs)]
            prev = {1: state}
        return seen

    def test_replays_a_real_match(self):
        """Everton 19 Sep 2026: 1-0 at 14:21, 2-0 at 15:26, FT 16:07, corrected to
        1-0 at 16:17 (observed in the pipeline's git history). The result waits
        for the correction instead of announcing 2-0."""
        seen = self._replay([
            (11, _m("TIMED")),
            (21, _m("IN_PLAY", 1, 0)),
            (26, _m("IN_PLAY", 1, 0)),
            (86, _m("IN_PLAY", 2, 0)),
            (127, _m("FINISHED", 2, 0)),
            (132, _m("FINISHED", 2, 0)),
            (137, _m("FINISHED", 1, 0)),
            (142, _m("FINISHED", 1, 0)),
            (147, _m("FINISHED", 1, 0)),
            (152, _m("FINISHED", 1, 0)),
            (157, _m("FINISHED", 1, 0)),
        ])
        self.assertEqual(seen, [
            (21, "goal", (1, 0)),
            (86, "goal", (2, 0)),
            (137, "disallowed", (1, 0)),   # the post-whistle correction
            (152, "final", (1, 0)),        # 15 min after the corrected score
        ])

    def test_first_sighting_already_scored_is_a_goal(self):
        evs = detect({}, {1: _m("IN_PLAY", 1, 0)}, _at(11), {})
        self.assertEqual(_types(evs), [("goal", (1, 0))])

    def test_two_goals_between_runs_is_one_event_with_the_new_score(self):
        evs = detect({1: _m("IN_PLAY", 0, 0)}, {1: _m("IN_PLAY", 2, 0)}, _at(30), {})
        self.assertEqual(_types(evs), [("goal", (2, 0))])

    def test_no_goal_for_0_0_kickoff(self):
        self.assertEqual(detect({1: _m("TIMED")}, {1: _m("IN_PLAY", 0, 0)}, _at(5), {}), [])

    def test_ledger_prevents_duplicates(self):
        ledger = {"1:goal:1-0": "2026-09-19T14:21:00Z"}
        # A flicker (score lost, then back) must not re-announce the goal.
        evs = detect({1: _m("IN_PLAY")}, {1: _m("IN_PLAY", 1, 0)}, _at(40), ledger)
        self.assertEqual(evs, [])

    def test_final_waits_for_the_score_to_settle(self):
        ft = _m("FINISHED", 2, 1)
        seen = self._replay([(105, _m("IN_PLAY", 2, 1)), (110, ft), (115, ft), (120, ft), (125, ft)])
        self.assertEqual(seen, [(105, "goal", (2, 1)), (125, "final", (2, 1))])
        self.assertEqual(FINAL_SETTLE.total_seconds(), 15 * 60)

    def test_final_announced_once(self):
        ledger = {"1:final": "2026-09-19T15:55:00Z", "1:ft:2-1": "2026-09-19T15:40:00Z"}
        self.assertEqual(
            detect({1: _m("FINISHED", 2, 1)}, {1: _m("FINISHED", 2, 1)}, _at(120), ledger), [])

    def test_highlights_when_a_video_is_attached(self):
        evs = detect({1: _m("FINISHED", 2, 1)},
                     {1: _m("FINISHED", 2, 1, video="abc")}, _at(25 * 60), {"1:final": "x"})
        self.assertEqual([e["type"] for e in evs], ["highlights"])
        self.assertEqual(evs[0]["video_id"], "abc")

    def test_old_matches_are_ignored(self):
        # Goals/finals only within 6 h of kickoff; highlights within 72 h, so a
        # backfill of old clips can't flood anyone.
        self.assertEqual(detect({}, {1: _m("FINISHED", 3, 0)}, _at(7 * 60), {}), [])
        self.assertEqual(
            detect({1: _m("FINISHED", 3, 0)}, {1: _m("FINISHED", 3, 0, video="v")},
                   _at(73 * 60), {}), [])

    def test_future_and_unscheduled_matches_are_ignored(self):
        future = _m("TIMED", ko=KO + timedelta(days=1))
        self.assertEqual(detect({}, {1: future}, _at(0), {}), [])

    def test_unchanged_state_emits_nothing(self):
        live = {1: _m("IN_PLAY", 1, 0)}
        self.assertEqual(detect(live, live, _at(30), {"1:goal:1-0": "x"}), [])

    def test_event_payload_has_teams_and_timing(self):
        e = detect({}, {1: _m("IN_PLAY", 1, 0)}, _at(21), {})[0]
        self.assertEqual(e["key"], "1:goal:1-0")
        self.assertEqual(e["home"], {"id": 62, "name": "Everton FC"})
        self.assertEqual(e["away"]["id"], 397)
        self.assertEqual(e["competition"], "premier-league")
        self.assertEqual(e["minutes_after_kickoff"], 21)
        self.assertEqual(e["detected_at"], "2026-09-19T14:21:00Z")


class TestHelpers(unittest.TestCase):
    def test_flatten_accepts_both_shapes(self):
        groups = [{"competition": "ucl", "matches": [_m("IN_PLAY", 0, 0, mid=7)]}]
        self.assertIn(7, flatten({"2026-09": {"2026-09-19": groups}}))
        self.assertIn(7, flatten({"2026-09": {"month": "2026-09",
                                              "dates": {"2026-09-19": groups}}}))
        self.assertEqual(flatten({"2026-09": {"2026-09-19": groups}})[7]["competition"], "ucl")

    def test_ledger_prunes_old_keys(self):
        now = _at(0)
        ledger = {"old": "2026-09-10T00:00:00Z", "new": "2026-09-19T10:00:00Z"}
        out = update_ledger(ledger, [{"key": "k", "detected_at": "2026-09-19T14:00:00Z"}], now)
        self.assertEqual(set(out), {"new", "k"})

    def test_months_around_spans_a_month_boundary(self):
        self.assertEqual(months_around(datetime(2026, 10, 1, tzinfo=timezone.utc)),
                         ["2026-09", "2026-10"])
        self.assertEqual(months_around(datetime(2027, 1, 15, tzinfo=timezone.utc)),
                         ["2026-12", "2027-01"])


class TestRun(unittest.TestCase):
    def test_log_mode_records_events_and_ledger_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            events_dir = Path(d) / "push-events"
            with mock.patch.object(push_events, "EVENTS_DIR", events_dir), \
                 mock.patch.object(push_events, "LEDGER_PATH", events_dir / "ledger.json"), \
                 mock.patch.object(push_events, "load_head_state",
                                   return_value={1: _m("IN_PLAY", 0, 0)}), \
                 mock.patch.object(push_events, "load_current_state",
                                   return_value={1: _m("IN_PLAY", 1, 0)}):
                evs = push_events.run("log", now=_at(21))
                self.assertEqual(_types(evs), [("goal", (1, 0))])
                lines = (events_dir / "events-2026-09.jsonl").read_text(encoding="utf-8").splitlines()
                self.assertEqual(json.loads(lines[0])["key"], "1:goal:1-0")
                self.assertIn("1:goal:1-0", json.loads((events_dir / "ledger.json").read_text()))
                # Same state again: nothing new appended.
                self.assertEqual(push_events.run("log", now=_at(26)), [])
                self.assertEqual(len((events_dir / "events-2026-09.jsonl")
                                     .read_text(encoding="utf-8").splitlines()), 1)

    def test_errors_never_fail_the_pipeline(self):
        with mock.patch.object(push_events, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(push_events.main([]), 0)



class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {"sent": 0, "duplicate": 0, "invalid": 0}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _Http:
    """Fake `requests`: records posts, answers with [status]."""

    def __init__(self, status=200):
        self.status = status
        self.posts = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "events": json["events"], "headers": headers})
        n = len(json["events"])
        return _Resp(self.status, {"sent": n, "duplicate": 0, "invalid": 0})


class TestSend(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        events_dir = Path(self._dir.name) / "push-events"
        self._patches = [
            mock.patch.object(push_events, "EVENTS_DIR", events_dir),
            mock.patch.object(push_events, "LEDGER_PATH", events_dir / "ledger.json"),
            mock.patch.object(push_events, "PENDING_PATH", events_dir / "pending.json"),
            mock.patch.object(push_events, "load_head_state",
                              return_value={1: _m("IN_PLAY", 0, 0)}),
            mock.patch.object(push_events, "load_current_state",
                              return_value={1: _m("IN_PLAY", 1, 0)}),
            mock.patch.dict(os.environ, {"PUSH_EVENTS_SECRET": "s3cret"}),
        ]
        for p in self._patches:
            p.start()
        self.pending = events_dir / "pending.json"

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._dir.cleanup()

    def test_send_posts_events_with_the_secret(self):
        http = _Http()
        push_events.run("send", now=_at(21), http=http)
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(http.posts[0]["headers"], {"Authorization": "Bearer s3cret"})
        self.assertEqual(http.posts[0]["events"][0]["key"], "1:goal:1-0")
        self.assertEqual(json.loads(self.pending.read_text()), [])

    def test_a_failed_post_is_retried_next_run(self):
        push_events.run("send", now=_at(21), http=_Http(status=500))
        self.assertEqual([e["key"] for e in json.loads(self.pending.read_text())],
                         ["1:goal:1-0"])
        # Next run: no new events (already in the ledger), the pending one is sent.
        http = _Http()
        push_events.run("send", now=_at(26), http=http)
        self.assertEqual([e["key"] for e in http.posts[0]["events"]], ["1:goal:1-0"])
        self.assertEqual(json.loads(self.pending.read_text()), [])

    def test_stale_unsent_events_are_dropped(self):
        push_events.run("send", now=_at(21), http=_Http(status=500))
        http = _Http()
        push_events.run("send", now=_at(21 + 45), http=http)  # 45 min later
        self.assertEqual(http.posts, [], "a 45-minute-old goal alert isn't sent")
        self.assertEqual(json.loads(self.pending.read_text()), [])

    def test_prune_keeps_highlights_longer_than_goals(self):
        now = _at(0)
        old = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        kept = push_events.prune_pending(
            [{"type": "goal", "detected_at": old}, {"type": "highlights", "detected_at": old}],
            now)
        self.assertEqual([e["type"] for e in kept], ["highlights"])

    def test_large_batches_are_chunked(self):
        http = _Http()
        events = [{"key": str(i)} for i in range(push_events.SEND_CHUNK * 2 + 5)]
        total = push_events.post_events(events, "u", "s", http=http)
        self.assertEqual([len(p["events"]) for p in http.posts],
                         [push_events.SEND_CHUNK, push_events.SEND_CHUNK, 5])
        self.assertEqual(total["sent"], len(events))

    def test_without_the_secret_send_falls_back_to_log(self):
        http = _Http()
        with mock.patch.dict(os.environ, {"PUSH_EVENTS_SECRET": ""}):
            evs = push_events.run("send", now=_at(21), http=http)
        self.assertEqual(len(evs), 1, "still detected and logged")
        self.assertEqual(http.posts, [])
        self.assertFalse(self.pending.exists())

    def test_log_mode_never_posts(self):
        http = _Http()
        push_events.run("log", now=_at(21), http=http)
        self.assertEqual(http.posts, [])

    def test_send_mode_errors_never_fail_the_pipeline(self):
        with mock.patch.object(push_events, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(push_events.main(["--mode", "send"]), 0)


if __name__ == "__main__":
    unittest.main()
