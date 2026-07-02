"""Tests for the playlist owner verification guard.

Invariants covered:
  - Owner mismatch: a right-length ID filed under the wrong broadcaster label is
    flagged as an error, not silently passed.
  - Unresolvable ID: a playlist that the API cannot resolve (private/deleted) is
    flagged as an error.
  - Correct label: a correctly-labelled, resolvable ID produces no errors.
  - No re-fetch for cached IDs: an ID already in playlist-owners.json does not
    trigger an API call.
  - Quota accounting: each new-ID fetch increments the quota tracker by 1.
  - SBS relabel: the real SBS Sport playlist passes when filed under 'SBS Sport'.

No live API calls are made.  All HTTP is injected via a mock session.
"""

import json
from unittest.mock import MagicMock

import pytest

from highlights_common import (
    fetch_playlist_owner,
    labels_match,
    verify_playlist_owners,
)


# ── Fixtures and helpers ──────────────────────────────────────────────────────

_FAKE_KEY = "fake-api-key-no-real-calls"

# A valid-format playlist ID (passes extract_playlist_id regex).
_PL_SBS   = "PLNuJDkj3zBvPVhoKC6Oq8j4w7AH9l-ejG"
_PL_OTHER = "PLSoN6Th-EepMUaxmTobuR_SBwVkdkxdfO"


def _yt_playlists_response(channel_id: str, channel_title: str, pl_title: str) -> dict:
    """Build a minimal playlists.list API response."""
    return {
        "items": [{
            "snippet": {
                "channelId":    channel_id,
                "channelTitle": channel_title,
                "title":        pl_title,
            }
        }]
    }


def _empty_response() -> dict:
    return {"items": []}


