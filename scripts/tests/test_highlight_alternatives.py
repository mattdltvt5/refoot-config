"""Alternatives pass: matches whose clips don't play in a launch country get a
clip that does, from any configured source that adds a country. No API calls: the matcher is
patched or its HTTP layer mocked."""
import json
from datetime import datetime, timedelta, timezone

import highlights_common as hc
import highlight_alternatives as ha

NOW = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
US_ONLY = {"allowed": ["PR", "US"]}
GLOBAL = {}


def vid(i, region=US_ONLY, title=None):
    v = {"video_id": i, "title": title or f"clip {i}", "published_at": "2026-11-21", "tier_used": 4}
    if region is not None:
        v["region"] = region
    return v


# ── coverage + scheduling ─────────────────────────────────────────────────────

def test_uncovered_and_needs():
    assert ha.uncovered([vid("a")]) == ["GB", "CA", "IE", "ES"]
    assert ha.uncovered([vid("a"), vid("b", GLOBAL)]) == []
    assert ha.uncovered([vid("a", None)]) == []             # unknown = playable
    assert ha.uncovered([vid("a", {"allowed": []})]) == hc.LAUNCH_COUNTRIES
    assert ha.needs_alternatives({"videos": [vid("a")]})
    assert not ha.needs_alternatives({"videos": []})
    assert not ha.needs_alternatives({"videos": [vid("a", {"blocked": ["DE"]})]})


def _m(date, **state):
    m = {"match_id": 1, "date": date, "videos": [vid("a")]}
    if state:
        m["alternatives"] = state
    return m


def test_recent_backoff_2_4_8_hours_then_stop():
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ha.due(_m("2026-11-21"), NOW)
    last = NOW - timedelta(hours=1, minutes=59)
    assert not ha.due(_m("2026-11-21", checks=1, last=iso(last)), NOW)
    assert ha.due(_m("2026-11-21", checks=1, last=iso(NOW - timedelta(hours=2))), NOW)
    assert not ha.due(_m("2026-11-21", checks=2, last=iso(NOW - timedelta(hours=3))), NOW)
    assert ha.due(_m("2026-11-21", checks=3, last=iso(NOW - timedelta(hours=8))), NOW)
    assert not ha.due(_m("2026-11-21", checks=6, last=iso(NOW - timedelta(days=30))), NOW)


def test_older_matches_are_checked_once():
    assert ha.due(_m("2026-10-01"), NOW)
    assert not ha.due(_m("2026-10-01", checks=1, last="2026-11-01T00:00:00Z"), NOW)


def test_covered_or_empty_matches_never_due():
    m = _m("2026-11-21")
    m["videos"].append(vid("g", GLOBAL))
    assert not ha.due(m, NOW)
    assert not ha.due({"match_id": 2, "date": "2026-11-21", "videos": []}, NOW)


def test_accept_only_new_clips_that_add_a_country():
    accept = ha.make_accept([vid("a", US_ONLY, "Same Title")])
    assert accept(vid("g", GLOBAL))
    assert accept(vid("gb", {"allowed": ["GB"]}))
    assert not accept(vid("us2", {"allowed": ["US"]}))       # adds nothing
    assert not accept(vid("a", GLOBAL))                     # already listed
    assert not accept(vid("x", GLOBAL, "same title"))       # duplicate title
    assert not accept(vid("n", None))                       # can't show it helps


# ── matcher alternatives mode ─────────────────────────────────────────────────

FIX = {"date": "2026-11-21", "home_team": "Arsenal FC", "home_short": "Arsenal", "home_tla": "ARS",
       "away_team": "Chelsea FC", "away_short": "Chelsea", "away_tla": "CHE"}


def _config():
    return {"team_playlists": {}, "competition_channels": {"Premier League": "UCcomp"},
            "competition_playlists": {"Premier League": {"B": ["PLbroadcaster"]}},
            "team_channels": {"Arsenal FC": "UChome"}}


def test_skip_broadcasters_and_accept_move_past_refused_tiers(monkeypatch):
    searched = []

    def fake_search(playlist_id, *a, **k):
        searched.append(playlist_id)
        return [{"video_id": playlist_id, "title": "Arsenal v Chelsea | Highlights",
                 "published_at": "2026-11-21"}]

    regions = {"UUcomp": {"allowed": ["US"]}, "UUhome": None}
    monkeypatch.setattr(hc, "search_playlist", fake_search)
    monkeypatch.setattr(hc, "find_gameweek_playlist", lambda *a, **k: None)
    monkeypatch.setattr(hc, "fetch_video_details", lambda ids, *a: {
        i: {"duration_seconds": 300, "is_portrait": False, "is_embeddable": True,
            "is_region_restricted": regions[i] is not None, "is_playable_nowhere": False,
            "region_restriction": regions[i]} for i in ids})
    got = hc.resolve_videos_for_fixture(
        FIX, "Premier League", _config(), "k", object(), 10**9,
        skip_broadcasters=True, accept=ha.make_accept([vid("old", US_ONLY)]))
    assert "PLbroadcaster" not in searched
    assert searched == ["UUcomp", "UUhome"]          # US-only comp clip refused → next tier
    assert [v["video_id"] for v in got] == ["UUhome"] and got[0]["region"] == {}


