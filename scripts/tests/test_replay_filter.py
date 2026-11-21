"""
Tests for the full-match / live-replay filter (no network):

  - COMP_CHANNEL_REQUIRED_TERMS: Bundesliga tier-2 titles must carry a highlights
    marker; the gate is passed ONLY to the competition-channel source.
  - TITLE_BLOCKLIST_PATTERNS: "LIVE 🔴" / leading "LIVE" replays are blocked on
    every source, without catching "live" elsewhere in a title.
  - MAX_VIDEO_DURATION_SECONDS (+ per-competition override).
  - Region semantics: absent = everywhere, allowed: [] = nowhere.
  - title_gate_guard: cached entries that fail the current gates are dropped so
    the match is re-searched; valid entries are untouched.

Titles below are real cached titles from the 2026-11-21 audit.
"""

from unittest.mock import MagicMock, patch

import pytest

import highlights_common as H
from highlights_common import (
    COMP_CHANNEL_REQUIRED_TERMS,
    COMP_CHANNEL_TITLE_TERMS,
    apply_existing_record_guards,
    is_highlight_title,
    prefer_unrestricted,
    region_is_playable_in,
    region_is_playable_nowhere,
    region_is_restricted,
    resolve_videos_for_fixture,
    search_playlist,
    title_gate_guard,
)

BL = "Bundesliga"

HIGHLIGHTS_TITLES = [
    "NMECHA-BRACE! | BORUSSIA DORTMUND - SV ELVERSBERG | Highlights | Matchday 9 – Bundesliga 2026/27",
    "Comeback After Early Shock!! | RB LEIPZIG - FC AUGSBURG | Highlights | Matchday 9 – Bundesliga 26/27",
    "WHAT A DEBUT! | SV ELVERSBERG - BAYER 04 LEVERKUSEN | Highlights | Matchday 1 – Bundesliga 2026/27",
    # Short "| HL |" marker used by the same channel in 2025/26
    "90+1! Undav On Fire Saves VFB! | BORUSSIA DORTMUND - VFB STUTTGART | HL | MD 11 – Bundesliga",
]
REPLAY_TITLES = [
    "SV Elversberg vs. Sport-Club Freiburg | Matchday 10 - Bundesliga 2026/27",
    "FC Bayern München vs. VfB Stuttgart | Matchday 1 - Bundesliga 2026/27",
]
LIVE_TITLES = [
    "LIVE 🔴 Hamburger SV vs. Borussia Mönchengladbach | Matchday 9 - Bundesliga 2026/27",
    "LIVE 🔴 FC Bayern München vs. 1. FC Union Berlin | Matchday 4 - Bundesliga 2026/27",
]


@pytest.fixture(autouse=True)
def _clear_rejections():
    H.GUARD_REJECTIONS.clear()
    yield
    H.GUARD_REJECTIONS.clear()


def _bl_tier2_gate(title, require_allowlist=True):
    """The exact title gate search_playlist applies for a Bundesliga tier-2 source."""
    return is_highlight_title(
        title, COMP_CHANNEL_TITLE_TERMS[BL],
        require_allowlist=require_allowlist,
        required_terms=COMP_CHANNEL_REQUIRED_TERMS[BL],
    )


# ── Required-term gate (tier 2 only) ──────────────────────────────────────────

@pytest.mark.parametrize("title", HIGHLIGHTS_TITLES)
def test_bundesliga_tier2_accepts_highlights_titles(title):
    assert _bl_tier2_gate(title) is True


@pytest.mark.parametrize("title", REPLAY_TITLES + LIVE_TITLES)
def test_bundesliga_tier2_rejects_replay_titles(title):
    assert _bl_tier2_gate(title) is False


@pytest.mark.parametrize("title", REPLAY_TITLES)
def test_required_terms_hold_even_when_playlist_bypasses_allowlist(title):
    # A GW playlist whose own title says "highlights" skips the allowlist, but
    # the required-term gate still applies.
    assert _bl_tier2_gate(title, require_allowlist=False) is False


def test_required_term_in_hashtag_only_does_not_count():
    t = "SV Elversberg vs. Sport-Club Freiburg | Matchday 10 #BundesligaHighlights"
    assert _bl_tier2_gate(t) is False