def _mock_session(response_body: dict, status_code: int = 200) -> MagicMock:
    """Return a mock requests.Session whose .get() returns a fixed response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = response_body
    resp.raise_for_status.return_value = None
    sess = MagicMock()
    sess.get.return_value = resp
    return sess


class _MockQuota:
    """Stub QuotaTracker — never reads or writes quota-tracker.json."""

    def __init__(self) -> None:
        self.calls = 0

    def increment(self, cap: int) -> None:
        self.calls += 1


def _sources(playlists: dict) -> dict:
    """Minimal sources.json structure."""
    comps = list(playlists.keys())
    return {
        "competitions": {c: "" for c in comps},
        "teams": {},
        "playlists": playlists,
        "teamPlaylists": {},
        "teamLists": {},
    }


# ── labels_match ──────────────────────────────────────────────────────────────

class TestLabelsMatch:
    def test_exact_match(self):
        assert labels_match("SBS Sport", "SBS Sport")

    def test_case_insensitive(self):
        assert labels_match("sbs sport", "SBS Sport")

    def test_sport_suffix_stripped(self):
        assert labels_match("SBS", "SBS Sport")

    def test_sports_suffix_stripped(self):
        assert labels_match("FOX sports", "Fox Sports")

    def test_genuine_wrong_owner(self):
        assert not labels_match("FIFA", "SBS Sport")

    def test_cbs_golazo_vs_cbs_sports(self):
        # 'CBS Sport Golazo' label vs 'CBS Sports' channel — same org, passes.
        assert labels_match("CBS Sport Golazo", "CBS Sports")

    def test_bundesliga_tv(self):
        assert labels_match("Bundesliga TV", "Bundesliga")

    def test_tudn_usa(self):
        assert labels_match("TUDN USA", "TUDN")

    def test_completely_different_entities(self):
        assert not labels_match("Telemundo", "SBS Sport")


# ── fetch_playlist_owner ──────────────────────────────────────────────────────

class TestFetchPlaylistOwner:
    def test_returns_owner_on_success(self):
        sess = _mock_session(
            _yt_playlists_response("UCabc123", "SBS Sport", "FIFA WC Highlights")
        )
        result = fetch_playlist_owner(_PL_SBS, _FAKE_KEY, session=sess)

        assert result is not None
        assert result["channel_title"] == "SBS Sport"
        assert result["channel_id"]    == "UCabc123"
        assert result["playlist_title"] == "FIFA WC Highlights"

    def test_returns_none_when_playlist_unresolvable(self):
        sess = _mock_session(_empty_response())
        result = fetch_playlist_owner(_PL_OTHER, _FAKE_KEY, session=sess)
        assert result is None

    def test_quota_incremented_on_success(self):
        quota = _MockQuota()
        sess  = _mock_session(
            _yt_playlists_response("UCabc123", "SBS Sport", "WC Highlights")
        )
        fetch_playlist_owner(_PL_SBS, _FAKE_KEY, session=sess, quota=quota)
        assert quota.calls == 1

    def test_quota_incremented_even_when_empty(self):
        # Empty items = resolved call (200 OK), still costs 1 unit.
        quota = _MockQuota()
        sess  = _mock_session(_empty_response())
        fetch_playlist_owner(_PL_SBS, _FAKE_KEY, session=sess, quota=quota)
        assert quota.calls == 1


# ── verify_playlist_owners ────────────────────────────────────────────────────

class TestVerifyPlaylistOwners:
    def _run(
        self,
        playlists: dict,
        session: MagicMock,
        cached: dict | None = None,
        tmp_path=None,
        api_key: str = _FAKE_KEY,
    ) -> tuple[list[str], "_MockQuota"]:
        import json

        quota = _MockQuota()
        sp = tmp_path / "sources.json"
        op = tmp_path / "owners.json"

        sp.write_text(json.dumps(_sources(playlists)), encoding="utf-8")
        if cached is not None:
            op.write_text(json.dumps(cached), encoding="utf-8")

        errors = verify_playlist_owners(
            api_key,
            quota=quota,
            session=session,
            sources_path=sp,
            owners_path=op,
        )
        return errors, quota

    # ── owner mismatch ────────────────────────────────────────────────────────

    def test_owner_mismatch_is_flagged(self, tmp_path):
        # Playlist is owned by SBS Sport but filed under 'FIFA' — must error.
        sess = _mock_session(
            _yt_playlists_response("UCsbsxx", "SBS Sport", "WC Highlights")
        )
        errors, _ = self._run(
            {"World Cup": {"FIFA": [_PL_SBS]}},
            sess, tmp_path=tmp_path,
        )
        assert len(errors) == 1
        assert "SBS Sport" in errors[0]
        assert "FIFA" in errors[0]

    def test_cached_owner_mismatch_still_flagged(self, tmp_path):
        # ID is in cache with correct channel_title but label is wrong — must error.
        cached = {_PL_SBS: {"channel_title": "SBS Sport", "channel_id": "UCsbs"}}
        sess = _mock_session(_empty_response())  # must NOT be called
        errors, quota = self._run(
            {"World Cup": {"FIFA": [_PL_SBS]}},
            sess, cached=cached, tmp_path=tmp_path,
        )
        assert len(errors) == 1
        assert "SBS Sport" in errors[0]
        # No API call for a cached ID.
        assert quota.calls == 0

    # ── unresolvable ID ───────────────────────────────────────────────────────

    def test_unresolvable_id_is_flagged(self, tmp_path):
        sess = _mock_session(_empty_response())
        errors, _ = self._run(
            {"World Cup": {"SBS Sport": [_PL_SBS]}},
            sess, tmp_path=tmp_path,
        )
        assert len(errors) == 1
        assert "resolve" in errors[0].lower() or "private" in errors[0].lower()

    def test_cached_unresolvable_re_flagged(self, tmp_path):
        # An ID previously recorded as unresolvable (channel_title=None) should
        # keep being flagged until the operator fixes or removes it.
        cached = {_PL_SBS: {"channel_title": None, "channel_id": None, "error": "unresolvable"}}
        sess = _mock_session(_empty_response())
        errors, quota = self._run(
            {"World Cup": {"SBS Sport": [_PL_SBS]}},
            sess, cached=cached, tmp_path=tmp_path,
        )
        assert len(errors) == 1
        assert quota.calls == 0  # no re-fetch

    # ── correct label ─────────────────────────────────────────────────────────

    def test_correct_label_passes(self, tmp_path):
        sess = _mock_session(
            _yt_playlists_response("UCsbsxx", "SBS Sport", "FIFA WC Highlights")
        )
        errors, _ = self._run(
            {"World Cup": {"SBS Sport": [_PL_SBS]}},
            sess, tmp_path=tmp_path,
        )
        assert errors == []

    # ── no re-fetch for cached IDs ────────────────────────────────────────────

    def test_cached_id_skips_api_call(self, tmp_path):
        cached = {_PL_SBS: {"channel_title": "SBS Sport", "channel_id": "UCsbs"}}
        sess = MagicMock()  # .get should never be called
        errors, quota = self._run(
            {"World Cup": {"SBS Sport": [_PL_SBS]}},
            sess, cached=cached, tmp_path=tmp_path,
        )
        assert errors == []
        assert quota.calls == 0
        sess.get.assert_not_called()

    # ── quota accounting ──────────────────────────────────────────────────────

    def test_quota_incremented_once_per_new_id(self, tmp_path):
        # Two new IDs, both resolvable → 2 quota units.
        def _side_effect(url, params, timeout):
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = _yt_playlists_response(
                "UCx", "FOX sports", "WC Highlights"
            )
            return resp

        sess = MagicMock()
        sess.get.side_effect = _side_effect

        errors, quota = self._run(
            {"World Cup": {
                "FOX sports": [_PL_SBS, _PL_OTHER],
            }},
            sess, tmp_path=tmp_path,
        )
        assert quota.calls == 2

    def test_no_quota_for_all_cached(self, tmp_path):
        cached = {
            _PL_SBS:   {"channel_title": "SBS Sport",  "channel_id": "UCsbs"},
            _PL_OTHER: {"channel_title": "FOX sports",  "channel_id": "UCfox"},
        }
        sess = MagicMock()
        errors, quota = self._run(
            {"World Cup": {
                "SBS Sport": [_PL_SBS],
                "FOX sports": [_PL_OTHER],
            }},
            sess, cached=cached, tmp_path=tmp_path,
        )
        assert errors == []
        assert quota.calls == 0

    # ── missing API key ───────────────────────────────────────────────────────

    def test_missing_api_key_flagged_for_uncached_ids(self, tmp_path):
        sess = MagicMock()
        errors, quota = self._run(
            {"World Cup": {"SBS Sport": [_PL_SBS]}},
            sess, tmp_path=tmp_path, api_key="",
        )
        assert len(errors) == 1
        assert "YOUTUBE_API_KEY" in errors[0]
        assert quota.calls == 0

    def test_missing_api_key_ok_when_all_cached(self, tmp_path):
        cached = {_PL_SBS: {"channel_title": "SBS Sport", "channel_id": "UCsbs"}}
        sess = MagicMock()
        errors, quota = self._run(
            {"World Cup": {"SBS Sport": [_PL_SBS]}},
            sess, cached=cached, tmp_path=tmp_path, api_key="",
        )
        assert errors == []
        assert quota.calls == 0

    # ── SBS relabel and seed-entry first-resolution (Gap 1) ──────────────────

    def test_sbs_entry_passes_after_api_verification(self, tmp_path):
        """
        The relabelled SBS Sport entry passes once the verify workflow has run
        and populated a real channel_id.  Represents the stable state after
        the first automated resolution.
        """
        cached = {
            _PL_SBS: {
                "competition":    "World Cup",
                "label":          "SBS Sport",
                "channel_id":     "UCsbs_verified_by_workflow",
                "channel_title":  "SBS Sport",
                "playlist_title": "FIFA World Cup 2026™ Match Highlights",
                "verified_at":    "2026-07-02T12:00:00Z",
            }
        }
        sess = MagicMock()
        errors, quota = self._run(
            {"World Cup": {"SBS Sport": [_PL_SBS]}},
            sess, cached=cached, tmp_path=tmp_path, api_key="",
        )
        assert errors == [], f"Expected no errors, got: {errors}"
        assert quota.calls == 0
        sess.get.assert_not_called()

    def test_seeded_entry_with_null_channel_id_triggers_fetch(self, tmp_path):
        """
        Gap 1: a manually-seeded entry (channel_id = null) must NOT be trusted
        as verified.  The guard must fetch its owner and record the real
        channel_id rather than relying on the hand-asserted channel_title.
        """
        seeded_cache = {
            _PL_SBS: {
                "competition":    "World Cup",
                "label":          "SBS Sport",
                "channel_id":     None,          # ← manually seeded, no API call yet
                "channel_title":  "SBS Sport",
                "playlist_title": "FIFA World Cup 2026™ Match Highlights",
                "verified_at":    "2026-07-02T00:00:00Z",
                "note":           "Manually verified; channel_id pending first automated run.",
            }
        }
        sess = _mock_session(
            _yt_playlists_response("UCreal_sbs_channel", "SBS Sport", "FIFA WC Highlights")
        )
        errors, quota = self._run(
            {"World Cup": {"SBS Sport": [_PL_SBS]}},
            sess, cached=seeded_cache, tmp_path=tmp_path,
        )
        # Label check passes: "SBS Sport" owns playlist filed under "SBS Sport".
        assert errors == [], f"Expected no errors after fetch, got: {errors}"
        # The API WAS called — the guard did not trust the seeded entry.
        sess.get.assert_called_once()
        assert quota.calls == 1
        # The owners file is updated with the real channel_id.
        op = tmp_path / "owners.json"
        updated = json.loads(op.read_text(encoding="utf-8"))
        assert updated[_PL_SBS]["channel_id"] == "UCreal_sbs_channel", (
            "channel_id must be populated from the API, not left as the seeded null"
        )

    def test_seeded_entry_with_null_channel_id_and_no_api_key_errors(self, tmp_path):
        """
        A seeded entry (channel_id = null) with no API key must error — the
        guard cannot trust the assertion without a real fetch.
        """
        seeded_cache = {
            _PL_SBS: {
                "channel_id": None,
                "channel_title": "SBS Sport",
            }
        }
        sess = MagicMock()
        errors, quota = self._run(
            {"World Cup": {"SBS Sport": [_PL_SBS]}},
            sess, cached=seeded_cache, tmp_path=tmp_path, api_key="",
        )
        assert len(errors) == 1
        assert "YOUTUBE_API_KEY" in errors[0]
        assert quota.calls == 0
        sess.get.assert_not_called()

    # ── wrong-owner / matching-title (Gap 3) ─────────────────────────────────

    def test_wrong_owner_with_matching_playlist_title_is_flagged(self, tmp_path):
        """
        Gap 3: a playlist titled 'FIFA World Cup 2026™ Match Highlights' but
        OWNED by 'SBS Sport', filed under a 'FIFA' broadcaster label, must be
        flagged as a mismatch.

        The comparison must use channel_title ('SBS Sport') — the owning
        channel's name — NOT playlist_title ('FIFA World Cup 2026™ Match
        Highlights').  If the comparison accidentally keyed on the playlist
        title, the 'FIFA' token in it would satisfy the label, hiding the
        wrong-owner bug this guard exists to catch.
        """
        sess = _mock_session(
            _yt_playlists_response(
                "UCsbsxx",
                "SBS Sport",                              # channel_title: real owner
                "FIFA World Cup 2026™ Match Highlights",  # playlist_title: contains "FIFA"
            )
        )
        errors, _ = self._run(
            {"World Cup": {"FIFA": [_PL_SBS]}},  # label = "FIFA"
            sess, tmp_path=tmp_path,
        )
        # channel_title "SBS Sport" ≠ "FIFA" → mismatch flagged.
        assert len(errors) == 1, (
            "Mismatch expected: channel_title 'SBS Sport' must not satisfy 'FIFA' label "
            "even though the playlist title contains 'FIFA' tokens"
        )
        assert "SBS Sport" in errors[0]
        assert "FIFA" in errors[0]

    def test_tolerant_label_sbs_vs_sbs_sport_passes(self, tmp_path):
        """
        Gap 3b: the tolerant owner-vs-label matching works for 'SBS' vs 'SBS Sport'.
        Qualifier words ('Sport', 'TV', etc.) must not cause a false mismatch.
        """
        sess = _mock_session(
            _yt_playlists_response("UCsbs", "SBS Sport", "Any Playlist Title")
        )
        errors, _ = self._run(
            {"World Cup": {"SBS": [_PL_SBS]}},  # label "SBS" vs channel "SBS Sport"
            sess, tmp_path=tmp_path,
        )
        assert errors == [], (
            "Label 'SBS' should match channel_title 'SBS Sport' — "
            "qualifier-word tolerance must not block the correct owner"
        )

    # ── quota accounting for playlists.list (Gap 2) ──────────────────────────

    def test_playlists_list_call_costs_one_quota_unit(self):
        """
        Gap 2: playlists.list?part=snippet costs 1 quota unit, identical to
        playlistItems.list.  Each call to fetch_playlist_owner must increment
        the tracker by exactly 1 — no more, no less.
        """
        quota = _MockQuota()
        sess  = _mock_session(
            _yt_playlists_response("UCsbs", "SBS Sport", "WC Highlights")
        )
        fetch_playlist_owner(_PL_SBS, _FAKE_KEY, session=sess, quota=quota)
        assert quota.calls == 1, (
            "playlists.list?part=snippet must count as exactly 1 quota unit "
            "(same as playlistItems.list)"
        )
