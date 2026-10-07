"""Tests for scripts/commit_gate.py - skip timestamp-only commits, keep a heartbeat."""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from commit_gate import decide, strip_volatile  # noqa: E402

NOW = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _fixtures(stamp, score=None):
    return json.dumps({"generated_at": stamp,
                       "fixtures": [{"id": 1, "score": score}]})


class TestCommitGate(unittest.TestCase):
    def test_timestamp_only_recent_is_skipped(self):
        verdict, _ = decide(
            [("fixtures/pl/2026.json", _fixtures(_iso(0.5)), _fixtures(_iso(0)))],
            NOW, 3)
        self.assertEqual(verdict, "skip")

    def test_real_data_change_commits(self):
        verdict, _ = decide(
            [("fixtures/pl/2026.json", _fixtures(_iso(0.1), None),
              _fixtures(_iso(0), {"home": 2, "away": 1}))],
            NOW, 3)
        self.assertEqual(verdict, "commit")

    def test_heartbeat_commits_when_data_getting_old(self):
        # The app rejects fixtures older than 6 h, so 3 h must refresh them.
        verdict, reason = decide(
            [("fixtures/pl/2026.json", _fixtures(_iso(3.2)), _fixtures(_iso(0)))],
            NOW, 3)
        self.assertEqual(verdict, "commit")
        self.assertIn("heartbeat", reason)

    def test_heartbeat_uses_oldest_file(self):
        verdict, _ = decide(
            [("a.json", _fixtures(_iso(0.2)), _fixtures(_iso(0))),
             ("b.json", _fixtures(_iso(4)), _fixtures(_iso(0)))],
            NOW, 3)
        self.assertEqual(verdict, "commit")

    def test_nested_and_other_volatile_keys_ignored(self):
        head = json.dumps({"last_run": _iso(1), "meta": {"last_updated": _iso(1)},
                           "counts": {"2026-10-07": 50}})
        staged = json.dumps({"last_run": _iso(0), "meta": {"last_updated": _iso(0)},
                             "counts": {"2026-10-07": 50}})
        self.assertEqual(decide([("highlights/quota-tracker.json", head, staged)],
                                NOW, 3)[0], "skip")

    def test_quota_count_change_is_real(self):
        head = json.dumps({"last_updated": _iso(1), "counts": {"2026-10-07": 50}})
        staged = json.dumps({"last_updated": _iso(0), "counts": {"2026-10-07": 150}})
        self.assertEqual(decide([("highlights/quota-tracker.json", head, staged)],
                                NOW, 3)[0], "commit")

    def test_new_or_deleted_file_commits(self):
        self.assertEqual(decide([("x.json", None, "{}")], NOW, 3)[0], "commit")
        self.assertEqual(decide([("x.json", "{}", None)], NOW, 3)[0], "commit")

    def test_non_json_and_bad_json_commit(self):
        self.assertEqual(decide([("x.txt", "a", "b")], NOW, 3)[0], "commit")
        self.assertEqual(decide([("x.json", "{", "{}")], NOW, 3)[0], "commit")

    def test_no_timestamp_at_head_commits(self):
        self.assertEqual(decide([("x.json", '{"a": 1}', '{"a": 1}')], NOW, 3)[0],
                         "commit")

    def test_nothing_staged_skips(self):
        self.assertEqual(decide([], NOW, 3)[0], "skip")

    def test_strip_volatile_keeps_data(self):
        self.assertEqual(strip_volatile({"generated_at": "x", "a": [{"_updated": 1, "b": 2}]}),
                         {"a": [{"b": 2}]})


class TestWorkflowUsesGate(unittest.TestCase):
    def test_fetch_highlights_commit_step_is_gated(self):
        wf = os.path.join(os.path.dirname(__file__), "..", "..", ".github",
                          "workflows", "fetch-highlights.yml")
        text = open(wf, encoding="utf-8").read()
        self.assertIn("scripts/commit_gate.py", text)


if __name__ == "__main__":
    unittest.main()