@pytest.mark.parametrize("title", REPLAY_TITLES)
def test_club_sources_unaffected_by_required_terms(title):
    # Team playlists / broadcaster playlists bypass the allowlist and get no
    # required terms: a replay-format title behaves exactly as before.
    assert is_highlight_title(title, require_allowlist=False) is True
    # Club channel uploads (allowlist on, no comp extras, no required terms): as before.
    assert is_highlight_title(title) is False


def _bl_fixture():
    return {
        "match_id": 565860, "date": "2026-11-21", "matchday": 10, "stage": "REGULAR_SEASON",
        "home_team": "SV 07 Elversberg", "home_short": "Elversberg", "home_tla": "ELV",
        "away_team": "SC Freiburg", "away_short": "Freiburg", "away_tla": "SCF",
        "status": "FINISHED", "score": {"fullTime": {"home": 1, "away": 1}},
    }


def _config():
    return {
        "team_playlists":        {BL: {"SV 07 Elversberg": "PLhome", "SC Freiburg": "PLaway"}},
        "competition_channels":  {BL: "UCblchannel"},
        "competition_playlists": {BL: {"Bundesliga TV": ["PLbroadcaster"]}},
        "team_channels":         {"SV 07 Elversberg": "UChomeclub", "SC Freiburg": "UCawayclub"},
    }


def test_required_terms_passed_only_to_tier2_sources():
    calls = {}

    def fake_search(playlist_id, *a, **kw):
        calls[playlist_id] = tuple(kw.get("required_terms", ()))
        return []

    with patch.object(H, "search_playlist", side_effect=fake_search), \
         patch.object(H, "find_gameweek_playlist", return_value=("PLgw10", "Matchday 10")):
        assert resolve_videos_for_fixture(_bl_fixture(), BL, _config(), "key", None, 10**6) == []

    required = tuple(COMP_CHANNEL_REQUIRED_TERMS[BL])
    assert calls["PLgw10"] == required            # tier 2a GW playlist
    assert calls["UUblchannel"] == required       # tier 2b channel uploads
    for other in ("PLhome", "PLaway", "PLbroadcaster", "UUhomeclub", "UUawayclub"):
        assert calls[other] == (), other


class _Quota:
    def increment(self, cap):
        pass


def _item(video_id, title, date="2026-11-21"):
    return {"snippet": {"title": title, "publishedAt": f"{date}T20:00:00Z",
                        "resourceId": {"videoId": video_id}}}


def _search(items, **kw):
    resp = MagicMock(status_code=200, ok=True)
    resp.json.return_value = {"items": items, "nextPageToken": None}
    sink = []
    with patch("highlights_common.requests.get", return_value=resp):
        got = search_playlist("PLx", "key", _bl_fixture(), BL, _Quota(), 10**6,
                              requires_both_teams=True, debug_sink=sink, **kw)
    return got, sink


def test_search_playlist_tier2_picks_highlights_over_replay_and_reports_reason():
    got, sink = _search(
        [_item("REPLAY", REPLAY_TITLES[0]),
         _item("HL", "LATE DRAMA! | SV ELVERSBERG - SC FREIBURG | Highlights | Matchday 10 – Bundesliga 2026/27")],
        extra_allowlist=COMP_CHANNEL_TITLE_TERMS[BL],
        required_terms=COMP_CHANNEL_REQUIRED_TERMS[BL],
    )
    assert [v["video_id"] for v in got] == ["HL"]
    reasons = {r["video_id"]: r["reason"] for r in sink}
    assert reasons["REPLAY"] == "title-filter:missing-required-term"


def test_search_playlist_without_required_terms_still_accepts_replay_from_curated_source():
    got, _ = _search([_item("REPLAY", REPLAY_TITLES[0])], bypass_highlight_allowlist=True)
    assert [v["video_id"] for v in got] == ["REPLAY"]


# ── Live-replay blocklist patterns ────────────────────────────────────────────

@pytest.mark.parametrize("title", LIVE_TITLES + [
    "🔴 LIVE | Bayern vs Köln | Highlights",
    "LIVE: FC Bayern München vs. 1. FC Köln Highlights",
    "live 🔴️ Bayern vs Köln highlights",     # with emoji variation selector
])
def test_live_replay_markers_blocked_on_every_source(title):
    assert is_highlight_title(title) is False
    assert is_highlight_title(title, require_allowlist=False) is False


