"""Provider registry refactor (PR 1 of the Europa League work).

The four job scripts (fetch_highlights, backfill_highlights, sync_standings,
sync_tournaments) take their competitions from highlights_common.COMPETITION_CONFIG
and their data through fixture_providers' registry. These tests pin that the
refactor changed nothing observable: same competitions per script, same output
paths and shapes, API-Sports never constructed by the generic jobs, the merge
guard intact. Since PR 2 the Europa League is a dormant TheSportsDB entry: no job
requests it (it used to be requested from football-data and refused with 403).

All HTTP is mocked: nothing here reaches football-data.org, API-Sports,
TheSportsDB or YouTube.
"""

import json
import os
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import highlights_common as hc  # noqa: E402
import fixture_providers as fp  # noqa: E402
import sync_standings  # noqa: E402
import sync_tournaments  # noqa: E402

# ── The pre-refactor hardcoded lists ("before" sets) ──────────────────────────

# highlights_common.COMPETITION_CODE_MAP as it was (fetch + backfill loops),
# minus the Europa League: it moved to TheSportsDB (dormant) in PR 2, so
# football-data is no longer asked for it.
BEFORE_HIGHLIGHTS = [
    ("PL", "Premier League"), ("PD", "LaLiga"), ("SA", "Serie A"),
    ("BL1", "Bundesliga"), ("FL1", "Ligue 1"), ("CL", "Champions League"),
    ("EC", "Euro Cup"), ("WC", "World Cup"),
]
# sync_standings.COMPETITIONS as it was: (FD id, name, slug).
BEFORE_STANDINGS = [
    (2021, "Premier League", "premier-league"), (2014, "LaLiga", "laliga"),
    (2019, "Serie A", "serie-a"), (2002, "Bundesliga", "bundesliga"),
    (2015, "Ligue 1", "ligue-1"), (2001, "Champions League", "ucl"),
]
# sync_tournaments.TOURNAMENT_COMPETITIONS as it was.
BEFORE_TOURNAMENTS = [
    (2018, "Euro Cup", "euro-cup"), (2000, "World Cup", "world-cup"),
    (2001, "Champions League", "ucl"),
]
DOMESTIC = {"Premier League", "LaLiga", "Serie A", "Bundesliga", "Ligue 1"}


@pytest.fixture(autouse=True)
def _no_sleep_no_network(monkeypatch):
    monkeypatch.setattr(fp.time, "sleep", lambda *_: None)
    monkeypatch.setattr(sync_standings.time, "sleep", lambda *_: None)
    monkeypatch.setattr(sync_tournaments.time, "sleep", lambda *_: None)

    def _blocked(*a, **k):
        raise AssertionError("real network call attempted")
    monkeypatch.setattr("urllib.request.urlopen", _blocked)
    monkeypatch.setattr("requests.get", _blocked)
    monkeypatch.delenv("APISPORTS_API_KEY", raising=False)


def _names(job, capability):
    return [(n, c) for n, c, _ in fp.iter_job_providers(job, capability, {"fd_key": "k"},
                                                        report=lambda _l: None)]


# ── Same competitions per script, before and after ────────────────────────────


def test_highlights_jobs_process_the_same_competitions_in_the_same_order():
    got = [(c["fd_code"], n) for n, c in _names(hc.JOB_HIGHLIGHTS, fp.CAP_FIXTURES)]
    assert got == BEFORE_HIGHLIGHTS
    # The derived map other callers use is byte-for-byte the old one.
    assert list(hc.COMPETITION_CODE_MAP.items()) == BEFORE_HIGHLIGHTS


def test_standings_job_processes_the_same_competitions():
    got = [(c["fd_id"], n, c["slug"]) for n, c, _ in sync_standings.standings_providers("k")]
    assert got == BEFORE_STANDINGS


def test_tournament_job_processes_the_same_competitions_in_the_same_order():
    # Order matters here: the UCL cache must come last, after the pauses free
    # football-data's rate limit (first, it hit HTTP 429 on every run).
    got = [(c["fd_id"], n, c["slug"]) for n, c, _ in sync_tournaments.tournament_providers("k")]
    assert got == BEFORE_TOURNAMENTS


def test_job_order_ranks_first_then_table_order():
    cfg = {
        "A": {"slug": "a", "provider": "footballdata", "jobs": ["x"]},
        "B": {"slug": "b", "provider": "footballdata", "jobs": ["x"], "job_order": {"x": 1}},
        "C": {"slug": "c", "provider": "footballdata", "jobs": ["x"], "job_order": {"x": 0}},
        "D": {"slug": "d", "provider": "footballdata", "jobs": ["x"], "job_order": {"y": 0}},
    }
    with patch.dict(hc.COMPETITION_CONFIG, cfg, clear=True):
        assert [n for n, _ in hc.competitions_for_job("x")] == ["C", "B", "A", "D"]


