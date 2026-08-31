"""Tests for the region-availability preference in highlights_common.

A geo-locked clip (e.g. a US-only broadcaster upload — contentDetails.
regionRestriction.allowed = ["US"]) does not play for viewers elsewhere; in the
app it is a silent black box with no "Watch on YouTube" fallback. The pipeline
therefore prefers globally-available clips:

  - fetch_video_details() reports regionRestriction as is_region_restricted
  - prefer_unrestricted() drops region-locked clips when a global one exists,
    keeping them only as a last resort.

requests.get is patched — no real YouTube API calls are made.
"""

from unittest.mock import MagicMock, patch

from highlights_common import fetch_video_details, prefer_unrestricted


# ── prefer_unrestricted (pure) ───────────────────────────────────────────────

def _v(video_id: str) -> dict:
    return {"video_id": video_id, "title": f"{video_id} title"}


def test_drops_region_locked_when_a_global_clip_exists():
    videos = [_v("us_only"), _v("global")]
    details = {
        "us_only": {"is_region_restricted": True},
        "global":  {"is_region_restricted": False},
    }
    kept, dropped = prefer_unrestricted(videos, details)
    assert [v["video_id"] for v in kept] == ["global"]
    assert [v["video_id"] for v in dropped] == ["us_only"]


def test_keeps_all_when_every_clip_is_region_locked():
    videos = [_v("us_only"), _v("uk_only")]
    details = {
        "us_only": {"is_region_restricted": True},
        "uk_only": {"is_region_restricted": True},
    }
    kept, dropped = prefer_unrestricted(videos, details)
    assert [v["video_id"] for v in kept] == ["us_only", "uk_only"]
    assert dropped == []


def test_unknown_metadata_is_treated_as_global():
    videos = [_v("unknown"), _v("us_only")]
    details = {"us_only": {"is_region_restricted": True}}
    kept, dropped = prefer_unrestricted(videos, details)
    assert [v["video_id"] for v in kept] == ["unknown"]
    assert [v["video_id"] for v in dropped] == ["us_only"]


def test_keeps_all_when_all_global():
    videos = [_v("a"), _v("b")]
    details = {"a": {"is_region_restricted": False}, "b": {}}
    kept, dropped = prefer_unrestricted(videos, details)
    assert [v["video_id"] for v in kept] == ["a", "b"]
    assert dropped == []


# ── fetch_video_details parses regionRestriction ─────────────────────────────

class _MockQuota:
    def increment(self, cap: int) -> None:
        pass


def _item(video_id: str, region: dict | None) -> dict:
    cd = {"duration": "PT5M0S"}
    if region is not None:
        cd["regionRestriction"] = region
    return {
        "id": video_id,
        "contentDetails": cd,
        "snippet": {"thumbnails": {"high": {"width": 480, "height": 360}}},
        "status": {"embeddable": True},
    }


def _http_ok(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = data
    return resp


def test_fetch_video_details_flags_region_restriction():
    body = {"items": [
        _item("allowed_us", {"allowed": ["US"]}),
        _item("blocked_de", {"blocked": ["DE"]}),
        _item("global", None),
        _item("empty_lists", {"allowed": []}),  # empty → not a real restriction
    ]}
    with patch("highlights_common.requests.get", return_value=_http_ok(body)):
        out = fetch_video_details(
            ["allowed_us", "blocked_de", "global", "empty_lists"],
            "fake-key", _MockQuota(), 10**6)
    assert out["allowed_us"]["is_region_restricted"] is True
    assert out["blocked_de"]["is_region_restricted"] is True
    assert out["global"]["is_region_restricted"] is False
    assert out["empty_lists"]["is_region_restricted"] is False
    # Existing fields still present.
    assert out["global"]["duration_seconds"] == 300
    assert out["global"]["is_embeddable"] is True