def test_default_mode_unchanged(monkeypatch):
    searched = []
    monkeypatch.setattr(hc, "search_playlist", lambda pl, *a, **k: searched.append(pl) or [])
    monkeypatch.setattr(hc, "find_gameweek_playlist", lambda *a, **k: None)
    hc.resolve_videos_for_fixture(FIX, "Premier League", _config(), "k", object(), 10**9)
    assert searched == ["UUcomp", "PLbroadcaster", "UUhome"]


# ── the pass over cache files ─────────────────────────────────────────────────

class _Quota:
    def __init__(self, used=0):
        self.units_used = used

    def increment(self, cap):
        self.units_used += 1


def _setup(tmp_path, matches, fixtures):
    hl = tmp_path / "highlights" / "serie-a" / "2026"
    hl.mkdir(parents=True)
    p = hl / "gameweek-12.json"
    p.write_text(json.dumps({"generated_at": "old", "matches": matches}), encoding="utf-8")
    fx = tmp_path / "fixtures" / "serie-a"
    fx.mkdir(parents=True)
    (fx / "2026.json").write_text(json.dumps({"fixtures": fixtures}), encoding="utf-8")
    return p


def _fx(mid, date):
    return {"match_id": mid, "matchday": 12, "status": "FINISHED", "utcDate": f"{date}T14:00:00Z",
            "homeTeam": {"name": "SS Lazio", "shortName": "Lazio", "tla": "LAZ"},
            "awayTeam": {"name": "US Lecce", "shortName": "Lecce", "tla": "LEC"},
            "score": {"fullTime": {"home": 2, "away": 0}}}


def _only_serie_a(monkeypatch):
    monkeypatch.setattr(ha, "DOMESTIC_LEAGUE_COMPS", {"Serie A"})
    monkeypatch.setattr(ha, "season_for_competition", lambda comp, now=None: 2026)


def test_pass_appends_found_clip_and_books_the_check(tmp_path, monkeypatch):
    _only_serie_a(monkeypatch)
    p = _setup(tmp_path,
               [{"match_id": 1, "date": "2026-11-21", "home_team": "SS Lazio", "away_team": "US Lecce",
                 "videos": [vid("cbs")]},
                {"match_id": 2, "date": "2026-11-21", "videos": [vid("ok", GLOBAL)]}],
               [_fx(1, "2026-11-21"), _fx(2, "2026-11-21")])
    calls = []

    def fake_resolve(fixture, comp, config, key, quota, cap, **kw):
        calls.append((fixture["match_id"], kw.get("skip_broadcasters", False)))
        cand = vid("seriea", GLOBAL, "Lazio-Lecce | Highlights")
        return [cand] if kw["accept"](cand) else []

    monkeypatch.setattr(ha, "resolve_videos_for_fixture", fake_resolve)
    written = ha.find_alternatives("k", _Quota(), 10**9, {}, now=NOW,
                                   root=tmp_path / "highlights", fixtures_dir=tmp_path / "fixtures")
    assert written == [p] and calls == [(1, False)]
    m = json.loads(p.read_text())["matches"][0]
    assert [v["video_id"] for v in m["videos"]] == ["cbs", "seriea"]   # existing stays first
    assert m["alternatives"] == {"checks": 1, "last": "2026-11-22T12:00:00Z"}
    assert ha.uncovered(m["videos"]) == []
    # Covered now: never checked again.
    assert ha.find_alternatives("k", _Quota(), 10**9, {}, now=NOW + timedelta(days=1),
                                root=tmp_path / "highlights", fixtures_dir=tmp_path / "fixtures") == []


def test_nothing_found_still_books_and_backs_off(tmp_path, monkeypatch):
    _only_serie_a(monkeypatch)
    p = _setup(tmp_path, [{"match_id": 1, "date": "2026-11-21", "videos": [vid("cbs")]}],
               [_fx(1, "2026-11-21")])
    monkeypatch.setattr(ha, "resolve_videos_for_fixture", lambda *a, **k: [])
    ha.find_alternatives("k", _Quota(), 10**9, {}, now=NOW, root=tmp_path / "highlights",
                         fixtures_dir=tmp_path / "fixtures")
    data = json.loads(p.read_text())
    assert data["generated_at"] == "old"                  # bookkeeping isn't new content
    assert data["matches"][0]["alternatives"]["checks"] == 1
    assert ha.find_alternatives("k", _Quota(), 10**9, {}, now=NOW + timedelta(hours=1),
                                root=tmp_path / "highlights", fixtures_dir=tmp_path / "fixtures") == []