def test_config_slugs_match_the_output_slug_map():
    for name, cfg in hc.COMPETITION_CONFIG.items():
        assert hc.COMPETITION_SLUG_MAP[name] == cfg["slug"], name
        fp.provider_class(cfg["provider"], name)  # every value is registered


def test_copa_keeps_apisports_and_europa_is_a_dormant_thesportsdb_entry():
    assert hc.COMPETITION_CONFIG["Copa America"]["provider"] == hc.PROVIDER_APISPORTS
    uel = hc.COMPETITION_CONFIG["Europa League"]
    assert uel["provider"] == hc.PROVIDER_THESPORTSDB
    assert uel["tsdb_league_id"] == 4481
    assert uel["enabled"] is False


def test_fetch_all_fixtures_calls_each_competition_and_full_season_for_leagues_only():
    import fetch_highlights as fh
    fixtures_calls, season_calls = [], []

    def fake_fixtures(self, code, comp_name, season):
        fixtures_calls.append(code)
        return {"gameweek-1": [{"match_id": code}]}

    def fake_full(self, code, comp_name, season):
        season_calls.append(comp_name)
        return [{"match_id": code}]

    with patch.object(fp.FootballDataProvider, "get_fixtures", fake_fixtures), \
         patch.object(fp.FootballDataProvider, "get_full_season", fake_full):
        highlights, artifacts = fh.fetch_all_fixtures("k")
    assert fixtures_calls == [code for code, _ in BEFORE_HIGHLIGHTS]
    assert set(season_calls) == DOMESTIC
    assert set(highlights) == {n for _, n in BEFORE_HIGHLIGHTS}
    assert set(artifacts) == DOMESTIC


def test_backfill_loops_over_the_highlights_job_through_the_registry():
    import backfill_highlights as bh
    src = Path(bh.__file__).read_text(encoding="utf-8")
    assert "iter_job_providers(" in src
    assert "JOB_HIGHLIGHTS, CAP_FIXTURES" in src
    # fetch_season_fixtures delegates to the provider it is handed.
    provider = MagicMock()
    provider.competition_fixtures.return_value = {"matchday-1": []}
    cfg = hc.COMPETITION_CONFIG["Champions League"]
    assert bh.fetch_season_fixtures(provider, "Champions League", cfg, 2026) == {"matchday-1": []}
    provider.competition_fixtures.assert_called_once_with("Champions League", cfg, 2026)


@pytest.mark.parametrize("name", ["fetch_highlights.py", "backfill_highlights.py",
                                  "sync_standings.py", "sync_tournaments.py"])
def test_job_scripts_never_name_the_football_data_list_or_client(name):
    src = (Path(hc.__file__).parent / name).read_text(encoding="utf-8")
    for banned in ("COMPETITION_CODE_MAP", "TOURNAMENT_COMPETITIONS", "FootballDataProvider(",
                   "fd_get(", "FD_BASE", "api.football-data.org", "urllib.request"):
        assert banned not in src, f"{name} still references {banned}"


# ── API-Sports is never built or called by the generic jobs ───────────────────


def test_apisports_provider_is_skipped_before_construction_with_a_log_line():
    lines = []
    with patch.object(fp.ApiSportsProvider, "__init__",
                      side_effect=AssertionError("ApiSportsProvider constructed")):
        got = [n for n, _, _ in fp.iter_job_providers(
            hc.JOB_HIGHLIGHTS, fp.CAP_FIXTURES, {"fd_key": "k"}, report=lines.append)]
    assert "Copa America" not in got
    assert any("Copa America" in l and "apisports" in l for l in lines)


def test_fetch_all_fixtures_never_constructs_apisports():
    import fetch_highlights as fh
    with patch.object(fp.ApiSportsProvider, "__init__",
                      side_effect=AssertionError("ApiSportsProvider constructed")), \
         patch.object(fp.FootballDataProvider, "get_fixtures", lambda *a: {}), \
         patch.object(fp.FootballDataProvider, "get_full_season", lambda *a: []):
        fh.fetch_all_fixtures("k")


# ── Europa League: dormant, so no job requests it ─────────────────────────────


