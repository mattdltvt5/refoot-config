"""Tests for Copa America event backfill: API-Sports event normalization and the
build_events assembly. No network — the provider's HTTP is stubbed."""

import sys
import pathlib
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fixture_providers import ApiSportsProvider
import backfill_copa_events as bce


# A representative API-Sports /fixtures/events "response" payload.
_RAW_EVENTS = [
    {"time": {"elapsed": 22, "extra": None}, "team": {"id": 26, "name": "Argentina"},
     "player": {"id": 1, "name": "L. Messi"}, "type": "Goal", "detail": "Normal Goal"},
    {"time": {"elapsed": 45, "extra": 2}, "team": {"id": 6, "name": "Brazil"},
     "player": {"id": 2, "name": "Vinicius"}, "type": "Card", "detail": "Yellow Card"},
    {"time": {"elapsed": 105, "extra": None}, "team": {"id": 26, "name": "Argentina"},
     "player": {"id": 3, "name": "L. Martinez"}, "type": "Goal", "detail": "Normal Goal"},
    {"time": {"elapsed": 70, "extra": None}, "team": {"id": 6, "name": "Brazil"},
     "player": {"id": 4, "name": "Casemiro"}, "type": "subst", "detail": "Substitution 1"},
    {"time": {"elapsed": 60, "extra": None}, "team": {"id": 26, "name": "Argentina"},
     "player": {"id": 5, "name": "Otamendi"}, "type": "Card", "detail": "Red Card"},
]


class TestNormalizeEvents(unittest.TestCase):
    def test_keeps_only_goals_and_cards(self):
        out = ApiSportsProvider._normalize_events(_RAW_EVENTS)
        kinds = [e["kind"] for e in out]
        self.assertEqual(kinds.count("goal"), 2)
        self.assertEqual(kinds.count("card"), 2)
        self.assertNotIn("subst", kinds)          # substitution filtered out
        self.assertEqual(len(out), 4)

    def test_extra_time_phase(self):
        out = ApiSportsProvider._normalize_events(_RAW_EVENTS)
        goal105 = next(e for e in out if e["minute"] == 105)
        goal22 = next(e for e in out if e["minute"] == 22)
        self.assertEqual(goal105["phase"], "extra")   # prorroga
        self.assertEqual(goal22["phase"], "regular")

    def test_fields_and_detail_preserved(self):
        out = ApiSportsProvider._normalize_events(_RAW_EVENTS)
        g = next(e for e in out if e["player"] == "L. Messi")
        self.assertEqual(g["kind"], "goal")
        self.assertEqual(g["team"], "Argentina")
        self.assertEqual(g["detail"], "Normal Goal")
        r = next(e for e in out if e["detail"] == "Red Card")
        self.assertEqual(r["kind"], "card")
        self.assertEqual(r["player"], "Otamendi")

    def test_sorted_by_minute_then_extra(self):
        out = ApiSportsProvider._normalize_events(_RAW_EVENTS)
        minutes = [e["minute"] for e in out]
        self.assertEqual(minutes, sorted(minutes))

    def test_empty_and_none(self):
        self.assertEqual(ApiSportsProvider._normalize_events([]), [])
        self.assertEqual(ApiSportsProvider._normalize_events(None), [])


class TestBuildEvents(unittest.TestCase):
    def test_assembles_events_by_match_id_and_omits_empty(self):
        provider = MagicMock()
        provider.get_fixtures.return_value = {
            "matchday-1": [{"match_id": 1001}, {"match_id": 1002}],
            "final":      [{"match_id": 2001}],
        }
        # 1001 has events, 1002 has none, 2001 has one goal
        provider.get_events.side_effect = lambda mid: {
            1001: [{"kind": "goal", "minute": 10, "extra": None, "phase": "regular",
                    "team": "A", "player": "X", "detail": "Normal Goal"}],
            1002: [],
            2001: [{"kind": "goal", "minute": 118, "extra": None, "phase": "extra",
                    "team": "B", "player": "Y", "detail": "Penalty"}],
        }[mid]

        events, total = bce.build_events(provider, {})
        self.assertEqual(total, 3)
        self.assertEqual(set(events.keys()), {"1001", "2001"})   # 1002 omitted (no events)
        self.assertEqual(events["2001"][0]["phase"], "extra")

    def test_no_fixtures_returns_none(self):
        provider = MagicMock()
        provider.get_fixtures.return_value = {}
        events, total = bce.build_events(provider, {})
        self.assertIsNone(events)
        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
