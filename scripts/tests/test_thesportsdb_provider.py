"""TheSportsDBProvider (PR 2 of the Europa League work).

TheSportsDB events become football-data-shaped matches and standings, rounds are
cached so the ~5-minute runs re-request only rounds that can still change, and
the free key's rate limit is respected. All HTTP is mocked: nothing here reaches
TheSportsDB, football-data.org, API-Sports or YouTube.
"""

import io
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import highlights_common as hc  # noqa: E402
import fixture_providers as fp  # noqa: E402
import sync_standings  # noqa: E402
import sync_tournaments  # noqa: E402

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
OFF = fp.TSDB_ID_OFFSET
UEL = {**hc.COMPETITION_CONFIG["Europa League"], "enabled": True}

FD_TEAMS = [
    {"id": 521, "name": "Lille OSC", "shortName": "Lille", "tla": "LIL",
     "crest": "https://crests.football-data.org/521.png"},
    {"id": 58, "name": "Aston Villa FC", "shortName": "Aston Villa", "tla": "AVL",
     "crest": "https://crests.football-data.org/58.png"},
]


def ev(event_id, rnd, ts, home, away, status="FT", hs=None, as_=None, **extra):
    """A raw TheSportsDB event; home/away are (id, name)."""
    e = {
        "idEvent": str(event_id), "idLeague": "4481", "intRound": str(rnd),
        "strTimestamp": ts, "dateEvent": ts[:10], "strTime": ts[11:],
        "idHomeTeam": str(home[0]), "strHomeTeam": home[1],
        "strHomeTeamBadge": f"https://r2.thesportsdb.com/{home[0]}.png",
        "idAwayTeam": str(away[0]), "strAwayTeam": away[1],
        "strAwayTeamBadge": f"https://r2.thesportsdb.com/{away[0]}.png",
        "intHomeScore": None if hs is None else str(hs),
        "intAwayScore": None if as_ is None else str(as_),
        "intHomeScoreExtra": None, "intAwayScoreExtra": None,
        "strStatus": status, "strPostponed": "no",
        "strPoster": "https://r2.thesportsdb.com/poster.jpg",   # not cached
    }
    e.update(extra)
    return e


LIL, AVL = (133711, "Lille"), (133601, "Aston Villa")
CEL, BEN = (133647, "Celtic"), (134108, "Benfica")
FER = (134620, "Ferencváros")

ROUNDS = {
    1: [
        # A July qualifier sharing round number 1 - dropped by the season start.
        ev(1, 1, "2026-07-10T18:00:00", (9001, "Larne"), (9002, "Iberia 1999"), "FT", 1, 2),
        ev(10, 1, "2026-09-24T19:00:00", LIL, CEL, "FT", 2, 0),
        ev(11, 1, "2026-09-24T19:00:00", AVL, BEN, "FT", 1, 1),
    ],
    2: [
        ev(20, 2, "2026-10-01T16:45:00", CEL, AVL, "FT", 0, 3),
        ev(21, 2, "2026-10-01T16:45:00", BEN, LIL, "FT", 2, 2),
    ],
    3: [ev(30, 3, "2026-12-10T20:00:00", LIL, AVL, "NS")],
    16: [
        ev(160, 16, "2027-03-11T17:45:00", LIL, AVL, "FT", 0, 1),
        ev(161, 16, "2027-03-18T20:00:00", AVL, LIL, "FT", 2, 0),
    ],
    150: [
        ev(150, 150, "2027-04-30T19:00:00", BEN, FER, "AET", 2, 1),
        ev(151, 150, "2027-05-07T19:00:00", FER, BEN, "PEN", 1, 0,
           intHomeScoreExtra="4", intAwayScoreExtra="3"),
    ],
    200: [ev(200, 200, "2027-05-26T19:00:00", AVL, FER, "NS")],
}

SMALL = {**UEL, "tsdb_league_rounds": 3}


