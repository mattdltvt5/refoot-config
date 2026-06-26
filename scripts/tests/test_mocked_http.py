"""Mocked-HTTP tests for highlights_common.search_playlist().

requests.get is patched — no real YouTube API calls are made.

Invariants covered:
  11. requires_both_teams=True rejects a title naming only one team
  12. bypass_highlight_allowlist applies only to tiers 1c/1d/4 (curated playlists),
      never to tiers 1a/1b (club-channel uploads); the blocklist still fires even
      when the allowlist is bypassed
"""

from unittest.mock import MagicMock, patch

from highlights_common import search_playlist


# ── Shared helpers ────────────────────────────────────────────────────────────

# Minimal fixture dict.  home_team / away_team are optional in search_playlist
# (read via .get()), but must be provided here so TEAM_TITLE_ALIASES is consulted
# (keyed by exact FD team.name).
_FIXTURE = {
    "date": "2025-10-03",
    "home_team": "Arsenal FC",
    "home_short": "Arsenal",
    "home_tla": "ARS",
    "away_team": "Chelsea FC",
    "away_short": "Chelsea",
    "away_tla": "CHE",
}

_PLAYLIST_ID = "PLtest123"
_YT_KEY = "fake-key-no-real-calls"
_CAP = 1_000_000  # unreachably large; quota is never the gate in these tests


class _MockQuota:
    """Stub QuotaTracker — never reads or writes quota-tracker.json."""

    def increment(self, cap: int) -> None:  # noqa: D102
        pass

    @property
    def over_incremental_cap(self) -> bool:  # noqa: D102
        return False


def _yt_response(items: list, next_page_token: str = "") -> dict:
    """Build a minimal YouTube playlistItems.list response body."""
    return {
        "items": items,
        "nextPageToken": next_page_token or None,
    }


def _yt_item(video_id: str, title: str, pub_date: str) -> dict:
    """Build a single playlistItems.list snippet item."""
    return {
        "snippet": {
            "title": title,
            "publishedAt": f"{pub_date}T12:00:00Z",
            "resourceId": {"videoId": video_id},
        }
    }


def _http_ok(data: dict) -> MagicMock:
    """Return a mock requests.Response with status 200 and the given body."""
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = data
    return resp


def _run(fixture=_FIXTURE, items=(), **kwargs):
    """Call search_playlist with a mocked HTTP layer returning `items`."""
    response = _yt_response(list(items))
    with patch("highlights_common.requests.get", return_value=_http_ok(response)):
        return search_playlist(
            _PLAYLIST_ID,
            _YT_KEY,
            fixture,
            "Premier League",
            _MockQuota(),
            _CAP,
            **kwargs,
        )


# ── Invariant 11: requires_both_teams ────────────────────────────────────────

def test_requires_both_teams_rejects_single_team_title():
    """requires_both_teams=True must reject a title that names only one team.

    Without this guard, a "PSG vs Nantes" fixture would wrongly accept a
    "Rennais vs Nantes" video because 'nantes' (away) appears in both titles.
    """
    results = _run(
        items=[_yt_item("vid001", "Arsenal FC Highlights Extended", "2025-10-03")],
        requires_both_teams=True,
    )
    assert results == [], (
        "Title naming only the home team must be rejected when requires_both_teams=True"
    )


def test_requires_both_teams_also_rejects_away_only_title():
    """requires_both_teams=True must reject a title that names only the away team."""
    results = _run(
        items=[_yt_item("vid002", "Chelsea FC | Highlights", "2025-10-03")],
        requires_both_teams=True,
    )
    assert results == [], (
        "Title naming only the away team must be rejected when requires_both_teams=True"
    )


def test_requires_both_teams_accepts_title_with_both():
    """requires_both_teams=True accepts a title that names both teams."""
    results = _run(
        items=[
            _yt_item("vid003", "Arsenal vs Chelsea | Match Highlights", "2025-10-03")
        ],
        requires_both_teams=True,
    )
    assert len(results) == 1
    assert results[0]["video_id"] == "vid003"


def test_requires_both_teams_false_accepts_single_team():
    """requires_both_teams=False (default, tiers 1a/1b) accepts a one-team title.

    Club-channel upload playlists are already scoped to one team — only the
    opponent's name needs to appear in the title.
    """
    results = _run(
        items=[_yt_item("vid004", "Arsenal FC Highlights", "2025-10-03")],
        requires_both_teams=False,
    )
    assert len(results) == 1, (
        "Single-team title must be accepted when requires_both_teams=False"
    )


# ── Invariant 12: bypass_highlight_allowlist ──────────────────────────────────

def test_bypass_false_rejects_title_without_allowlist_term():
    """bypass_highlight_allowlist=False (tiers 1a/1b) rejects a title with no highlight keyword.

    A scoreline-only title such as 'Arsenal vs Chelsea 2-1' has no blocklist
    term but also no allowlist term ('highlights', 'résumé', 'gol', …).
    Tiers 1a/1b must reject it because their uploads feeds contain non-highlight
    content (press conferences, training clips, etc.).
    """
    results = _run(
        items=[_yt_item("vid005", "Arsenal vs Chelsea 2-1", "2025-10-03")],
        requires_both_teams=True,
        bypass_highlight_allowlist=False,
    )
    assert results == [], (
        "Scoreline-only title must be rejected when bypass_highlight_allowlist=False"
    )


def test_bypass_true_accepts_title_without_allowlist_term():
    """bypass_highlight_allowlist=True (tiers 1c/1d/4) accepts a title with no highlight keyword.

    Curated playlists (team competition playlists and broadcaster playlists) are
    pre-filtered by the playlist owner — the per-video title need not repeat the
    word 'highlights'.  bypass=True reflects this.
    """
    results = _run(
        items=[_yt_item("vid006", "Arsenal vs Chelsea 2-1", "2025-10-03")],
        requires_both_teams=True,
        bypass_highlight_allowlist=True,
    )
    assert len(results) == 1, (
        "Scoreline-only title must be accepted when bypass_highlight_allowlist=True"
    )
    assert results[0]["video_id"] == "vid006"


def test_bypass_true_still_applies_blocklist():
    """bypass_highlight_allowlist=True does NOT bypass the blocklist.

    The blocklist always wins — a press-conference clip that slipped into a
    curated playlist must still be rejected.
    """
    results = _run(
        items=[
            _yt_item("vid007", "Arsenal vs Chelsea | Press Conference", "2025-10-03")
        ],
        requires_both_teams=True,
        bypass_highlight_allowlist=True,
    )
    assert results == [], (
        "Blocklist term 'press conference' must reject even when bypass=True"
    )


def test_outside_date_window_always_rejected():
    """A video published 10 days after the fixture is rejected regardless of other flags."""
    results = _run(
        items=[_yt_item("vid008", "Arsenal vs Chelsea | Match Highlights", "2025-10-13")],
        requires_both_teams=True,
        bypass_highlight_allowlist=True,
    )
    assert results == [], "Video outside date window must be rejected"
