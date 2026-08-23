"""Tests for the cross-competition Home index assembler (build_home_index.py).

No network — everything derives from tmp fixture/highlights/tournament files that
mimic the real cached artifacts. Covers: UTC date bucketing, raw-status passthrough,
video_id join (domestic) / embedding (tournament), skipping undated matches,
canonical competition order, per-month file layout, presence/absence semantics,
and idempotency.
"""

import json
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import build_home_index as bhi


# ── Pure helpers ────────────────────────────────────────────────────────────────

class TestBucketDate:
    def test_z_suffix_utc_date(self):
        assert bhi._bucket_date("2026-03-10T17:45:00Z") == "2026-03-10"

    def test_near_midnight_stays_utc(self):
        # 23:30Z buckets to that UTC day (client re-labels in local time).
        assert bhi._bucket_date("2026-08-23T23:30:00Z") == "2026-08-23"

    def test_offset_converted_to_utc(self):
        # 00:30+02:00 == 22:30Z previous day.
        assert bhi._bucket_date("2026-08-24T00:30:00+02:00") == "2026-08-23"

    def test_none_and_bad(self):
        assert bhi._bucket_date(None) is None
        assert bhi._bucket_date("") is None
        assert bhi._bucket_date("not-a-date") is None


class TestNormalizeMatch:
    def _domestic(self, **kw):
        base = {
            "match_id": 1001,
            "homeTeam": {"name": "Arsenal FC", "shortName": "Arsenal", "tla": "ARS",
                         "crest": "https://crests.football-data.org/57.png"},
            "awayTeam": {"name": "Chelsea FC", "shortName": "Chelsea", "tla": "CHE",
                         "crest": "https://crests.football-data.org/61.png"},
            "score": {"fullTime": {"home": 2, "away": 1}},
            "status": "FINISHED",
            "utcDate": "2026-03-01T14:00:00Z",
        }
        base.update(kw)
        return base

    def test_full_domestic_fields(self):
        m = bhi.normalize_match(self._domestic(), "VID123")
        assert m["match_id"] == 1001
        assert m["homeTeam"] == {"name": "Arsenal FC", "shortName": "Arsenal",
                                 "tla": "ARS", "crest": "https://crests.football-data.org/57.png"}
        assert m["homeScore"] == 2 and m["awayScore"] == 1
        assert m["status"] == "FINISHED"
        assert m["utcDate"] == "2026-03-01T14:00:00Z"
        assert m["videoId"] == "VID123"

    def test_raw_status_not_collapsed(self):
        for tok in ("TIMED", "IN_PLAY", "PAUSED", "SCHEDULED", "AWARDED"):
            m = bhi.normalize_match(self._domestic(status=tok), None)
            assert m["status"] == tok  # never renamed/collapsed; no 'live' derived
            assert "live" not in m

    def test_null_scores_when_absent(self):
        m = bhi.normalize_match(self._domestic(score={"fullTime": {}}), None)
        assert m["homeScore"] is None and m["awayScore"] is None

    def test_no_video_key_when_none(self):
        m = bhi.normalize_match(self._domestic(), None)
        assert "videoId" not in m

    def test_tournament_id_and_embedded_video(self):
        raw = {
            "id": 552069,
            "homeTeam": {"name": "Galatasaray SK", "shortName": "Galatasaray",
                         "tla": "GAL", "crest": "c1"},
            "awayTeam": {"name": "Liverpool FC", "shortName": "Liverpool",
                         "tla": "LIV", "crest": "c2"},
            "score": {"fullTime": {"home": 1, "away": 0}},
            "status": "FINISHED",
            "utcDate": "2026-03-10T17:45:00Z",
            "video_id": "EMBED",
        }
        m = bhi.normalize_match(raw, None)
        assert m["match_id"] == 552069  # reads `id` when `match_id` absent
        assert m["videoId"] == "EMBED"

    def test_missing_id_skipped(self):
        raw = self._domestic()
        del raw["match_id"]
        assert bhi.normalize_match(raw, None) is None

    def test_missing_utcdate_skipped(self):
        # All tournament group matches / Copa knockout look like this — skipped, never
        # given a fabricated date.
        raw = self._domestic()
        del raw["utcDate"]
        assert bhi.normalize_match(raw, None) is None


class TestCanonicalOrder:
    def test_all_ten_competitions_have_slugs(self):
        from highlights_common import COMPETITION_SLUG_MAP
        assert len(COMPETITION_SLUG_MAP) == 10


# ── Integration: build + write against tmp cache dirs ───────────────────────────