class FakeTsdb:
    """Stands in for urllib.request.urlopen: serves ROUNDS by r= and records calls."""

    def __init__(self, rounds=None, fail=None):
        self.rounds = ROUNDS if rounds is None else rounds
        self.fail = fail or {}      # round -> list of HTTP codes to raise first
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req.get_full_url() if hasattr(req, "get_full_url") else req
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        rnd = int(q["r"][0])
        self.calls.append((url, rnd, q["s"][0]))
        codes = self.fail.get(rnd) or []
        if codes:
            raise urllib.error.HTTPError(url, codes.pop(0), "err", {}, None)
        events = self.rounds.get(rnd)
        return io.BytesIO(json.dumps({"events": events or None}).encode())


@pytest.fixture(autouse=True)
def _no_sleep_no_network(monkeypatch):
    sleeps = []
    # fp.time, sync_standings.time and sync_tournaments.time are one module:
    # patch it once, or a later patch replaces this recorder.
    assert fp.time is sync_standings.time is sync_tournaments.time
    monkeypatch.setattr(fp.time, "sleep", sleeps.append)

    def _blocked(*a, **k):
        raise AssertionError("real network call attempted")
    monkeypatch.setattr("urllib.request.urlopen", _blocked)
    monkeypatch.setattr("requests.get", _blocked)
    monkeypatch.delenv("THESPORTSDB_API_KEY", raising=False)
    return sleeps


def provider(tmp_path, now=NOW, key="123"):
    return fp.TheSportsDBProvider(
        key, cache_path=tmp_path / "rounds.json",
        team_index=fp.TsdbTeamIndex(FD_TEAMS), now=lambda: now)


def run(fake, fn):
    with patch("urllib.request.urlopen", fake):
        return fn()


# ── Field conversions ─────────────────────────────────────────────────────────


def test_kickoff_is_utc_with_a_z():
    assert fp.tsdb_kickoff({"strTimestamp": "2026-03-12T17:45:00"}) == "2026-03-12T17:45:00Z"
    assert fp.tsdb_kickoff({"strTimestamp": "2026-03-12T17:45:00+01:00"}) == "2026-03-12T16:45:00Z"
    assert fp.tsdb_kickoff({"dateEvent": "2026-03-12", "strTime": "20:00:00"}) == "2026-03-12T20:00:00Z"
    assert fp.tsdb_kickoff({}) is None
    assert fp.tsdb_kickoff({"strTimestamp": "soon"}) is None


@pytest.mark.parametrize("raw,want", [
    ("FT", "FINISHED"), ("AET", "FINISHED"), ("PEN", "FINISHED"), ("Match Finished", "FINISHED"),
    ("NS", "TIMED"), ("", "TIMED"), ("1H", "IN_PLAY"), ("ET", "IN_PLAY"), ("HT", "PAUSED"),
    ("PST", "POSTPONED"), ("CANC", "CANCELLED"), ("ABD", "SUSPENDED"), ("AWD", "AWARDED"),
    ("Weird", "Weird"),
])
def test_status_mapping(raw, want):
    assert fp.tsdb_status({"strStatus": raw}) == want


def test_postponed_flag_wins_over_the_status():
    assert fp.tsdb_status({"strStatus": "NS", "strPostponed": "yes"}) == "POSTPONED"


def test_every_mapped_status_is_one_football_data_documents():
    assert set(fp._TSDB_STATUS.values()) <= fp.FD_STATUSES


def test_shootout_full_time_includes_the_penalties_like_football_data():
    e = ev(1, 150, "2027-05-07T19:00:00", FER, BEN, "PEN", 1, 0,
           intHomeScoreExtra="4", intAwayScoreExtra="3")
    s = fp.tsdb_score(e, "FINISHED")
    assert s["fullTime"] == {"home": 5, "away": 3}
    assert s["penalties"] == {"home": 4, "away": 3}
    assert (s["duration"], s["winner"]) == ("PENALTY_SHOOTOUT", "HOME_TEAM")


def test_extra_time_and_unplayed_scores():
    aet = fp.tsdb_score(ev(1, 150, "2027-04-30T19:00:00", BEN, FER, "AET", 2, 1), "FINISHED")
    assert (aet["duration"], aet["fullTime"], aet["winner"]) == ("EXTRA_TIME", {"home": 2, "away": 1}, "HOME_TEAM")
    ns = fp.tsdb_score(ev(2, 3, "2026-12-10T20:00:00", LIL, AVL, "NS"), "TIMED")
    assert ns["fullTime"] == {"home": None, "away": None} and ns["winner"] is None
    draw = fp.tsdb_score(ev(3, 1, "2026-09-24T19:00:00", AVL, BEN, "FT", 0, 0), "FINISHED")
    assert draw["fullTime"] == {"home": 0, "away": 0} and draw["winner"] == "DRAW"