def test_dormant_europa_league_is_requested_by_no_job():
    import fetch_highlights as fh
    for job in (hc.JOB_HIGHLIGHTS, hc.JOB_STANDINGS, hc.JOB_TOURNAMENT_GROUPS):
        assert "Europa League" not in [n for n, _ in hc.competitions_for_job(job)]
    urls = []

    def fake_fd_get(url, key, params=None):
        urls.append(url)
        resp = MagicMock()
        resp.status_code, resp.ok = 200, True
        resp.json.return_value = {"matches": [{
            "id": hash(url) % 10**6, "status": "FINISHED", "stage": "REGULAR_SEASON",
            "matchday": 1, "utcDate": "2026-09-01T19:00:00Z",
            "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"}, "score": {}}]}
        return resp

    with patch.object(fp, "fd_get", fake_fd_get),          patch.object(fp.TheSportsDBProvider, "__init__",
                      side_effect=AssertionError("TheSportsDBProvider constructed")):
        highlights, _ = fh.fetch_all_fixtures("k")
    assert "Europa League" not in highlights
    assert "Premier League" in highlights
    assert not any("/competitions/EL/" in u for u in urls)


# ── Capability model + unknown providers ──────────────────────────────────────


class _FixturesOnly(fp.FixtureProvider):
    CAPABILITIES = frozenset({fp.CAP_FIXTURES})


def test_base_class_supports_nothing_and_capabilities_raise():
    base = fp.FixtureProvider()
    assert not base.supports(fp.CAP_STANDINGS)
    assert not base.supports(fp.CAP_TOURNAMENT_GROUPS)
    with pytest.raises(NotImplementedError):
        base.get_standings("X", {})
    with pytest.raises(NotImplementedError):
        base.get_tournament_groups("X", {})


def test_provider_without_a_capability_is_skipped_with_a_log_line(capsys):
    cfg = {"Test Cup": {"slug": "test-cup", "provider": "fixtures-only",
                        "jobs": [hc.JOB_STANDINGS, hc.JOB_TOURNAMENT_GROUPS]}}
    with patch.dict(fp.PROVIDERS, {"fixtures-only": _FixturesOnly}), \
         patch.dict(hc.COMPETITION_CONFIG, cfg, clear=True):
        assert sync_standings.standings_providers("k") == []
        assert sync_tournaments.tournament_providers("k") == []
        sync_standings.main("k", out_dir="unused-not-written")  # no error
    err = capsys.readouterr().err
    assert "Test Cup" in err and "'standings'" in err
    assert "'tournament_groups'" in err


def test_unknown_provider_value_fails_clearly():
    with pytest.raises(fp.UnknownProviderError) as e:
        fp.get_provider("thesportsdb-typo", {}, competition="Europa League")
    msg = str(e.value)
    assert "Europa League" in msg and "thesportsdb-typo" in msg
    cfg = {"Mystery League": {"slug": "mystery", "provider": "nope", "jobs": [hc.JOB_HIGHLIGHTS]}}
    with patch.dict(hc.COMPETITION_CONFIG, cfg, clear=True):
        with pytest.raises(fp.UnknownProviderError, match="Mystery League.*'nope'"):
            list(fp.iter_job_providers(hc.JOB_HIGHLIGHTS, fp.CAP_FIXTURES, {}))


def test_disabled_competition_is_skipped():
    cfg = dict(hc.COMPETITION_CONFIG)
    cfg["LaLiga"] = {**cfg["LaLiga"], "enabled": False}
    with patch.dict(hc.COMPETITION_CONFIG, cfg, clear=True):
        names = [n for n, _ in _names(hc.JOB_HIGHLIGHTS, fp.CAP_FIXTURES)]
    assert "LaLiga" not in names and "Premier League" in names


# ── FootballDataProvider standings / tournament groups: same calls, same shape ─


def _mock_response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda self: self
    resp.__exit__ = MagicMock(return_value=False)
    return resp


STANDINGS = {"filters": {"season": "2026"}, "standings": [
    {"type": "TOTAL", "group": None, "table": [{"position": 1, "team": {"id": 57, "crest": "c"}}]},
    {"type": "HOME", "table": []}]}
MATCHES = {"filters": {"season": "2026"}, "matches": [
    {"id": 1, "stage": "LEAGUE_STAGE", "group": None, "matchday": 1, "status": "FINISHED",
     "utcDate": "2026-09-16T19:00:00Z", "homeTeam": {"id": 1, "name": "A", "crest": ""},
     "awayTeam": {"id": 2, "name": "B", "crest": ""},
     "score": {"fullTime": {"home": 2, "away": 1}}},
    {"id": 2, "stage": "FINAL", "group": None, "matchday": None, "status": "TIMED",
     "utcDate": "2027-05-29T19:00:00Z", "homeTeam": {"id": 3, "name": "C", "crest": ""},
     "awayTeam": {"id": 4, "name": "D", "crest": ""},
     "score": {"fullTime": {"home": None, "away": None}}}]}