def test_catchup_budget_and_ceiling(tmp_path, monkeypatch):
    _only_serie_a(monkeypatch)
    old = [{"match_id": i, "date": f"2026-10-{i:02d}", "videos": [vid(f"c{i}")]} for i in range(1, 11)]
    _setup(tmp_path, old, [_fx(i, f"2026-10-{i:02d}") for i in range(1, 11)])
    order = []

    def spend(fixture, comp, config, key, quota, cap, **kw):
        order.append(fixture["match_id"])
        quota.units_used += 4
        return []

    monkeypatch.setattr(ha, "resolve_videos_for_fixture", spend)
    kw = dict(root=tmp_path / "highlights", fixtures_dir=tmp_path / "fixtures", now=NOW)
    ha.find_alternatives("k", _Quota(), 10**9, {}, catchup_units=10, **kw)
    assert order == [10, 9, 8]                            # newest first, stops at the budget
    order.clear()
    ha.find_alternatives("k", _Quota(used=6000), 10**9, {}, catchup_ceiling=5000, **kw)
    assert order == []                                    # busy day: no catch-up


def test_quota_cap_midway_writes_what_was_found(tmp_path, monkeypatch):
    _only_serie_a(monkeypatch)
    p = _setup(tmp_path, [{"match_id": 1, "date": "2026-11-21", "videos": [vid("a")]},
                          {"match_id": 2, "date": "2026-11-21", "videos": [vid("b")]}],
               [_fx(1, "2026-11-21"), _fx(2, "2026-11-21")])
    seq = iter([[vid("g", GLOBAL, "found")], hc.QuotaCapReached("cap")])

    def fake(*a, **k):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ha, "resolve_videos_for_fixture", fake)
    try:
        ha.find_alternatives("k", _Quota(), 10**9, {}, now=NOW, root=tmp_path / "highlights",
                             fixtures_dir=tmp_path / "fixtures")
        raise AssertionError("expected QuotaCapReached")
    except hc.QuotaCapReached:
        pass
    m = json.loads(p.read_text())["matches"]
    assert [v["video_id"] for v in m[0]["videos"]] == ["a", "g"]


def test_broadcaster_clip_taken_only_when_it_adds_a_missing_country(monkeypatch):
    """A match missing the US (channel clip blocks US/CA/ES) takes the US
    broadcaster's clip; a match missing only GB/IE/ES refuses it and moves on."""
    regions = {"UUcomp": {"blocked": ["CA", "ES", "US"]},
               "PLbroadcaster": {"allowed": ["PR", "US"]},
               "UUhome": None}
    searched = []

    def fake_search(playlist_id, *a, **k):
        searched.append(playlist_id)
        return [{"video_id": playlist_id, "title": f"Arsenal v Chelsea | Highlights {playlist_id}",
                 "published_at": "2026-11-21"}]

    monkeypatch.setattr(hc, "search_playlist", fake_search)
    monkeypatch.setattr(hc, "find_gameweek_playlist", lambda *a, **k: None)
    monkeypatch.setattr(hc, "fetch_video_details", lambda ids, *a: {
        i: {"duration_seconds": 300, "is_portrait": False, "is_embeddable": True,
            "is_region_restricted": regions[i] is not None, "is_playable_nowhere": False,
            "region_restriction": regions[i]} for i in ids})

    missing_us = [vid("laliga", {"blocked": ["CA", "ES", "US"]})]
    got = hc.resolve_videos_for_fixture(FIX, "Premier League", _config(), "k", object(), 10**9,
                                        accept=ha.make_accept(missing_us))
    assert [v["video_id"] for v in got] == ["PLbroadcaster"]
    assert ha.uncovered(missing_us + got) == ["CA", "ES"]

    searched.clear()
    regions["UUcomp"] = {"allowed": ["US"]}    # now every clip before the club's is US-only
    missing_gb = [vid("cbs", US_ONLY)]
    got = hc.resolve_videos_for_fixture(FIX, "Premier League", _config(), "k", object(), 10**9,
                                        accept=ha.make_accept(missing_gb))
    assert searched == ["UUcomp", "PLbroadcaster", "UUhome"]   # US-only clips refused
    assert [v["video_id"] for v in got] == ["UUhome"]