# ── Team matching ─────────────────────────────────────────────────────────────


def test_team_index_matches_football_data_names_and_refuses_ambiguity():
    idx = fp.TsdbTeamIndex(FD_TEAMS + [
        {"id": 1, "name": "Sporting Clube de Portugal", "shortName": "Sporting"},
        {"id": 2, "name": "Real Sporting de Gijón", "shortName": "Sporting"},
    ])
    assert idx.fd_team("Lille")["id"] == 521
    assert idx.fd_team("LOSC Lille")["id"] == 521
    assert idx.fd_team("Aston Villa")["id"] == 58
    assert idx.fd_team("Sporting") is None          # two FD teams share the key
    assert idx.fd_team("Celtic") is None
    # A club with title aliases but no football-data identity keeps its alias name.
    assert idx.canonical_name("Sparta Prague") == "AC Sparta Praha"


def test_known_teams_come_from_league_fixtures_and_club_tournaments(tmp_path):
    (tmp_path / "fixtures" / "ligue-1").mkdir(parents=True)
    (tmp_path / "fixtures" / "ligue-1" / "2026.json").write_text(json.dumps({"fixtures": [
        {"homeTeam": FD_TEAMS[0], "awayTeam": {"id": 548, "name": "AS Monaco FC"}}]}))
    (tmp_path / "tournament-groups").mkdir()
    (tmp_path / "tournament-groups" / "ucl.json").write_text(json.dumps({
        "matches": [{"homeTeam": FD_TEAMS[1], "awayTeam": {"id": OFF + 5, "name": "TSDB"}}],
        "groupMatches": []}))
    (tmp_path / "tournament-groups" / "world-cup.json").write_text(json.dumps({
        "matches": [{"homeTeam": {"id": 770, "name": "England"}, "awayTeam": {}}]}))
    ids = sorted(t["id"] for t in fp.load_known_fd_teams(tmp_path))
    assert ids == [58, 521, 548]   # no national teams, no TheSportsDB ids


# ── Season build ──────────────────────────────────────────────────────────────


def test_season_matches_are_football_data_shaped(tmp_path):
    p = provider(tmp_path)
    matches = run(FakeTsdb(), lambda: p.season_matches("Europa League", SMALL, 2026))
    by_event = {m["tsdbEventId"]: m for m in matches}
    assert "1" not in by_event                       # qualifier dropped
    assert [m["utcDate"] for m in matches] == sorted(m["utcDate"] for m in matches)
    m = by_event["10"]
    assert m["id"] == OFF + 10
    assert (m["stage"], m["matchday"], m["status"]) == ("LEAGUE_STAGE", 1, "FINISHED")
    assert m["utcDate"] == "2026-09-24T19:00:00Z"
    assert m["homeTeam"] == {**FD_TEAMS[0]}           # matched: football-data identity
    # Unmatched: TheSportsDB id (namespaced) and badge, named by its title alias.
    assert m["awayTeam"] == {"id": OFF + 133647, "name": "Celtic FC", "shortName": "Celtic",
                             "tla": "", "crest": "https://r2.thesportsdb.com/133647.png"}
    assert m["score"]["fullTime"] == {"home": 2, "away": 0}
    # Knockout legs follow kickoff order; the final has none.
    assert (by_event["160"]["stage"], by_event["160"]["matchday"]) == ("LAST_16", 1)
    assert by_event["161"]["matchday"] == 2
    assert (by_event["150"]["matchday"], by_event["151"]["matchday"]) == (1, 2)
    assert (by_event["200"]["stage"], by_event["200"]["matchday"]) == ("FINAL", None)
    assert by_event["30"]["status"] == "TIMED"