def test_football_data_standings_call_and_payload_unchanged():
    cfg = hc.COMPETITION_CONFIG["Premier League"]
    with patch("urllib.request.urlopen", return_value=_mock_response(STANDINGS)) as m:
        got = fp.FootballDataProvider("secret", base_url="https://mock.api").get_standings(
            "Premier League", cfg, season=2026)
    req = m.call_args[0][0]
    assert req.get_full_url() == "https://mock.api/competitions/2021/standings?season=2026"
    assert req.get_header("X-auth-token") == "secret"
    assert got == STANDINGS


def test_football_data_tournament_groups_calls_and_payloads_unchanged():
    cfg = hc.COMPETITION_CONFIG["Champions League"]
    with patch("urllib.request.urlopen",
               side_effect=[_mock_response(STANDINGS), _mock_response(MATCHES)]) as m:
        standings, matches = fp.FootballDataProvider(
            "k", base_url="https://mock.api").get_tournament_groups("Champions League", cfg)
    urls = [c[0][0].get_full_url() for c in m.call_args_list]
    # Standings first, then matches; no season and no status filter (as before).
    assert urls == ["https://mock.api/competitions/2001/standings",
                    "https://mock.api/competitions/2001/matches"]
    assert (standings, matches) == (STANDINGS, MATCHES)


def test_football_data_http_error_still_propagates():
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(urllib.error.HTTPError):
            fp.FootballDataProvider("k").get_standings(
                "Champions League", hc.COMPETITION_CONFIG["Champions League"])


def test_sync_standings_writes_the_same_paths_and_shape(tmp_path):
    with patch.object(sync_standings, "current_season", return_value=2026), \
         patch.object(fp, "fd_fetch_standings", return_value=json.loads(json.dumps(STANDINGS))) as m:
        sync_standings.main("k", out_dir=str(tmp_path))
    assert [c.args[0] for c in m.call_args_list] == [i for i, _, _ in BEFORE_STANDINGS]
    for _, name, slug in BEFORE_STANDINGS:
        data = json.loads((tmp_path / "standings" / slug / "2026.json").read_text(encoding="utf-8"))
        assert set(data) == {"generated_at", "competition", "slug", "season", "standings"}
        assert (data["competition"], data["slug"], data["season"]) == (name, slug, 2026)
        assert data["standings"] == [{"position": 1, "team": {"id": 57, "crest": "c"}}]


def test_sync_tournaments_writes_the_same_paths_and_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_tournaments, "graft_video_ids", lambda *_a, **_k: None)
    with patch.object(fp, "fd_fetch_standings", side_effect=lambda *a, **k: json.loads(json.dumps(STANDINGS))), \
         patch.object(fp, "fd_fetch_matches", side_effect=lambda *a, **k: json.loads(json.dumps(MATCHES))):
        sync_tournaments.main("k", out_dir=str(tmp_path))
    for _, _, slug in BEFORE_TOURNAMENTS:
        written = json.loads((tmp_path / "tournament-groups" / f"{slug}.json").read_text(encoding="utf-8"))
        expected = sync_tournaments.build_tournament_data(
            slug, json.loads(json.dumps(STANDINGS)), json.loads(json.dumps(MATCHES)), {})
        drop = lambda d: {k: v for k, v in d.items() if k != "generated_at"}  # noqa: E731
        assert drop(written) == drop(expected)
        # The per-season archive is still written next to it.
        assert (tmp_path / "tournament-groups" / slug / "2026.json").exists()


def test_tournament_http_error_keeps_existing_cache(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sync_tournaments, "graft_video_ids", lambda *_a, **_k: None)
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    with patch.object(fp, "fd_fetch_standings", side_effect=err):
        sync_tournaments.main("k", out_dir=str(tmp_path))
    assert not (tmp_path / "tournament-groups").exists()
    assert "HTTP 403" in capsys.readouterr().err


# ── Merge guard still holds ───────────────────────────────────────────────────


FINISHED = {"match_id": 9, "status": "FINISHED", "utcDate": "2026-09-01T19:00:00Z",
            "score": {"fullTime": {"home": 0, "away": 0}}}


def test_finished_scored_record_not_overwritten_by_null_score():
    incoming = {"match_id": 9, "status": "TIMED", "utcDate": "2026-09-01T19:00:00Z",
                "score": {"fullTime": {"home": None, "away": None}}}
    # 0-0 counts as resolved.
    assert fp.merge_preserve_finished(FINISHED, incoming) == FINISHED
    assert fp.merge_fixtures_preserving_finished([FINISHED], [incoming]) == [FINISHED]


def test_terminal_status_still_replaces_a_finished_record():
    postponed = {"match_id": 9, "status": "POSTPONED", "utcDate": "2026-09-01T19:00:00Z",
                 "score": {"fullTime": {"home": None, "away": None}}}
    assert fp.merge_preserve_finished(FINISHED, postponed) == postponed