@pytest.mark.parametrize("title", [
    "Liverpool 2-1 Arsenal | Premier League Highlights",
    "LIVERPOOL vs ARSENAL | HIGHLIGHTS",
    "Lively derby! | Highlights | Matchday 9 – Bundesliga",
    "Highlights: Bayern deliver late winner",
    "Résumé : OM - PSG, les buts en live du Vélodrome",
    "Zusammenfassung: Bayern live-stark gegen Köln",
    "Highlights | Arsenal 3-1 Chelsea | Live reactions excluded",   # "live" mid-title
])
def test_live_rule_does_not_reject_legitimate_titles(title):
    # Pattern-only check: no TITLE_BLOCKLIST_PATTERNS entry fires.
    lower = title.lower()
    assert not any(p.search(lower) for p in H.TITLE_BLOCKLIST_PATTERNS), title


def test_allowlist_has_no_term_the_live_patterns_would_block():
    for term in H.TITLE_ALLOWLIST:
        assert not any(p.search(term) for p in H.TITLE_BLOCKLIST_PATTERNS), term


# ── Max duration ──────────────────────────────────────────────────────────────

def _details(duration):
    return {"HL": {"duration_seconds": duration, "is_portrait": False,
                   "is_embeddable": True, "is_region_restricted": False,
                   "is_playable_nowhere": False}}


def _resolve_with_duration(duration):
    hl = {"video_id": "HL", "title": HIGHLIGHTS_TITLES[0], "published_at": "2026-11-21"}

    def fake_search(playlist_id, *a, **kw):
        return [dict(hl)] if playlist_id == "PLgw10" else []

    with patch.object(H, "search_playlist", side_effect=fake_search), \
         patch.object(H, "find_gameweek_playlist", return_value=("PLgw10", "Matchday 10")), \
         patch.object(H, "fetch_video_details", return_value=_details(duration)):
        return resolve_videos_for_fixture(_bl_fixture(), BL, _config(), "key", None, 10**6)


def test_default_max_duration_is_1800():
    assert H.MAX_VIDEO_DURATION_SECONDS == 1800
    assert H.max_video_duration("Premier League") == 1800


def test_video_over_max_duration_rejected():
    assert _resolve_with_duration(1801) == []


def test_video_within_max_duration_accepted():
    got = _resolve_with_duration(600)
    assert [v["video_id"] for v in got] == ["HL"]


def test_per_competition_duration_override_respected(monkeypatch):
    monkeypatch.setitem(H.MAX_VIDEO_DURATION_OVERRIDES, BL, 3600)
    assert H.max_video_duration(BL) == 3600
    assert [v["video_id"] for v in _resolve_with_duration(2400)] == ["HL"]
    monkeypatch.setitem(H.MAX_VIDEO_DURATION_OVERRIDES, BL, 300)
    assert _resolve_with_duration(600) == []


# ── Region semantics ──────────────────────────────────────────────────────────

def test_missing_restriction_is_everywhere():
    assert region_is_playable_in(None, "US") and region_is_playable_in(None, "DE")
    assert region_is_restricted(None) is False
    assert region_is_playable_nowhere(None) is False


def test_empty_allowed_is_nowhere():
    r = {"allowed": []}
    assert not region_is_playable_in(r, "US") and not region_is_playable_in(r, "DE")
    assert region_is_restricted(r) is True
    assert region_is_playable_nowhere(r) is True


def test_allowed_us():
    r = {"allowed": ["US"]}
    assert region_is_playable_in(r, "US") is True
    assert region_is_playable_in(r, "de") is False
    assert region_is_restricted(r) is True and region_is_playable_nowhere(r) is False


def test_blocked_de():
    r = {"blocked": ["DE"]}
    assert region_is_playable_in(r, "US") is True
    assert region_is_playable_in(r, "DE") is False
    assert region_is_restricted(r) is True and region_is_playable_nowhere(r) is False


def test_empty_blocked_is_everywhere():
    assert region_is_restricted({"blocked": []}) is False
    assert region_is_playable_in({"blocked": []}, "DE") is True


def test_prefer_unrestricted_drops_nowhere_clip_even_when_alone():
    videos = [{"video_id": "nowhere"}]
    details = {"nowhere": {"is_region_restricted": True, "is_playable_nowhere": True}}
    kept, dropped = prefer_unrestricted(videos, details)
    assert kept == [] and dropped == videos