def test_competition_fixtures_use_the_existing_file_stems(tmp_path):
    p = provider(tmp_path)
    by_stem = run(FakeTsdb(), lambda: p.competition_fixtures("Europa League", SMALL, 2026))
    assert {k: len(v) for k, v in by_stem.items()} == {
        "matchday-1": 2, "matchday-2": 2, "round-of-16-leg-1": 1, "round-of-16-leg-2": 1,
        "semi-final-leg-1": 1, "semi-final-leg-2": 1}  # unplayed games have no stem yet
    f = by_stem["matchday-1"][0]
    assert set(f) == {"match_id", "home_team", "home_short", "home_tla", "away_team",
                      "away_short", "away_tla", "date", "kickoff", "matchday", "stage",
                      "status", "score"}
    assert (f["home_team"], f["home_short"], f["home_tla"]) == ("Lille OSC", "Lille", "LIL")


def test_league_table_is_computed_from_results(tmp_path):
    p = provider(tmp_path)
    standings = run(FakeTsdb(), lambda: p.get_standings("Europa League", SMALL, 2026))
    group = standings["standings"][0]
    assert (group["type"], group["stage"]) == ("TOTAL", "LEAGUE_STAGE")
    table = [(r["position"], r["team"]["name"], r["playedGames"], r["points"], r["goalDifference"])
             for r in group["table"]]
    # Villa 4 pts GD+3, Lille 4 pts GD+2, Benfica 2 pts, Celtic 0; knockouts don't count.
    assert table == [(1, "Aston Villa FC", 2, 4, 3), (2, "Lille OSC", 2, 4, 2),
                     (3, "Sport Lisboa e Benfica", 2, 2, 0), (4, "Celtic FC", 2, 0, -5)]
    assert standings["filters"] == {"season": "2026"}


def test_league_table_tiebreaks_goals_scored_then_away_goals():
    def m(h, a, hg, ag):
        return {"stage": "LEAGUE_STAGE", "status": "FINISHED",
                "homeTeam": {"id": h, "name": str(h)}, "awayTeam": {"id": a, "name": str(a)},
                "score": {"fullTime": {"home": hg, "away": ag}}}
    rows = fp.compute_league_table([
        m(1, 9, 3, 2),   # team 1: W, GD +1, GF 3 (home)
        m(9, 2, 2, 3),   # team 2: W, GD +1, GF 3 (away) -> ahead on away goals
        m(3, 9, 2, 1),   # team 3: W, GD +1, GF 2
    ])
    assert [r["team"]["id"] for r in rows][:3] == [2, 1, 3]


def test_tournament_groups_feed_the_existing_cache_builder(tmp_path):
    p = provider(tmp_path)
    standings, matches = run(FakeTsdb(), lambda: p.get_tournament_groups("Europa League", SMALL, 2026))
    data = sync_tournaments.build_tournament_data("uel", standings, matches, {})
    assert data["season"] == 2026
    assert len(data["standings"][0]["table"]) == 4
    assert sorted((m["stage"], m["matchday"]) for m in data["matches"]) == [
        ("FINAL", None), ("LAST_16", 1), ("LAST_16", 2), ("SEMI_FINALS", 1), ("SEMI_FINALS", 2)]
    assert {g["group"] for g in data["groupMatches"]} == {"LEAGUE_PHASE"}
    assert len(data["groupMatches"]) == 5


def test_no_data_raises_so_the_jobs_keep_their_files(tmp_path):
    p = provider(tmp_path)
    with pytest.raises(RuntimeError, match="no TheSportsDB data"):
        run(FakeTsdb(rounds={}), lambda: p.get_standings("Europa League", SMALL, 2026))
    assert run(FakeTsdb(rounds={}), lambda: provider(tmp_path).competition_fixtures(
        "Europa League", SMALL, 2026)) == {}


# ── Requests: season string, spacing, key, retries ────────────────────────────