def test_fixture_index_matches_the_matchers_shape(tmp_path):
    fx = tmp_path / "serie-a"
    fx.mkdir()
    (fx / "2026.json").write_text(json.dumps({"fixtures": [_fx(7, "2026-11-21")]}))
    f = ha.fixture_index("serie-a", 2026, tmp_path)[7]
    assert (f["home_short"], f["away_tla"], f["date"], f["matchday"]) == ("Lazio", "LEC", "2026-11-21", 12)
    assert f["score"]["fullTime"] == {"home": 2, "away": 0}


def test_fetch_run_calls_pass_after_region_fill():
    from pathlib import Path
    src = (Path(hc.__file__).parent / "fetch_highlights.py").read_text(encoding="utf-8")
    assert src.index("find_alternatives(") > src.index("fill_missing_regions(yt_key")


# ── last season + records without fixture data ────────────────────────────────

def test_fixture_from_record_builds_a_matcher_fixture(tmp_path):
    p = tmp_path / "matchday-3.json"
    f = ha.fixture_from_record({"match_id": 5, "home_team": "Arsenal FC", "away_team": "SSC Napoli",
                                "date": "2025-10-01", "videos": []}, p)
    assert (f["home_short"], f["away_short"], f["matchday"], f["stage"], f["score"]) == \
        ("Arsenal FC", "SSC Napoli", 3, "LEAGUE_STAGE", {})
    assert ha.fixture_from_record({"match_id": 6, "home_team": "A", "date": "2025-10-01"}, p) is None
    g = ha.fixture_from_record({"match_id": 7, "home_team": "A", "away_team": "B", "date": "2025-10-01"},
                               tmp_path / "final.json")
    assert g["matchday"] is None and g["stage"] == "REGULAR_SEASON"


def test_catchup_includes_last_season_after_this_one(tmp_path, monkeypatch):
    monkeypatch.setattr(ha, "ALT_COMPS", {"Serie A"})
    monkeypatch.setattr(ha, "season_for_competition", lambda comp, now=None: 2026)
    root = tmp_path / "highlights" / "serie-a"
    for season, date, mid in [(2026, "2026-10-04", 1), (2025, "2026-05-24", 2), (2024, "2025-05-25", 3)]:
        (root / str(season)).mkdir(parents=True)
        (root / str(season) / "gameweek-38.json").write_text(json.dumps({"matches": [
            {"match_id": mid, "home_team": "SS Lazio", "away_team": "US Lecce", "date": date,
             "videos": [vid(f"c{mid}")]}]}), encoding="utf-8")
    recent, catchup = ha._candidates(tmp_path / "highlights", tmp_path / "fixtures", NOW,
                                     tmp_path / "tournament-groups")
    assert recent == []
    assert [it[3]["match_id"] for it in catchup] == [1, 2]     # 2024 is out of range
    assert catchup[1][4]["home_short"] == "SS Lazio"           # built from the record


def test_ucl_is_current_season_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ha, "ALT_COMPS", {"Champions League", "Serie A"})
    monkeypatch.setattr(ha, "season_for_competition", lambda comp, now=None: 2026)
    for slug, season, mid in [("ucl", 2026, 1), ("ucl", 2025, 2), ("serie-a", 2025, 3)]:
        d = tmp_path / "highlights" / slug / str(season)
        d.mkdir(parents=True, exist_ok=True)
        (d / "matchday-1.json").write_text(json.dumps({"matches": [
            {"match_id": mid, "home_team": "A", "away_team": "B", "date": f"{season}-10-01",
             "videos": [vid(f"c{mid}")]}]}), encoding="utf-8")
    _, catchup = ha._candidates(tmp_path / "highlights", tmp_path / "fixtures", NOW,
                                tmp_path / "tournament-groups")
    assert sorted(it[3]["match_id"] for it in catchup) == [1, 3]   # UCL 2025 skipped


def test_ucl_archive_season_is_read(tmp_path):
    arch = tmp_path / "tournament-groups" / "ucl"
    arch.mkdir(parents=True)
    (arch / "2025.json").write_text(json.dumps({"season": 2025, "groupMatches": [], "matches": [
        {"id": 77, "utcDate": "2026-05-30T16:00:00Z", "stage": "FINAL",
         "homeTeam": {"name": "Paris Saint-Germain FC", "tla": "PSG"},
         "awayTeam": {"name": "Arsenal FC", "tla": "ARS"}}]}))
    idx = ha.fixture_index("ucl", 2025, tmp_path / "fixtures", tmp_path / "tournament-groups")
    assert idx[77]["stage"] == "FINAL" and idx[77]["home_tla"] == "PSG"
