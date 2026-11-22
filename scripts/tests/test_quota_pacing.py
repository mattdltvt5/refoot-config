"""Quota pacing in search_playlist(): fewer playlistItems.list pages per run.

1. Channel uploads (UU…, newest first) stop paginating once a page reaches
   videos older than the fixture date minus UPLOADS_EARLY_STOP_MARGIN.
   Curated playlists (PL…, any order) keep paging as before.
2. A per-run ``page_cache`` serves a page another fixture already fetched this
   run with no request and no quota.

Neither changes which videos are accepted. requests.get is patched; no API calls.
"""
from unittest.mock import MagicMock, patch

import highlights_common as hc
from highlights_common import MAX_YT_PAGES, search_playlist

_FIXTURE = {
    "date": "2026-11-21",
    "home_team": "Arsenal FC", "home_short": "Arsenal", "home_tla": "ARS",
    "away_team": "Chelsea FC", "away_short": "Chelsea", "away_tla": "CHE",
}
_OTHER_FIXTURE = {
    "date": "2026-11-21",
    "home_team": "Liverpool FC", "home_short": "Liverpool", "home_tla": "LIV",
    "away_team": "Everton FC", "away_short": "Everton", "away_tla": "EVE",
}
_HIGHLIGHT = "Arsenal v Chelsea | Highlights"


class _Quota:
    def __init__(self):
        self.units = 0

    def increment(self, cap):
        self.units += 1


def _item(vid, title, date):
    return {"snippet": {"title": title, "publishedAt": f"{date}T12:00:00Z",
                        "resourceId": {"videoId": vid}}}


def _page(items, token=""):
    return {"items": items, "nextPageToken": token or None}


def _ok(body):
    r = MagicMock(status_code=200, ok=True)
    r.json.return_value = body
    return r


class _Feed:
    """Serves pages by pageToken ('' -> p0, 't1' -> p1, ...), counting requests."""

    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def __call__(self, url, params=None, timeout=None):
        token = params.get("pageToken", "")
        idx = 0 if not token else int(token[1:])
        self.requests.append((params["playlistId"], token))
        return _ok(self.pages[idx])


def _endless(dates_per_page, title="Training session", start=0):
    """Pages whose items have the given dates; each links to the next."""
    pages = []
    for i, dates in enumerate(dates_per_page):
        items = [_item(f"v{start}{i}{j}", title, d) for j, d in enumerate(dates)]
        pages.append(_page(items, f"t{i + 1}"))
    return pages


def _search(playlist_id, feed, fixture=_FIXTURE, page_cache=None, quota=None):
    quota = quota or _Quota()
    with patch("highlights_common.requests.get", side_effect=feed):
        got = search_playlist(playlist_id, "k", fixture, "Premier League", quota,
                              10**9, requires_both_teams=True, page_cache=page_cache)
    return got, quota


# ── 1. uploads early stop ─────────────────────────────────────────────────────

def test_uploads_feed_stops_once_page_reaches_before_fixture():
    # Page 0 spans the match day back to 4 days before; no highlight yet.
    feed = _Feed(_endless([["2026-11-21", "2026-11-19", "2026-11-17"]] * MAX_YT_PAGES))
    got, quota = _search("UUclub", feed)
    assert got == []
    assert quota.units == 1 and len(feed.requests) == 1   # was MAX_YT_PAGES (10)


def test_uploads_feed_pages_through_newer_items_to_reach_fixture():
    # Backfill of an older fixture: newer uploads first, then the match.
    old = dict(_FIXTURE, date="2026-11-01")
    pages = _endless([["2026-11-21", "2026-11-15"], ["2026-11-10", "2026-11-05"]])
    pages.append(_page([_item("hit", _HIGHLIGHT, "2026-11-02"),
                        _item("x", "Training", "2026-10-20")], "t4"))
    feed = _Feed(pages)
    got, quota = _search("UUclub", feed, fixture=old)
    assert [v["video_id"] for v in got] == ["hit"]
    assert quota.units == 3


def test_margin_keeps_paging_when_page_ends_at_the_day_before():
    # Oldest item is the day before the match: within the 1-day margin.
    pages = _endless([["2026-11-21", "2026-11-20"], ["2026-11-20", "2026-11-18"]])
    feed = _Feed(pages)
    _, quota = _search("UUclub", feed)
    assert quota.units == 2


def test_curated_playlist_keeps_paging_whatever_the_dates():
    # PL… playlists can be oldest-first or hand-ordered: no early stop.
    feed = _Feed(_endless([["2026-10-01"]] * MAX_YT_PAGES))
    _, quota = _search("PLcurated", feed)
    assert quota.units == MAX_YT_PAGES