def test_one_request_per_round_spaced_out(tmp_path, _no_sleep_no_network):
    fake = FakeTsdb()
    p = provider(tmp_path)
    run(fake, lambda: p.get_standings("Europa League", SMALL, 2026))
    codes = [1, 2, 3, 32, 16, 125, 150, 200]
    assert [c[1] for c in fake.calls] == codes
    assert {c[2] for c in fake.calls} == {"2026-2027"}
    assert all("/123/eventsround.php?id=4481&" in c[0] for c in fake.calls)
    # A pause between requests, none before the first.
    assert _no_sleep_no_network == [fp.TSDB_SLEEP_SECONDS] * (len(codes) - 1)
    # The same instance serves standings, tournament and fixtures from memory.
    run(fake, lambda: (p.get_tournament_groups("Europa League", SMALL, 2026),
                       p.competition_fixtures("Europa League", SMALL, 2026)))
    assert len(fake.calls) == len(codes)


def test_rate_limit_is_retried_once_after_the_back_off(tmp_path, _no_sleep_no_network):
    fake = FakeTsdb(fail={1: [429]})
    run(fake, lambda: provider(tmp_path).season_matches("Europa League", SMALL, 2026))
    assert [c[1] for c in fake.calls][:2] == [1, 1]
    assert fp.TSDB_RATE_LIMIT_BACKOFF_SECONDS in _no_sleep_no_network


def test_failed_round_keeps_its_cached_events_and_never_logs_the_key(tmp_path, caplog, capsys):
    run(FakeTsdb(), lambda: provider(tmp_path).season_matches("Europa League", SMALL, 2026))
    later = NOW + timedelta(days=2)   # round 3 (10 Dec) still future: due daily
    fake = FakeTsdb(fail={3: [500], 200: [429, 429]})
    with caplog.at_level(logging.WARNING):
        matches = run(fake, lambda: provider(tmp_path, now=later, key="s3cretKey")
                      .season_matches("Europa League", SMALL, 2026))
    assert "30" in {m["tsdbEventId"] for m in matches}      # round 3 kept from the cache
    assert "200" in {m["tsdbEventId"] for m in matches}
    out = caplog.text + capsys.readouterr().err
    assert "HTTP 500" in out and "s3cretKey" not in out


def test_a_failed_retry_stops_requests_for_the_run(tmp_path, caplog):
    fake = FakeTsdb(fail={2: [429, 429]})
    with caplog.at_level(logging.INFO):
        matches = run(fake, lambda: provider(tmp_path).season_matches("Europa League", SMALL, 2026))
    assert [c[1] for c in fake.calls] == [1, 2, 2]          # nothing after round 2
    assert {m["tsdbEventId"] for m in matches} == {"10", "11"}
    assert "rate-limited - 6 due round(s)" in caplog.text
    # The next run picks up where this one stopped.
    fake = FakeTsdb()
    run(fake, lambda: provider(tmp_path, now=NOW + timedelta(minutes=5))
        .season_matches("Europa League", SMALL, 2026))
    assert [c[1] for c in fake.calls] == [2, 3, 32, 16, 125, 150, 200]


def test_requests_per_run_are_capped(tmp_path, caplog):
    fake = FakeTsdb()
    with caplog.at_level(logging.INFO):
        run(fake, lambda: provider(tmp_path).season_matches("Europa League", UEL, 2026))
    assert len(fake.calls) == fp.TSDB_MAX_REQUESTS_PER_RUN
    assert "8-request cap reached - 5 due round(s)" in caplog.text


def test_api_key_comes_from_the_environment(monkeypatch):
    assert fp.TheSportsDBProvider.from_config({})._key == "123"
    monkeypatch.setenv("THESPORTSDB_API_KEY", "premium")
    assert fp.TheSportsDBProvider.from_config({"fd_key": "x"})._key == "premium"


# ── Round cache ───────────────────────────────────────────────────────────────


def _entry(fetched, events):
    return {"last_updated": fetched.strftime("%Y-%m-%dT%H:%M:%SZ"), "events": events}


