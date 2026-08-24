"""Tests for scripts/sync_tournaments.py — the frequent tournament-cache refresher.

All tests are pure-function / mocked-HTTP — no live FD or API-Sports calls.

Covers:
  - Verbatim FD score+status passthrough for finished games (incl. penalty-
    shootout and extra-time), and null preservation for unplayed games.
  - video_id graft (existing preservation + local highlights lookup), generic
    across all slugs including Copa América (whose scores are left untouched).
  - fetch endpoints hit /matches with NO status filter (all statuses needed).
  - Workflow consolidation: sync-teams.yml no longer writes tournament-groups
    (no double-write) and fetch-highlights.yml drives sync_tournaments.py.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sync_tournaments import (  # noqa: E402
    build_group_matches,
    build_tournament_data,
    fetch_matches,
    fetch_standings,
    graft_video_ids,
    read_existing_video_ids,
    write_tournament,
)

_WORKFLOWS = os.path.join(
    os.path.dirname(__file__), "..", "..", ".github", "workflows"
)


# ── Sample FD match objects (verbatim football-data.org /matches shapes) ────────

# Finished, decided in regulation.
_MATCH_REGULAR = {
    "id": 537417,
    "utcDate": "2026-06-28T19:00:00Z",
    "status": "FINISHED",
    "stage": "LAST_32",
    "homeTeam": {"id": 774, "name": "South Africa", "tla": "RSA", "crest": "rsa.svg"},
    "awayTeam": {"id": 828, "name": "Canada", "tla": "CAN", "crest": "can.svg"},
    "score": {
        "winner": "AWAY_TEAM",
        "duration": "REGULAR",
        "fullTime": {"home": 0, "away": 1},
        "halfTime": {"home": 0, "away": 0},
    },
}

# Finished on penalties — FD keeps the aggregate in fullTime, the draw in
# regularTime, and the shootout tally in penalties.  Every field must survive.
_MATCH_PENALTY = {
    "id": 537415,
    "utcDate": "2026-06-29T20:30:00Z",
    "status": "FINISHED",
    "stage": "LAST_32",
    "homeTeam": {"id": 759, "name": "Germany", "tla": "GER", "crest": "ger.svg"},
    "awayTeam": {"id": 764, "name": "Paraguay", "tla": "PAR", "crest": "par.svg"},
    "score": {
        "winner": "AWAY_TEAM",
        "duration": "PENALTY_SHOOTOUT",
        "fullTime": {"home": 4, "away": 5},
        "halfTime": {"home": 0, "away": 1},
        "regularTime": {"home": 1, "away": 1},
        "extraTime": {"home": 0, "away": 0},
        "penalties": {"home": 3, "away": 4},
    },
}

# Finished after extra time.
_MATCH_EXTRA_TIME = {
    "id": 537422,
    "utcDate": "2026-07-01T20:00:00Z",
    "status": "FINISHED",
    "stage": "LAST_32",
    "homeTeam": {"id": 805, "name": "Belgium", "tla": "BEL", "crest": "bel.svg"},
    "awayTeam": {"id": 765, "name": "Senegal", "tla": "SEN", "crest": "sen.svg"},
    "score": {
        "winner": "HOME_TEAM",
        "duration": "EXTRA_TIME",
        "fullTime": {"home": 3, "away": 2},
        "halfTime": {"home": 0, "away": 1},
        "regularTime": {"home": 2, "away": 2},
        "extraTime": {"home": 1, "away": 0},
    },
}

# Not yet played — kickoff after the last refresh.  Score must stay null.
_MATCH_TIMED = {
    "id": 537420,
    "utcDate": "2026-07-02T19:00:00Z",
    "status": "TIMED",
    "stage": "LAST_32",
    "homeTeam": {"id": 760, "name": "Spain", "tla": "ESP", "crest": "esp.svg"},
    "awayTeam": {"id": 816, "name": "Austria", "tla": "AUT", "crest": "aut.svg"},
    "score": {
        "winner": None,
        "duration": "REGULAR",
        "fullTime": {"home": None, "away": None},
        "halfTime": {"home": None, "away": None},
    },
}

# Group-stage fixture — must land in groupMatches, not matches.
_MATCH_GROUP = {
    "id": 600001,
    "utcDate": "2026-06-14T19:00:00Z",
    "status": "FINISHED",
    "stage": "GROUP_STAGE",
    "group": "Group A",
    "matchday": 1,
    "homeTeam": {"id": 1, "name": "Mexico", "tla": "MEX", "crest": "mex.svg"},
    "awayTeam": {"id": 2, "name": "Ecuador", "tla": "ECU", "crest": "ecu.svg"},
    "score": {
        "winner": "HOME_TEAM",
        "duration": "REGULAR",
        "fullTime": {"home": 2, "away": 0},
        "halfTime": {"home": 1, "away": 0},
    },
}

_STANDINGS_PAYLOAD = {
    "standings": [
        {"type": "HOME", "group": "GROUP_A", "table": [{"position": 1}]},
        {"type": "AWAY", "group": "GROUP_A", "table": [{"position": 1}]},
        {"type": "TOTAL", "group": "GROUP_A", "table": [{"position": 1}, {"position": 2}]},
        {"type": "TOTAL", "group": "GROUP_B", "table": [{"position": 1}]},
    ]
}


def _matches_payload(*matches):
    return {"matches": list(matches)}


# ── Verbatim knockout passthrough ──────────────────────────────────────────────


class TestBuildTournamentDataPassthrough(unittest.TestCase):
    def test_finished_regular_score_and_status_verbatim(self):
        data = build_tournament_data(
            "world-cup", _STANDINGS_PAYLOAD, _matches_payload(_MATCH_REGULAR)
        )
        self.assertEqual(len(data["matches"]), 1)
        m = data["matches"][0]
        # Score object is passed through unchanged (no transformation).
        self.assertEqual(m["score"], _MATCH_REGULAR["score"])
        self.assertEqual(m["status"], "FINISHED")
        # Other FD fields survive verbatim.
        self.assertEqual(m["utcDate"], "2026-06-28T19:00:00Z")
        self.assertEqual(m["stage"], "LAST_32")
        self.assertEqual(m["homeTeam"]["tla"], "RSA")
        # video_id key is always present (null until a highlight is grafted).
        self.assertIsNone(m["video_id"])

    def test_penalty_shootout_all_score_fields_preserved(self):
        data = build_tournament_data(
            "world-cup", {}, _matches_payload(_MATCH_PENALTY)
        )
        m = data["matches"][0]
        # Every FD score sub-field survives — the app relies on fullTime,
        # regularTime, penalties, duration and winner all being intact.
        self.assertEqual(m["score"], _MATCH_PENALTY["score"])
        self.assertEqual(m["score"]["duration"], "PENALTY_SHOOTOUT")
        self.assertEqual(m["score"]["fullTime"], {"home": 4, "away": 5})
        self.assertEqual(m["score"]["penalties"], {"home": 3, "away": 4})
        self.assertEqual(m["status"], "FINISHED")

    def test_extra_time_score_preserved(self):
        data = build_tournament_data("world-cup", {}, _matches_payload(_MATCH_EXTRA_TIME))
        m = data["matches"][0]
        self.assertEqual(m["score"], _MATCH_EXTRA_TIME["score"])
        self.assertEqual(m["score"]["duration"], "EXTRA_TIME")
        self.assertEqual(m["status"], "FINISHED")

    def test_unplayed_game_stays_null(self):
        data = build_tournament_data("world-cup", {}, _matches_payload(_MATCH_TIMED))
        m = data["matches"][0]
        self.assertEqual(m["status"], "TIMED")
        self.assertIsNone(m["score"]["fullTime"]["home"])
        self.assertIsNone(m["score"]["fullTime"]["away"])
        self.assertIsNone(m["video_id"])

    def test_mixed_finished_and_unplayed_in_one_build(self):
        data = build_tournament_data(
            "world-cup",
            _STANDINGS_PAYLOAD,
            _matches_payload(_MATCH_REGULAR, _MATCH_PENALTY, _MATCH_TIMED, _MATCH_GROUP),
        )
        # Group match excluded from knockout matches; 3 knockout matches kept.
        self.assertEqual(len(data["matches"]), 3)
        by_id = {m["id"]: m for m in data["matches"]}
        self.assertEqual(by_id[537417]["score"]["fullTime"], {"home": 0, "away": 1})
        self.assertIsNone(by_id[537420]["score"]["fullTime"]["home"])
        # Group match routed to groupMatches.
        self.assertEqual(len(data["groupMatches"]), 1)
        self.assertNotIn(600001, by_id)

    def test_only_knockout_stages_in_matches(self):
        data = build_tournament_data("world-cup", {}, _matches_payload(_MATCH_GROUP))
        self.assertEqual(data["matches"], [])

    def test_standings_filtered_to_total_only(self):
        data = build_tournament_data("world-cup", _STANDINGS_PAYLOAD, {})
        self.assertEqual(len(data["standings"]), 2)  # both TOTAL groups
        self.assertTrue(all(s["type"] == "TOTAL" for s in data["standings"]))

    def test_generated_at_ends_with_z(self):
        data = build_tournament_data("world-cup", {}, {})
        self.assertTrue(
            data["generated_at"].endswith("Z"),
            f"expected UTC 'Z' suffix, got {data['generated_at']!r}",
        )

    def test_slug_and_shape(self):
        data = build_tournament_data("ucl", {}, {})
        self.assertEqual(data["slug"], "ucl")
        self.assertIn("standings", data)
        self.assertIn("matches", data)
        self.assertIn("groupMatches", data)

    def test_generic_across_competitions(self):
        # No competition-specific branching: the same builder handles euro-cup.
        data = build_tournament_data("euro-cup", {}, _matches_payload(_MATCH_REGULAR))
        self.assertEqual(data["slug"], "euro-cup")
        self.assertEqual(data["matches"][0]["score"], _MATCH_REGULAR["score"])


# ── video_id graft into the build ──────────────────────────────────────────────


class TestVideoIdGraftIntoBuild(unittest.TestCase):
    def test_existing_video_id_preserved(self):
        data = build_tournament_data(
            "world-cup",
            {},
            _matches_payload(_MATCH_REGULAR),
            existing_video_ids={537417: "keepME123"},
        )
        self.assertEqual(data["matches"][0]["video_id"], "keepME123")

    def test_missing_id_yields_null_video_id(self):
        data = build_tournament_data(
            "world-cup", {}, _matches_payload(_MATCH_REGULAR),
            existing_video_ids={999999: "other"},
        )
        self.assertIsNone(data["matches"][0]["video_id"])

    def test_group_match_video_id_null_by_default(self):
        data = build_tournament_data(
            "world-cup", {}, _matches_payload(_MATCH_GROUP)
        )
        self.assertIsNone(data["groupMatches"][0]["video_id"])

    def test_group_match_existing_video_id_preserved(self):
        data = build_tournament_data(
            "world-cup", {}, _matches_payload(_MATCH_GROUP),
            existing_video_ids={600001: "PREV_VID"},
        )
        self.assertEqual(data["groupMatches"][0]["video_id"], "PREV_VID")


# ── group matches ───────────────────────────────────────────────────────────────


class TestBuildGroupMatches(unittest.TestCase):
    def test_group_match_carries_score_and_status(self):
        gms = build_group_matches(_matches_payload(_MATCH_GROUP))
        self.assertEqual(len(gms), 1)
        gm = gms[0]
        self.assertEqual(gm["group"], "GROUP_A")
        self.assertEqual(gm["matchday"], 1)
        self.assertEqual(gm["score"]["fullTime"], {"home": 2, "away": 0})
        self.assertEqual(gm["status"], "FINISHED")

    def test_group_match_has_match_id(self):
        gms = build_group_matches(_matches_payload(_MATCH_GROUP))
        self.assertEqual(gms[0]["match_id"], 600001)

    def test_group_match_missing_fd_id_emits_null_match_id(self):
        no_id = {k: v for k, v in _MATCH_GROUP.items() if k != "id"}
        gms = build_group_matches(_matches_payload(no_id))
        self.assertIsNone(gms[0]["match_id"])

    def test_group_match_carries_utcDate(self):
        # The extraction fix: utcDate must be copied from the FD match object so
        # group games get a date and appear on the date-driven Home feed.
        gms = build_group_matches(_matches_payload(_MATCH_GROUP))
        self.assertEqual(gms[0]["utcDate"], "2026-06-14T19:00:00Z")

    def test_group_match_missing_utcDate_is_null_not_error(self):
        # An unscheduled fixture with no date must not crash — None degrades
        # gracefully (normalize_match keeps it off Home).
        no_date = {k: v for k, v in _MATCH_GROUP.items() if k != "utcDate"}
        gms = build_group_matches(_matches_payload(no_date))
        self.assertIsNone(gms[0]["utcDate"])

    def test_group_match_preserves_other_fields_with_utcDate_added(self):
        # Adding utcDate must not disturb the existing projection.
        gm = build_group_matches(_matches_payload(_MATCH_GROUP))[0]
        self.assertEqual(gm["match_id"], 600001)
        self.assertEqual(gm["group"], "GROUP_A")
        self.assertEqual(gm["matchday"], 1)
        self.assertEqual(gm["sourceRound"], "Matchday 1")
        self.assertEqual(gm["homeTeam"]["tla"], "MEX")
        self.assertEqual(gm["awayTeam"]["tla"], "ECU")
        self.assertEqual(gm["score"]["fullTime"], {"home": 2, "away": 0})
        self.assertEqual(gm["status"], "FINISHED")

    def test_group_match_utcDate_flows_through_build_tournament_data(self):
        # End-to-end: the assembled artifact's groupMatches carry utcDate + video_id.
        data = build_tournament_data(
            "world-cup", {}, _matches_payload(_MATCH_GROUP),
            existing_video_ids={600001: "VID123"},
        )
        gm = data["groupMatches"][0]
        self.assertEqual(gm["utcDate"], "2026-06-14T19:00:00Z")
        self.assertEqual(gm["match_id"], 600001)
        self.assertEqual(gm["video_id"], "VID123")

    def test_knockout_excluded_from_group_matches(self):
        self.assertEqual(build_group_matches(_matches_payload(_MATCH_REGULAR)), [])

    def test_group_without_matchday_skipped(self):
        no_md = {**_MATCH_GROUP, "matchday": None}
        self.assertEqual(build_group_matches(_matches_payload(no_md)), [])

    def test_league_phase_no_group_skipped(self):
        league = {**_MATCH_GROUP, "group": None, "stage": "LEAGUE_STAGE"}
        self.assertEqual(build_group_matches(_matches_payload(league)), [])


# ── fetch endpoints (mocked urllib) ─────────────────────────────────────────────


class TestFetchEndpoints(unittest.TestCase):
    def _mock_response(self, payload):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__ = lambda self: self
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_matches_endpoint_has_no_status_filter(self):
        # The tournament cache needs ALL statuses (TIMED + FINISHED), so the
        # /matches call must NOT restrict to status=FINISHED.
        with patch("urllib.request.urlopen") as mock:
            mock.return_value = self._mock_response({"matches": []})
            fetch_matches(2000, "key", base_url="https://mock.api")
        url = mock.call_args[0][0].get_full_url()
        self.assertIn("/competitions/2000/matches", url)
        self.assertNotIn("status", url)
        self.assertNotIn("season", url)

    def test_matches_sends_auth_header(self):
        with patch("urllib.request.urlopen") as mock:
            mock.return_value = self._mock_response({"matches": []})
            fetch_matches(2000, "secret", base_url="https://mock.api")
        self.assertEqual(mock.call_args[0][0].get_header("X-auth-token"), "secret")

    def test_standings_endpoint(self):
        with patch("urllib.request.urlopen") as mock:
            mock.return_value = self._mock_response({"standings": []})
            result = fetch_standings(2000, "key", base_url="https://mock.api")
        url = mock.call_args[0][0].get_full_url()
        self.assertIn("/competitions/2000/standings", url)
        self.assertEqual(result, {"standings": []})

    def test_season_param_included_when_passed(self):
        with patch("urllib.request.urlopen") as mock:
            mock.return_value = self._mock_response({"matches": []})
            fetch_matches(2000, "key", base_url="https://mock.api", season=2026)
        self.assertIn("season=2026", mock.call_args[0][0].get_full_url())


# ── write / read / graft round-trip on disk ─────────────────────────────────────


class TestWriteReadGraft(unittest.TestCase):
    def test_write_and_read_existing_video_ids_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = build_tournament_data(
                "world-cup", {}, _matches_payload(_MATCH_REGULAR),
                existing_video_ids={537417: "vidABC"},
            )
            path = write_tournament("world-cup", data, out_dir=tmp)
            self.assertTrue(os.path.isfile(path))
            got = read_existing_video_ids(path)
            self.assertEqual(got, {537417: "vidABC"})

    def test_read_existing_video_ids_preserves_group_match_video_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = build_tournament_data(
                "world-cup", {}, _matches_payload(_MATCH_GROUP),
                existing_video_ids={600001: "GROUP_PREV"},
            )
            path = write_tournament("world-cup", data, out_dir=tmp)
            got = read_existing_video_ids(path)
            self.assertIn(600001, got)
            self.assertEqual(got[600001], "GROUP_PREV")

    def test_read_existing_video_ids_missing_file(self):
        self.assertEqual(read_existing_video_ids("/no/such/file.json"), {})

    def _write_highlights(self, tmp, slug, season, stem, entries):
        d = os.path.join(tmp, "highlights", slug, str(season))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{stem}.json"), "w", encoding="utf-8") as f:
            json.dump({"matches": entries}, f)

    def test_graft_sets_video_id_from_highlights(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = build_tournament_data(
                "world-cup", {}, _matches_payload(_MATCH_REGULAR)
            )
            write_tournament("world-cup", data, out_dir=tmp)
            # LAST_32 → round-of-32.json; World Cup season=2026.
            self._write_highlights(
                tmp, "world-cup", 2026, "round-of-32",
                [{"match_id": 537417, "videos": [{"video_id": "GRAFTED99"}]}],
            )
            graft_video_ids(out_dir=tmp)
            with open(
                os.path.join(tmp, "tournament-groups", "world-cup.json"),
                encoding="utf-8",
            ) as f:
                reloaded = json.load(f)
            self.assertEqual(reloaded["matches"][0]["video_id"], "GRAFTED99")

    def test_graft_generic_for_copa_leaves_scores_untouched(self):
        # Copa uses the API-Sports schema (no status, penalties in score.penalties).
        # graft must add its video_id without disturbing its scores.
        copa_match = {
            "id": 700001,
            "stage": "QUARTER_FINALS",
            "homeTeam": {"id": 26, "name": "Argentina", "tla": "", "crest": "a.png"},
            "awayTeam": {"id": 2382, "name": "Ecuador", "tla": "", "crest": "e.png"},
            "score": {"fullTime": {"home": 1, "away": 1}, "penalties": {"home": 4, "away": 2}},
            "video_id": None,
        }
        copa_cache = {
            "generated_at": "2026-07-01T13:00:00Z",
            "slug": "copa-america",
            "standings": [],
            "matches": [copa_match],
        }
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "tournament-groups"), exist_ok=True)
            with open(
                os.path.join(tmp, "tournament-groups", "copa-america.json"),
                "w", encoding="utf-8",
            ) as f:
                json.dump(copa_cache, f)
            # Copa America season=2024 (cycle anchor 2024, period 4).
            self._write_highlights(
                tmp, "copa-america", 2024, "quarter-final",
                [{"match_id": 700001, "videos": [{"video_id": "COPAVID1"}]}],
            )
            graft_video_ids(out_dir=tmp)
            with open(
                os.path.join(tmp, "tournament-groups", "copa-america.json"),
                encoding="utf-8",
            ) as f:
                reloaded = json.load(f)
            m = reloaded["matches"][0]
            self.assertEqual(m["video_id"], "COPAVID1")
            # Scores untouched — API-Sports schema preserved, no FD fields forced.
            self.assertEqual(m["score"], copa_match["score"])
            self.assertNotIn("status", m)

    def test_graft_sets_video_id_on_group_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = build_tournament_data(
                "world-cup", {}, _matches_payload(_MATCH_GROUP)
            )
            write_tournament("world-cup", data, out_dir=tmp)
            # GROUP_STAGE matchday=1 → matchday-1.json; World Cup season=2026.
            self._write_highlights(
                tmp, "world-cup", 2026, "matchday-1",
                [{"match_id": 600001, "videos": [{"video_id": "GROUPVID1"}]}],
            )
            graft_video_ids(out_dir=tmp)
            with open(
                os.path.join(tmp, "tournament-groups", "world-cup.json"),
                encoding="utf-8",
            ) as f:
                reloaded = json.load(f)
            gm = reloaded["groupMatches"][0]
            self.assertEqual(gm["video_id"], "GROUPVID1")

    def test_group_match_without_highlight_stays_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = build_tournament_data(
                "world-cup", {}, _matches_payload(_MATCH_GROUP)
            )
            write_tournament("world-cup", data, out_dir=tmp)
            # No highlights file written → video_id stays null.
            graft_video_ids(out_dir=tmp)
            with open(
                os.path.join(tmp, "tournament-groups", "world-cup.json"),
                encoding="utf-8",
            ) as f:
                reloaded = json.load(f)
            self.assertIsNone(reloaded["groupMatches"][0]["video_id"])

    def test_knockout_graft_non_regression_with_group_present(self):
        # Both a group match and a knockout match — graft must handle both;
        # knockout video_id is unaffected by the new group-match graft path.
        with tempfile.TemporaryDirectory() as tmp:
            data = build_tournament_data(
                "world-cup", {}, _matches_payload(_MATCH_REGULAR, _MATCH_GROUP)
            )
            write_tournament("world-cup", data, out_dir=tmp)
            self._write_highlights(
                tmp, "world-cup", 2026, "round-of-32",
                [{"match_id": 537417, "videos": [{"video_id": "KO_VID"}]}],
            )
            self._write_highlights(
                tmp, "world-cup", 2026, "matchday-1",
                [{"match_id": 600001, "videos": [{"video_id": "GRP_VID"}]}],
            )
            graft_video_ids(out_dir=tmp)
            with open(
                os.path.join(tmp, "tournament-groups", "world-cup.json"),
                encoding="utf-8",
            ) as f:
                reloaded = json.load(f)
            self.assertEqual(reloaded["matches"][0]["video_id"], "KO_VID")
            self.assertEqual(reloaded["groupMatches"][0]["video_id"], "GRP_VID")


# ── Workflow consolidation (no double-write) ────────────────────────────────────


class TestWorkflowConsolidation(unittest.TestCase):
    def _read(self, name):
        with open(os.path.join(_WORKFLOWS, name), encoding="utf-8") as f:
            return f.read()

    def test_sync_teams_no_longer_writes_tournament_groups(self):
        content = self._read("sync-teams.yml")
        # The weekly roster job must not build or commit the tournament cache.
        self.assertNotIn("TOURNAMENT_COMPETITIONS", content)
        self.assertNotIn("git add sources.json tournament-groups/", content)
        self.assertIn("git add sources.json", content)

    def test_fetch_highlights_drives_sync_tournaments(self):
        content = self._read("fetch-highlights.yml")
        self.assertIn("scripts/sync_tournaments.py", content)
        # The old inline PYEOF graft block is gone.
        self.assertNotIn("PYEOF", content)

    def test_no_apisports_call_in_frequent_path(self):
        # sync_tournaments.py (the frequent path) is football-data.org only.
        # Check for actual API-Sports call signatures — not documentation prose,
        # which legitimately mentions API-Sports to explain that it makes none.
        with open(
            os.path.join(os.path.dirname(__file__), "..", "sync_tournaments.py"),
            encoding="utf-8",
        ) as f:
            src = f.read().lower()
        self.assertNotIn("api-sports.io", src)      # API-Sports base URL
        self.assertNotIn("x-apisports-key", src)    # API-Sports auth header
        self.assertNotIn("apisportsprovider", src)  # API-Sports provider class
        self.assertNotIn("apisports_api_key", src)  # API-Sports secret


if __name__ == "__main__":
    unittest.main()
