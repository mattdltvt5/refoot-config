"""broadcasterChannels: a broadcaster's channel uploads as a tier-4 source, gated
by a required title phrase (TNT Sports Football's UCL highlights, GB/IE), and
the alternatives pass covering the Champions League. No API calls."""
import json

import highlights_common as hc
import highlight_alternatives as ha
import source_health_scan as scan

TNT = "UC4i_9WvfPRTuRWEaWyfKuFw"


def test_load_sources_parses_broadcaster_channels(tmp_path, monkeypatch):
    raw = {"competitions": {}, "teams": {}, "playlists": {}, "teamPlaylists": {},
           "broadcasterChannels": {"Champions League": [
               {"label": "TNT Sports Football", "channelId": TNT,
                "requiredTerms": ["Champions League Highlights"], "countries": ["gb", "IE"]},
               {"label": "bad, no id", "channelId": "https://youtube.com/x", "requiredTerms": ["x"]},
               {"label": "bad, no terms", "channelId": TNT, "requiredTerms": []},
           ]}}
    p = tmp_path / "sources.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(hc, "SOURCES_JSON", p)
    cfg = hc.load_sources()
    assert cfg["broadcaster_channels"] == {"Champions League": [{
        "label": "TNT Sports Football", "channel_id": TNT,
        "required_terms": ["champions league highlights"], "countries": ["GB", "IE"]}]}


def test_real_sources_json_has_tnt_for_ucl():
    bc = hc.load_sources()["broadcaster_channels"]["Champions League"]
    assert [(b["label"], b["channel_id"], b["countries"]) for b in bc] == \
        [("TNT Sports Football", TNT, ["GB", "IE"])]


FIX = {"date": "2026-11-04", "home_team": "Lens", "home_short": "Lens", "home_tla": "RCL",
       "away_team": "Como 1907", "away_short": "Como", "away_tla": "COM"}


def _cfg():
    return {"team_playlists": {}, "competition_channels": {},
            "competition_playlists": {"Champions League": {"CBS": ["PLcbs"]}},
            "team_channels": {"Lens": "UClens"},
            "broadcaster_channels": {"Champions League": [
                {"label": "TNT", "channel_id": TNT,
                 "required_terms": ["champions league highlights"], "countries": ["GB", "IE"]}]}}


def test_matcher_reads_channel_after_playlists_with_required_terms(monkeypatch):
    calls = []

    def fake_search(playlist_id, *a, required_terms=(), **k):
        calls.append((playlist_id, tuple(required_terms)))
        return []

    monkeypatch.setattr(hc, "search_playlist", fake_search)
    monkeypatch.setattr(hc, "find_gameweek_playlist", lambda *a, **k: None)
    hc.resolve_videos_for_fixture(FIX, "Champions League", _cfg(), "k", object(), 10**9)
    assert calls == [("PLcbs", ()), ("UU" + TNT[2:], ("champions league highlights",)),
                     ("UUlens", ())]
    calls.clear()
    hc.resolve_videos_for_fixture(FIX, "Champions League", _cfg(), "k", object(), 10**9,
                                  skip_broadcasters=True)
    assert [c[0] for c in calls] == ["UUlens"]


def test_required_phrase_keeps_other_competitions_out():
    terms = ["champions league highlights"]
    assert hc.is_highlight_title("Como Make UCL History | Lens 1-9 Como | UEFA Champions League Highlights",
                                 required_terms=terms)
    assert not hc.is_highlight_title("Man City 1-1 Fulham | Premier League Highlights",
                                     required_terms=terms)


def test_config_without_key_still_works(monkeypatch):
    cfg = _cfg()
    del cfg["broadcaster_channels"]
    monkeypatch.setattr(hc, "search_playlist", lambda *a, **k: [])
    monkeypatch.setattr(hc, "find_gameweek_playlist", lambda *a, **k: None)
    assert hc.resolve_videos_for_fixture(FIX, "Champions League", cfg, "k", object(), 10**9) == []


# ── alternatives pass covers UCL ──────────────────────────────────────────────

def test_ucl_is_in_scope_and_tournaments_are_not():
    assert "Champions League" in ha.ALT_COMPS
    assert not ({"World Cup", "Euro Cup", "Copa America"} & ha.ALT_COMPS)


def test_fixture_index_reads_ucl_from_tournament_groups(tmp_path):
    groups = tmp_path / "tournament-groups"
    groups.mkdir()
    (groups / "ucl.json").write_text(json.dumps({"season": 2026, "groupMatches": [{
        "match_id": 575323, "utcDate": "2026-09-08T16:45:00Z", "group": "LEAGUE_PHASE",
        "matchday": 1, "status": "FINISHED",
        "homeTeam": {"name": "Club Brugge KV", "tla": "BRU"},
        "awayTeam": {"name": "Aston Villa FC", "tla": "AVL"},
        "score": {"fullTime": {"home": 2, "away": 3}}}],
        "matches": [{"id": 9, "utcDate": "2027-02-17T20:00:00Z", "stage": "LAST_16",
                     "matchday": 1, "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}}]}))
    idx = ha.fixture_index("ucl", 2026, tmp_path / "fixtures", groups)
    f = idx[575323]
    assert (f["home_short"], f["away_tla"], f["matchday"], f["stage"], f["date"]) == \
        ("Club Brugge KV", "AVL", 1, "LEAGUE_STAGE", "2026-09-08")
    assert idx[9]["stage"] == "LAST_16"
    assert ha.fixture_index("ucl", 2027, tmp_path / "fixtures", groups) == {}   # other season


# ── weekly scan knows the source kind ─────────────────────────────────────────

def test_scan_lists_and_gates_broadcaster_channel():
    srcs = scan.configured_sources("Champions League", _cfg(), {"teamLists": {}})
    bc = [s for s in srcs if s["kind"] == "broadcaster_channel"]
    assert bc == [{"kind": "broadcaster_channel", "tier": 4, "label": "TNT", "channel_id": TNT,
                   "playlist_id": "UU" + TNT[2:], "required_terms": ["champions league highlights"]}]
    terms = bc[0]["required_terms"]
    assert scan.title_passes("broadcaster_channel", "Champions League",
                             "Porto 4-1 Napoli | UEFA Champions League Highlights", terms)
    assert not scan.title_passes("broadcaster_channel", "Champions League",
                                 "Juventus 4-0 Venezia | Serie A Highlights", terms)


def test_rights_registry_records_tnt_for_gb_and_ie():
    import rights_registry as rr
    from datetime import date
    for cc in ("GB", "IE"):
        got = [e for e in rr.active_entries("ucl", cc, date(2026, 11, 22))
               if e["status"] == "approved"]
        assert [(e["holder"], e["role"]) for e in got] == [("TNT Sports", "highlights")]
        assert rr.entry_source_ids(got[0]) == {"UU" + TNT[2:]}
