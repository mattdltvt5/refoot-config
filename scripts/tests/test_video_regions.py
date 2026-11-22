"""Per-video ``region`` in the highlights cache (country-aware selection).

compact_region's shape, the matcher attaching it at no extra quota, and the
budgeted fill pass for already-cached videos. requests.get is patched.
"""
import json
from unittest.mock import MagicMock, patch

import highlights_common as hc
import video_regions as vr
from highlights_common import compact_region


def test_compact_region_shapes():
    assert compact_region(None) == {}
    assert compact_region({}) == {}
    assert compact_region({"blocked": []}) == {}
    assert compact_region({"allowed": []}) == {"allowed": []}
    assert compact_region({"allowed": ["us", "PR", "US"]}) == {"allowed": ["PR", "US"]}
    assert compact_region({"blocked": ["US", "CA"]}) == {"blocked": ["CA", "US"]}


def test_compact_region_agrees_with_playability():
    for raw in [None, {"allowed": ["US"]}, {"allowed": []}, {"blocked": ["US"]}]:
        compact = compact_region(raw)
        for cc in ["US", "GB", "CA"]:
            assert hc.region_is_playable_in(compact or None, cc) == \
                hc.region_is_playable_in(raw, cc), (raw, cc)


# ── fill pass ─────────────────────────────────────────────────────────────────

class _Quota:
    def __init__(self):
        self.units = 0

    def increment(self, cap):
        self.units += 1


def _write(root, slug, season, stem, videos_per_match):
    p = root / slug / str(season) / f"{stem}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"generated_at": "keep", "matches": [
        {"match_id": i, "videos": vids} for i, vids in enumerate(videos_per_match)]}),
        encoding="utf-8")
    return p


def _videos_response(items):
    r = MagicMock(status_code=200, ok=True)
    r.json.return_value = {"items": [
        {"id": vid, "contentDetails": {"duration": "PT5M",
                                       **({"regionRestriction": rr} if rr else {})},
         "snippet": {"thumbnails": {}}, "status": {"embeddable": True}}
        for vid, rr in items]}
    return r


def test_missing_ids_newest_season_first_and_unique(tmp_path):
    _write(tmp_path, "laliga", 2025, "gameweek-1", [[{"video_id": "old"}]])
    _write(tmp_path, "laliga", 2026, "gameweek-1",
           [[{"video_id": "new"}, {"video_id": "done", "region": {}}], [{"video_id": "new"}]])
    (tmp_path / "summary.json").write_text("{}")
    assert vr.missing_region_ids(tmp_path) == ["new", "old"]


def test_fill_writes_regions_and_marks_gone_videos(tmp_path):
    p = _write(tmp_path, "bundesliga", 2026, "gameweek-10", [
        [{"video_id": "a", "title": "A"}, {"video_id": "b"}], [{"video_id": "gone"}]])
    resp = _videos_response([("a", None), ("b", {"blocked": ["US"]})])
    q = _Quota()
    with patch("highlights_common.requests.get", return_value=resp):
        changed = vr.fill_missing_regions("k", q, 10**9, root=tmp_path)
    assert changed == [p] and q.units == 1
    data = json.loads(p.read_text(encoding="utf-8"))
    v = [x for m in data["matches"] for x in m["videos"]]
    assert v[0] == {"video_id": "a", "title": "A", "region": {}}
    assert v[1]["region"] == {"blocked": ["US"]}
    assert v[2]["region"] == {"allowed": []}       # deleted/private: plays nowhere
    assert data["generated_at"] == "keep"           # not new content
    assert vr.missing_region_ids(tmp_path) == []


def test_failed_call_marks_nothing(tmp_path):
    p = _write(tmp_path, "ucl", 2026, "matchday-1", [[{"video_id": "a"}]])
    bad = MagicMock(status_code=500, ok=False)
    with patch("highlights_common.requests.get", return_value=bad):
        assert vr.fill_missing_regions("k", _Quota(), 10**9, root=tmp_path) == []
    assert "region" not in json.loads(p.read_text())["matches"][0]["videos"][0]


def test_budget_caps_videos_checked(tmp_path):
    vids = [[{"video_id": f"v{i:03d}"}] for i in range(120)]
    _write(tmp_path, "serie-a", 2026, "gameweek-1", vids)
    calls = []

    def fake_get(url, params=None, timeout=None):
        ids = params["id"].split(",")
        calls.append(len(ids))
        return _videos_response([(i, None) for i in ids])

    q = _Quota()
    with patch("highlights_common.requests.get", side_effect=fake_get):
        vr.fill_missing_regions("k", q, 10**9, budget_units=2, root=tmp_path)
    assert calls == [50, 50] and q.units == 2
    assert len(vr.missing_region_ids(tmp_path)) == 20