def test_early_stop_does_not_hide_a_match_on_the_same_page():
    feed = _Feed([_page([_item("hit", _HIGHLIGHT, "2026-11-21"),
                         _item("old", "Training", "2026-11-01")], "t1")])
    got, _ = _search("UUclub", feed)
    assert [v["video_id"] for v in got] == ["hit"]


def test_page_without_dated_items_does_not_stop():
    blank = {"snippet": {"title": "x", "publishedAt": "", "resourceId": {"videoId": "b"}}}
    pages = [_page([blank], "t1"), _page([_item("hit", _HIGHLIGHT, "2026-11-21")])]
    got, quota = _search("UUclub", _Feed(pages))
    assert [v["video_id"] for v in got] == ["hit"] and quota.units == 2


# ── 2. per-run page cache ─────────────────────────────────────────────────────

def test_second_fixture_reuses_pages_without_quota():
    pages = [_page([_item("a", _HIGHLIGHT, "2026-11-21"),
                    _item("l", "Liverpool v Everton | Highlights", "2026-11-21")])]
    feed = _Feed(pages)
    cache: dict = {}
    got1, q1 = _search("PLcomp", feed, page_cache=cache)
    got2, q2 = _search("PLcomp", feed, fixture=_OTHER_FIXTURE, page_cache=cache)
    assert [v["video_id"] for v in got1] == ["a"]
    assert [v["video_id"] for v in got2] == ["l"]
    assert (q1.units, q2.units, len(feed.requests)) == (1, 0, 1)


def test_cache_extends_when_a_later_fixture_needs_more_pages():
    pages = _endless([["2026-11-21"], ["2026-11-21"], ["2026-11-21"]])
    pages[2] = _page([_item("l", "Liverpool v Everton | Highlights", "2026-11-21")])
    pages[1] = _page([_item("a", _HIGHLIGHT, "2026-11-21")], "t2")
    feed = _Feed(pages)
    cache: dict = {}
    _, q1 = _search("PLcomp", feed, page_cache=cache)            # pages 0-1
    got, q2 = _search("PLcomp", feed, fixture=_OTHER_FIXTURE, page_cache=cache)
    assert [v["video_id"] for v in got] == ["l"]
    assert (q1.units, q2.units) == (2, 1)                       # only page 2 is new
    assert feed.requests == [("PLcomp", ""), ("PLcomp", "t1"), ("PLcomp", "t2")]


def test_cache_is_keyed_by_playlist():
    feed = _Feed([_page([])])
    cache: dict = {}
    _search("PLa", feed, page_cache=cache)
    _search("PLb", feed, page_cache=cache)
    assert len(feed.requests) == 2


def test_failed_pages_are_not_cached():
    cache: dict = {}
    bad = MagicMock(status_code=500, ok=False)
    with patch("highlights_common.requests.get", return_value=bad):
        search_playlist("PLx", "k", _FIXTURE, "Premier League", _Quota(), 10**9,
                        page_cache=cache)
    assert cache == {}


def test_no_cache_means_old_behaviour():
    feed = _Feed([_page([])])
    _search("PLx", feed)
    _search("PLx", feed)
    assert len(feed.requests) == 2


def test_resolver_threads_page_cache_to_every_tier(monkeypatch):
    seen = []

    def fake_search(playlist_id, *a, page_cache=None, **k):
        seen.append(page_cache)
        return []

    monkeypatch.setattr(hc, "search_playlist", fake_search)
    monkeypatch.setattr(hc, "find_gameweek_playlist", lambda *a, **k: None)
    config = {
        "team_playlists": {"Premier League": {"Arsenal FC": "PLh", "Chelsea FC": "PLa"}},
        "competition_channels": {"Premier League": "UCcomp"},
        "competition_playlists": {"Premier League": {"B": ["PLb"]}},
        "team_channels": {"Arsenal FC": "UCh", "Chelsea FC": "UCa"},
    }
    cache: dict = {}
    hc.resolve_videos_for_fixture(_FIXTURE, "Premier League", config, "k", _Quota(),
                                  10**9, page_cache=cache)
    assert len(seen) == 6 and all(c is cache for c in seen)


def test_run_scripts_share_one_cache_per_run():
    from pathlib import Path
    root = Path(hc.__file__).parent
    for name in ("fetch_highlights.py", "backfill_highlights.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert src.count("page_cache=page_cache") == src.count(
            "gw_playlist_cache=gw_playlist_cache"), name
