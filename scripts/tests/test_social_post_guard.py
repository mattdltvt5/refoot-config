"""Social posts in curated playlists + the pre-kickoff guard.

DAZN Football's UCL highlights playlist also carries social shorts. One,
"Who is the best player to have played for both PSG & Roma?! 👀 | #UCL",
was added at 17:11 UTC on match day (kick-off 20:00), named both teams, and,
because curated playlists skip the title allowlist, was cached as PSG v Roma's
highlights; the real package added at 22:39 was never looked at again.
"""
from unittest.mock import MagicMock, patch

import pytest

import highlights_common as H
from highlights_common import (
    apply_existing_record_guards,
    looks_like_social_post,
    published_before_kickoff,
    search_playlist,
    title_gate_guard,
)

PSG_ROMA_SHORT = "Who is the best player to have played for both PSG & Roma?! 👀 | #UCL"
PSG_ROMA_HL = "PSG vs Roma | UEFA Champions League Highlights | Matchday 5 | 2026-2027"


@pytest.mark.parametrize("title", [
    PSG_ROMA_SHORT,
    "Who is the best player to have played for both Borussia Dortmund & Arsenal?! 👀 | #UCL",
    "😆 Villarreal players were excited to hype team captain Logan Costa after the win over Slavia. #UCL",
    "👑 Antoine, Prince du HAC lors de #HACPSG",
])
def test_social_posts_detected(title):
    assert looks_like_social_post(title)


@pytest.mark.parametrize("title", [
    PSG_ROMA_HL,  # no emoji
    # Club playlist titles with emoji that write the fixture out (kept).
    "VALENCIA 0 vs 5 FC BARCELONA | LALIGA 2026/27 MD04 🔵🔴",
    "GOALS GALORE 🔥 | Crystal Palace 3-3 AFC Bournemouth",
    "OM 2-0 Toulouse : 3 points de plus ✅",
    "⚽ HIGHLIGHTS | PSG - Roma",  # names the package
    "UNBELIEVABLE! 10 Goals-Spectacle! | BORUSSIA M'GLADBACH - EINTRACHT FRANKFURT | Bundesliga 25/26",
])
def test_highlights_titles_not_social_posts(title):
    assert not looks_like_social_post(title)


def test_published_before_kickoff():
    ko = "2026-11-25T20:00:00Z"
    assert published_before_kickoff("2026-11-25T17:11:20Z", ko)
    assert not published_before_kickoff("2026-11-25T22:39:56Z", ko)
    # Date-only or missing values never trigger the guard.
    assert not published_before_kickoff("2026-11-25", ko)
    assert not published_before_kickoff("2026-11-25T17:11:20Z", "")
    assert not published_before_kickoff("2026-11-25T17:11:20Z", "2026-11-25")


# ── search_playlist (curated playlist = allowlist bypass, as for tier 4) ─────

class _Quota:
    def increment(self, cap):
        pass


_PSG_ROMA = {
    "match_id": 575408, "date": "2026-11-25", "kickoff": "2026-11-25T20:00:00Z",
    "home_team": "Paris Saint-Germain FC", "home_short": "PSG", "home_tla": "PSG",
    "away_team": "AS Roma", "away_short": "Roma", "away_tla": "ROM",
}


def _item(video_id, title, published):
    return {"snippet": {"title": title, "publishedAt": published,
                        "resourceId": {"videoId": video_id}}}


def _search(items, fixture=_PSG_ROMA):
    resp = MagicMock(status_code=200, ok=True)
    resp.json.return_value = {"items": items, "nextPageToken": None}
    sink: list = []
    with patch("highlights_common.requests.get", return_value=resp):
        got = search_playlist("PLx", "key", fixture, "Champions League", _Quota(), 10**6,
                              requires_both_teams=True, bypass_highlight_allowlist=True,
                              debug_sink=sink)
    return [v["video_id"] for v in got], {r["video_id"]: r["reason"] for r in sink}


def test_pre_kickoff_short_refused_real_highlights_taken():
    got, why = _search([
        _item("tELx6y3nPLc", PSG_ROMA_SHORT, "2026-11-25T17:11:20Z"),
        _item("REAL", PSG_ROMA_HL, "2026-11-25T22:39:56Z"),
    ])
    assert got == ["REAL"]
    assert why["tELx6y3nPLc"].startswith("published-before-kickoff")


def test_social_post_refused_even_after_kickoff():
    got, why = _search([_item("tELx6y3nPLc", PSG_ROMA_SHORT, "2026-11-25T23:00:00Z")])
    assert got == []
    assert why["tELx6y3nPLc"] == "title-filter:social-post"


def test_without_kickoff_the_date_window_still_applies():
    fixture = {k: v for k, v in _PSG_ROMA.items() if k != "kickoff"}
    got, _ = _search([_item("REAL", PSG_ROMA_HL, "2026-11-25T22:39:56Z")], fixture)
    assert got == ["REAL"]


# ── Cached entry re-check ─────────────────────────────────────────────────────

def test_cached_social_post_removed_and_match_reopened():
    assert title_gate_guard({"title": PSG_ROMA_SHORT, "tier_used": 4}, _PSG_ROMA) == \
        "title_gate_social_post"
    assert title_gate_guard({"title": PSG_ROMA_HL, "tier_used": 4}, _PSG_ROMA) is None
    existing = {"matches": [{"match_id": 575408, "videos": [
        {"video_id": "tELx6y3nPLc", "title": PSG_ROMA_SHORT, "tier_used": 4},
    ]}]}
    removed = apply_existing_record_guards(existing, [_PSG_ROMA])
    assert [r["video_id"] for r in removed] == ["tELx6y3nPLc"]
    assert existing["matches"][0]["videos"] == []