def test_quota_cap_midway_keeps_what_was_found(tmp_path):
    vids = [[{"video_id": f"v{i:03d}"}] for i in range(100)]
    p = _write(tmp_path, "ligue-1", 2026, "gameweek-1", vids)
    responses = iter([_videos_response([(f"v{i:03d}", None) for i in range(50)]),
                      MagicMock(status_code=403, ok=False)])
    with patch("highlights_common.requests.get", side_effect=lambda *a, **k: next(responses)):
        try:
            vr.fill_missing_regions("k", _Quota(), 10**9, root=tmp_path)
            raise AssertionError("expected QuotaCapReached")
        except hc.QuotaCapReached:
            pass
    data = json.loads(p.read_text())
    assert sum("region" in m["videos"][0] for m in data["matches"]) == 50


def test_same_video_in_two_files_filled_everywhere(tmp_path):
    a = _write(tmp_path, "euro-cup", 2026, "final", [[{"video_id": "x"}]])
    b = _write(tmp_path, "euro-cup", 2025, "final", [[{"video_id": "x"}]])
    with patch("highlights_common.requests.get",
               return_value=_videos_response([("x", {"allowed": ["GB"]})])):
        changed = vr.fill_missing_regions("k", _Quota(), 10**9, root=tmp_path)
    assert set(changed) == {a, b}


def test_fetch_run_calls_fill_after_matcher():
    from pathlib import Path
    src = (Path(hc.__file__).parent / "fetch_highlights.py").read_text(encoding="utf-8")
    assert "fill_missing_regions(yt_key, quota, INCREMENTAL_CAP)" in src
    assert src.index("fill_missing_regions(yt_key") > src.index("resolve_videos_for_fixture(\n")


# ── matcher attaches region ───────────────────────────────────────────────────

def test_matcher_results_carry_region(monkeypatch):
    fixture = {"date": "2026-11-21", "home_team": "Arsenal FC", "home_short": "Arsenal",
               "home_tla": "ARS", "away_team": "Chelsea FC", "away_short": "Chelsea",
               "away_tla": "CHE"}
    monkeypatch.setattr(hc, "search_playlist", lambda *a, **k: [
        {"video_id": "g", "title": "Arsenal v Chelsea | Highlights", "published_at": "2026-11-21"},
        {"video_id": "u", "title": "Arsenal v Chelsea | Highlights", "published_at": "2026-11-21"}])
    monkeypatch.setattr(hc, "fetch_video_details", lambda ids, *a: {
        "g": {"duration_seconds": 300, "is_portrait": False, "is_embeddable": True,
              "is_region_restricted": False, "is_playable_nowhere": False,
              "region_restriction": None}})
    config = {"team_playlists": {"Premier League": {"Arsenal FC": "PLh"}},
              "competition_channels": {}, "competition_playlists": {}, "team_channels": {}}
    got = hc.resolve_videos_for_fixture(fixture, "Premier League", config, "k", _Quota(), 10**9)
    by_id = {v["video_id"]: v for v in got}
    assert by_id["g"]["region"] == {}
    assert "region" not in by_id["u"]   # no details: unknown, left for the fill pass


# ── alternatives copied to home-index / tournament-groups ─────────────────────

def test_video_options_only_when_something_is_restricted():
    from highlights_common import video_options
    assert video_options(None) is None
    assert video_options([{"video_id": "a", "region": {}}, {"video_id": "b"}]) is None
    opts = video_options([{"video_id": "a", "region": {"blocked": ["US"]}, "title": "x"},
                          {"video_id": "b"}])
    assert opts == [{"video_id": "a", "region": {"blocked": ["US"]}},
                    {"video_id": "b", "region": {}}]


def test_home_index_carries_options_for_domestic_and_tournament():
    import build_home_index as bhi
    raw = {"match_id": 1, "utcDate": "2026-11-21T14:30:00Z", "status": "FINISHED",
           "homeTeam": {"id": 1, "name": "A"}, "awayTeam": {"id": 2, "name": "B"},
           "score": {"fullTime": {"home": 1, "away": 0}}}
    opts = [{"video_id": "a", "region": {"blocked": ["US"]}}, {"video_id": "b", "region": {}}]
    m = bhi.normalize_match(raw, "a", opts)
    assert m["videoId"] == "a"
    assert m["videoOptions"] == [{"videoId": "a", "region": {"blocked": ["US"]}},
                                 {"videoId": "b", "region": {}}]
    assert "videoOptions" not in bhi.normalize_match(raw, "a", None)
    t = bhi.normalize_match({**raw, "video_id": "a", "video_options": opts}, None)
    assert [o["videoId"] for o in t["videoOptions"]] == ["a", "b"]


def test_tournament_graft_sets_and_clears_options():
    import sync_tournaments as st
    m = {"id": 9}
    locked = [{"video_id": "a", "region": {"allowed": ["GB"]}}, {"video_id": "b", "region": {}}]
    assert st._graft(m, locked) is True
    assert m["video_id"] == "a" and [o["video_id"] for o in m["video_options"]] == ["a", "b"]
    assert st._graft(m, locked) is False                       # stable
    assert st._graft(m, [{"video_id": "a", "region": {}}]) is True
    assert "video_options" not in m                            # nothing locked any more
    assert st._graft(m, ["c"]) is True and m["video_id"] == "c"  # bare ids still work