def test_round_due_policy():
    due = fp.tsdb_round_due
    old_ft = [ev(1, 1, "2026-09-24T19:00:00", LIL, CEL, "FT", 2, 0)]
    soon = [ev(2, 6, "2026-11-28T20:00:00", LIL, CEL, "NS")]
    future = [ev(3, 7, "2027-01-21T20:00:00", LIL, CEL, "NS")]
    assert due(None, NOW)
    assert due({"last_updated": "garbage", "events": []}, NOW)
    assert not due(_entry(NOW - timedelta(days=60), old_ft), NOW)        # settled
    assert not due(_entry(NOW - timedelta(minutes=10), soon), NOW)       # active, fresh
    assert due(_entry(NOW - timedelta(minutes=31), soon), NOW)           # active, stale
    assert not due(_entry(NOW - timedelta(hours=2), future), NOW)        # future, fresh
    assert due(_entry(NOW - timedelta(hours=25), future), NOW)           # future, daily
    assert not due(_entry(NOW - timedelta(hours=2), []), NOW)            # not drawn yet
    assert due(_entry(NOW - timedelta(hours=25), []), NOW)
    # A postponed game keeps an old round open (daily) until it is played.
    pst = old_ft + [ev(4, 1, "2026-09-24T19:00:00", AVL, BEN, "NS", strPostponed="yes")]
    assert due(_entry(NOW - timedelta(hours=25), pst), NOW)
    # A finished round stays active for TSDB_SETTLED_AFTER (late corrections).
    recent = [ev(5, 5, "2026-11-27T20:00:00", LIL, CEL, "FT", 1, 0)]
    assert due(_entry(NOW - timedelta(minutes=31), recent), NOW)


def test_cache_spares_requests_and_drops_qualifiers_and_noise(tmp_path):
    run(FakeTsdb(), lambda: provider(tmp_path).season_matches("Europa League", SMALL, 2026))
    cache = json.loads((tmp_path / "rounds.json").read_text(encoding="utf-8"))
    r1 = cache["4481"]["2026"]["1"]["events"]
    assert [e["idEvent"] for e in r1] == ["10", "11"]       # July qualifier not cached
    assert "strPoster" not in r1[0] and "intHomeScoreExtra" not in r1[0]
    # A new run (new process) a minute later requests nothing...
    fake = FakeTsdb()
    run(fake, lambda: provider(tmp_path, now=NOW + timedelta(minutes=1))
        .season_matches("Europa League", SMALL, 2026))
    assert fake.calls == []
    # ...and a day later only the rounds that can still change: not the
    # September/October rounds (settled), yes the future ones.
    fake = FakeTsdb()
    run(fake, lambda: provider(tmp_path, now=NOW + timedelta(days=1, minutes=1))
        .season_matches("Europa League", SMALL, 2026))
    assert [c[1] for c in fake.calls] == [3, 32, 16, 125, 150, 200]


def test_cache_timestamps_are_commit_gate_volatile():
    import commit_gate
    assert "last_updated" in commit_gate.VOLATILE_KEYS


# ── Through the job scripts (as when the Europa League is switched on) ────────


def test_jobs_write_uel_files_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "TSDB_ROUNDS_CACHE", tmp_path / "rounds.json")
    monkeypatch.setattr(fp, "load_known_fd_teams", lambda *a: FD_TEAMS)
    only_uel = {"Europa League": SMALL}
    fake = FakeTsdb()
    with patch.dict(hc.COMPETITION_CONFIG, only_uel, clear=True), \
         patch.object(fp.TheSportsDBProvider, "__init__", _fixed_now_init()), \
         patch("urllib.request.urlopen", fake), \
         patch.object(sync_standings, "current_season", return_value=2026), \
         patch.object(sync_tournaments, "season_for_competition", return_value=2026):
        sync_standings.main("fd-key", out_dir=str(tmp_path))
        sync_tournaments.main("fd-key", out_dir=str(tmp_path))
    standings = json.loads((tmp_path / "standings" / "uel" / "2026.json").read_text(encoding="utf-8"))
    assert [r["team"]["name"] for r in standings["standings"]][:2] == ["Aston Villa FC", "Lille OSC"]
    uel = json.loads((tmp_path / "tournament-groups" / "uel.json").read_text(encoding="utf-8"))
    assert uel["slug"] == "uel" and len(uel["groupMatches"]) == 5 and len(uel["matches"]) == 5
    # Two processes, one set of requests: the second job read the cache.
    assert len(fake.calls) == 8


def _fixed_now_init():
    real = fp.TheSportsDBProvider.__init__

    def init(self, *a, **k):
        k.setdefault("now", lambda: NOW)
        real(self, *a, **k)
    return init