def _setup_repo(tmp_path, monkeypatch):
    fixtures = tmp_path / "fixtures"
    highlights = tmp_path / "highlights"
    tgroups = tmp_path / "tournament-groups"
    home_index = tmp_path / "home-index"
    sources = tmp_path / "sources.json"

    # Canonical order: Premier League then Champions League (domestic then tournament).
    sources.write_text(json.dumps({
        "competitions": {"Premier League": "", "Champions League": ""}
    }), encoding="utf-8")

    # Domestic fixtures (no video_id — joined from highlights).
    (fixtures / "premier-league").mkdir(parents=True)
    (fixtures / "premier-league" / "2026.json").write_text(json.dumps({
        "fixtures": [
            {"match_id": 1, "homeTeam": {"name": "A", "shortName": "A", "tla": "A", "crest": "a"},
             "awayTeam": {"name": "B", "shortName": "B", "tla": "B", "crest": "b"},
             "score": {"fullTime": {"home": 1, "away": 0}}, "status": "FINISHED",
             "utcDate": "2026-03-10T12:00:00Z"},
            {"match_id": 2, "homeTeam": {"name": "C", "shortName": "C", "tla": "C", "crest": "c"},
             "awayTeam": {"name": "D", "shortName": "D", "tla": "D", "crest": "d"},
             "score": {"fullTime": {"home": None, "away": None}}, "status": "TIMED",
             "utcDate": "2026-04-01T12:00:00Z"},
        ]
    }), encoding="utf-8")

    # Highlights artifact: video for match_id 1 only.
    (highlights / "premier-league" / "2026").mkdir(parents=True)
    (highlights / "premier-league" / "2026" / "gameweek-1.json").write_text(json.dumps({
        "matches": [{"match_id": 1, "videos": [{"video_id": "PLVID"}]},
                    {"match_id": 2, "videos": []}]
    }), encoding="utf-8")

    # Tournament: one dated knockout match (same date as PL match 1) + one undated
    # group match (must be skipped).
    tgroups.mkdir(parents=True)
    (tgroups / "ucl.json").write_text(json.dumps({
        "matches": [
            {"id": 900, "homeTeam": {"name": "E", "shortName": "E", "tla": "E", "crest": "e"},
             "awayTeam": {"name": "F", "shortName": "F", "tla": "F", "crest": "f"},
             "score": {"fullTime": {"home": 2, "away": 2}}, "status": "FINISHED",
             "utcDate": "2026-03-10T20:00:00Z", "video_id": "UCLVID"},
        ],
        "groupMatches": [
            {"homeTeam": {"name": "G"}, "awayTeam": {"name": "H"},
             "score": {"fullTime": {"home": 0, "away": 0}}, "status": "FINISHED"},  # no id/utcDate
        ],
    }), encoding="utf-8")

    monkeypatch.setattr(bhi, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(bhi, "HIGHLIGHTS_DIR", highlights)
    monkeypatch.setattr(bhi, "TOURNAMENT_GROUPS_DIR", tgroups)
    monkeypatch.setattr(bhi, "HOME_INDEX_DIR", home_index)
    monkeypatch.setattr(bhi, "SOURCES_JSON", sources)
    return home_index


class TestBuildIndex:
    def test_buckets_group_and_orders(self, tmp_path, monkeypatch):
        _setup_repo(tmp_path, monkeypatch)
        months, manifest = bhi.build_index()

        # Two months: 2026-03 (PL#1 + UCL#900) and 2026-04 (PL#2).
        assert set(months) == {"2026-03", "2026-04"}

        march = months["2026-03"]["2026-03-10"]
        # canonical order: premier-league before ucl.
        assert [g["competition"] for g in march] == ["premier-league", "ucl"]

        # Domestic video joined; tournament video embedded.
        pl = march[0]["matches"][0]
        assert pl["match_id"] == 1 and pl["videoId"] == "PLVID"
        ucl = march[1]["matches"][0]
        assert ucl["match_id"] == 900 and ucl["videoId"] == "UCLVID"

        # April date has only PL, no videoId (match 2 had no videos), null scores.
        apr = months["2026-04"]["2026-04-01"]
        assert [g["competition"] for g in apr] == ["premier-league"]
        assert "videoId" not in apr[0]["matches"][0]
        assert apr[0]["matches"][0]["homeScore"] is None

    def test_undated_group_match_skipped(self, tmp_path, monkeypatch):
        _setup_repo(tmp_path, monkeypatch)
        months, _ = bhi.build_index()
        all_ids = [m["match_id"]
                   for month in months.values()
                   for date in month.values()
                   for g in date for m in g["matches"]]
        assert set(all_ids) == {1, 2, 900}  # the undated groupMatch is absent

    def test_manifest_shape(self, tmp_path, monkeypatch):
        _setup_repo(tmp_path, monkeypatch)
        _, manifest = bhi.build_index()
        assert manifest["months"] == ["2026-03", "2026-04"]
        assert [c["slug"] for c in manifest["competitions"]] == ["premier-league", "ucl"]
        assert isinstance(manifest["current_season"], int)

    def test_absent_date_has_no_entry(self, tmp_path, monkeypatch):
        _setup_repo(tmp_path, monkeypatch)
        months, _ = bhi.build_index()
        # 2026-03 has only the 10th — the 11th is absent (client renders empty day).
        assert "2026-03-11" not in months["2026-03"]


class TestWriteHomeIndex:
    def test_writes_month_files_and_manifest(self, tmp_path, monkeypatch):
        home_index = _setup_repo(tmp_path, monkeypatch)
        bhi.regenerate()
        assert (home_index / "2026-03.json").exists()
        assert (home_index / "2026-04.json").exists()
        assert (home_index / "index.json").exists()
        # No monolithic all-dates file.
        assert not (home_index / "all.json").exists()

    def test_idempotent_rerun_no_rewrite(self, tmp_path, monkeypatch):
        home_index = _setup_repo(tmp_path, monkeypatch)
        bhi.regenerate()
        before = {p.name: p.read_bytes() for p in home_index.glob("*.json")}
        bhi.regenerate()
        after = {p.name: p.read_bytes() for p in home_index.glob("*.json")}
        assert before == after  # byte-identical: content-driven writes, no churn

    def test_stale_month_removed(self, tmp_path, monkeypatch):
        home_index = _setup_repo(tmp_path, monkeypatch)
        bhi.regenerate()
        stale = home_index / "1999-01.json"
        stale.write_text("{}", encoding="utf-8")
        bhi.regenerate()
        assert not stale.exists()  # removed (no longer backed by data)
        assert (home_index / "index.json").exists()  # manifest preserved