def test_prefer_unrestricted_keeps_locked_but_drops_nowhere_when_no_global():
    videos = [{"video_id": "us_only"}, {"video_id": "nowhere"}]
    details = {
        "us_only": {"is_region_restricted": True, "is_playable_nowhere": False},
        "nowhere": {"is_region_restricted": True, "is_playable_nowhere": True},
    }
    kept, dropped = prefer_unrestricted(videos, details)
    assert [v["video_id"] for v in kept] == ["us_only"]
    assert [v["video_id"] for v in dropped] == ["nowhere"]


def test_prefer_unrestricted_global_wins_over_locked_and_nowhere():
    videos = [{"video_id": "g"}, {"video_id": "us"}, {"video_id": "nowhere"}]
    details = {
        "g": {"is_region_restricted": False},
        "us": {"is_region_restricted": True},
        "nowhere": {"is_region_restricted": True, "is_playable_nowhere": True},
    }
    kept, dropped = prefer_unrestricted(videos, details)
    assert [v["video_id"] for v in kept] == ["g"]
    assert sorted(v["video_id"] for v in dropped) == ["nowhere", "us"]


# ── Re-check of cached entries (title_gate_guard) ─────────────────────────────

def _video(vid, title, tier):
    v = {"video_id": vid, "published_at": "2026-11-21", "tier_used": tier}
    if title is not None:
        v["title"] = title
    return v


def test_title_gate_guard_unit():
    fix = _bl_fixture()
    assert title_gate_guard(_video("a", REPLAY_TITLES[0], 2), fix, BL) == "title_gate_missing_required_term"
    assert title_gate_guard(_video("b", LIVE_TITLES[0], 2), fix, BL) == "title_gate_blocked"
    assert title_gate_guard(_video("c", HIGHLIGHTS_TITLES[0], 2), fix, BL) is None
    # Same replay title from a club source (tier 1) / broadcaster (tier 4): untouched.
    assert title_gate_guard(_video("d", REPLAY_TITLES[0], 1), fix, BL) is None
    assert title_gate_guard(_video("e", REPLAY_TITLES[0], 4), fix, BL) is None
    # A competition without required terms: tier-2 replay-style title untouched.
    assert title_gate_guard(_video("f", REPLAY_TITLES[0], 2), fix, "Ligue 1") is None
    # Missing / empty stored title: left alone.
    assert title_gate_guard(_video("g", None, 2), fix, BL) is None
    assert title_gate_guard(_video("h", "", 2), fix, BL) is None


def test_cached_replay_becomes_eligible_and_valid_entry_untouched():
    fixtures = [
        {"match_id": 1}, {"match_id": 2}, {"match_id": 3}, {"match_id": 4},
    ]
    existing = {"competition": BL, "matches": [
        {"match_id": 1, "videos": [_video("REPLAY", REPLAY_TITLES[0], 2)]},
        {"match_id": 2, "videos": [_video("HL", HIGHLIGHTS_TITLES[0], 2)]},
        {"match_id": 3, "videos": [_video("LIVE", LIVE_TITLES[0], 2)]},
        {"match_id": 4, "videos": [_video("NOTITLE", None, 2)]},
    ]}
    removed = apply_existing_record_guards(existing, fixtures)

    by_id = {m["match_id"]: m["videos"] for m in existing["matches"]}
    assert by_id[1] == []                                   # eligible for re-match
    assert [v["video_id"] for v in by_id[2]] == ["HL"]      # untouched
    assert by_id[3] == []
    assert [v["video_id"] for v in by_id[4]] == ["NOTITLE"]

    # Every drop is logged with match_id, old video_id and title.
    assert [(r["match_id"], r["video_id"], r["title"]) for r in removed] == [
        (1, "REPLAY", REPLAY_TITLES[0]),
        (3, "LIVE", LIVE_TITLES[0]),
    ]
    assert {r["action"] for r in removed} == {"removed_cached"}
    assert removed == H.GUARD_REJECTIONS


def test_title_gate_guard_is_registered_for_fetch_and_backfill():
    assert H.title_gate_guard in H.EXISTING_RECORD_GUARDS
