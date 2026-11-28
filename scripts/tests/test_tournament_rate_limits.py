"""Tournament refresh vs football-data's 10 req/min limit.

sync_tournaments runs right after fetch_highlights' burst of fixtures requests.
Before this fix the Euro was answered 404 on every run and the World Cup 429
about every other run, spending the budget the Champions League needs, though
both caches were complete editions that could no longer change. Now:
  - a finished summer-tournament edition is skipped until its next edition;
  - a 429 is retried once after FD_RATE_LIMIT_BACKOFF_SECONDS.

All HTTP is mocked.
"""

import json
import os
import sys
import urllib.error
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fixture_providers as fp  # noqa: E402
import sync_tournaments as st  # noqa: E402

NOW = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)


def _ok(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda self: self
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http(code):
    return urllib.error.HTTPError("u", code, "err", {}, None)


@pytest.fixture
def sleeps(monkeypatch):
    # fp.time and st.time are the same module: one patch records every pause.
    calls = []
    monkeypatch.setattr(fp.time, "sleep", calls.append)
    return calls


# ── One retry after a 429 ─────────────────────────────────────────────────────


def test_429_then_success_retries_once_after_the_backoff(sleeps):
    with patch("urllib.request.urlopen", side_effect=[_http(429), _ok({"standings": []})]) as m:
        assert fp.fd_fetch_standings(2001, "k", base_url="https://mock.api") == {"standings": []}
    assert m.call_count == 2
    assert sleeps == [fp.FD_RATE_LIMIT_BACKOFF_SECONDS]


def test_second_429_raises_so_the_cache_is_kept(sleeps):
    with patch("urllib.request.urlopen", side_effect=[_http(429), _http(429)]) as m:
        with pytest.raises(urllib.error.HTTPError) as e:
            fp.fd_fetch_matches(2001, "k", base_url="https://mock.api")
    assert e.value.code == 429 and m.call_count == 2


def test_other_errors_are_not_retried(sleeps):
    with patch("urllib.request.urlopen", side_effect=[_http(404)]) as m:
        with pytest.raises(urllib.error.HTTPError):
            fp.fd_fetch_standings(2018, "k", base_url="https://mock.api")
    assert m.call_count == 1 and sleeps == []


# ── Finished editions are left alone ──────────────────────────────────────────


def _cache(*kickoffs, status="FINISHED"):
    return {"groupMatches": [{"status": "FINISHED", "utcDate": kickoffs[0]}],
            "matches": [{"status": status, "utcDate": k} for k in kickoffs[1:]]}


def test_finished_current_edition_is_complete():
    euro = _cache("2024-06-14T19:00:00Z", "2024-07-14T19:00:00Z")
    assert st.edition_complete("Euro Cup", euro, NOW) == 2024
    wc = _cache("2026-06-11T19:00:00Z", "2026-07-19T19:00:00Z")
    assert st.edition_complete("World Cup", wc, NOW) == 2026


def test_unfinished_or_just_finished_edition_is_refreshed():
    assert st.edition_complete("World Cup", _cache(
        "2026-06-11T19:00:00Z", "2026-07-19T19:00:00Z", status="TIMED"), NOW) is None
    recent = datetime(2026, 8, 1, tzinfo=timezone.utc)  # final 19 Jul: < 30 days
    assert st.edition_complete("World Cup", _cache(
        "2026-06-11T19:00:00Z", "2026-07-19T19:00:00Z"), recent) is None


def test_refresh_resumes_when_the_next_edition_comes_round():
    euro = _cache("2024-06-14T19:00:00Z", "2024-07-14T19:00:00Z")
    assert st.edition_complete("Euro Cup", euro, datetime(2028, 1, 2, tzinfo=timezone.utc)) is None


def test_season_competitions_and_empty_caches_are_never_skipped():
    ucl = _cache("2025-09-16T19:00:00Z", "2026-05-30T19:00:00Z")
    assert st.edition_complete("Champions League", ucl, NOW) is None
    assert st.edition_complete("Euro Cup", {}, NOW) is None
    assert st.edition_complete("Euro Cup", {"groupMatches": [], "matches": []}, NOW) is None


def test_main_requests_only_the_champions_league_when_the_tournaments_are_done(
        tmp_path, capsys, monkeypatch):
    tg = tmp_path / "tournament-groups"
    tg.mkdir()
    (tg / "euro-cup.json").write_text(json.dumps(
        _cache("2024-06-14T19:00:00Z", "2024-07-14T19:00:00Z")), encoding="utf-8")
    (tg / "world-cup.json").write_text(json.dumps(
        _cache("2026-06-11T19:00:00Z", "2026-07-19T19:00:00Z")), encoding="utf-8")
    monkeypatch.setattr(st, "graft_video_ids", lambda *_a, **_k: None)
    monkeypatch.setattr(st.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fp.time, "sleep", lambda *_: None)
    real_complete = st.edition_complete
    monkeypatch.setattr(st, "edition_complete", lambda c, e, now=None: real_complete(c, e, NOW))
    with patch.object(fp, "fd_fetch_standings", return_value={"standings": []}) as s, \
         patch.object(fp, "fd_fetch_matches", return_value={"matches": []}) as m:
        st.main("k", out_dir=str(tmp_path))
    assert [c.args[0] for c in s.call_args_list] == [2001]
    assert [c.args[0] for c in m.call_args_list] == [2001]
    out = capsys.readouterr().out
    assert "Euro Cup: 2024 edition complete" in out
    assert "World Cup: 2026 edition complete" in out
    assert (tg / "ucl.json").exists()
    # The finished editions' files are untouched.
    assert json.loads((tg / "euro-cup.json").read_text(encoding="utf-8"))["matches"][0]["utcDate"] \
        == "2024-07-14T19:00:00Z"
