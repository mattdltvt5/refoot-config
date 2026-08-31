"""Tests for the embeddability preference in highlights_common.

A clip whose owner disabled embedding plays on youtube.com but cannot load in
the app's IFrame player (YouTube error 101/150 → a dead "Video unavailable"
card). The pipeline therefore prefers embeddable clips:

  - fetch_video_details() reports status.embeddable as is_embeddable
  - prefer_embeddable() drops non-embeddable clips when an embeddable one exists,
    but keeps them as a last resort so the app's "Watch on YouTube" fallback
    still has something to link to.

requests.get is patched — no real YouTube API calls are made.
"""

from unittest.mock import MagicMock, patch

from highlights_common import fetch_video_details, prefer_embeddable


# ── prefer_embeddable (pure) ─────────────────────────────────────────────────

def _v(video_id: str) -> dict:
    return {"video_id": video_id, "title": f"{video_id} title"}


def test_prefer_embeddable_drops_non_embeddable_when_an_embeddable_exists():
    videos = [_v("bad"), _v("good")]
    details = {
        "bad":  {"is_embeddable": False},
        "good": {"is_embeddable": True},
    }
    kept, dropped = prefer_embeddable(videos, details)
    assert [v["video_id"] for v in kept] == ["good"]
    assert [v["video_id"] for v in dropped] == ["bad"]


def test_prefer_embeddable_keeps_all_when_none_are_embeddable():
    # Last resort: a non-embeddable clip still plays on youtube.com (the app
    # shows a "Watch on YouTube" button), so it beats emitting no highlight.
    videos = [_v("bad1"), _v("bad2")]
    details = {
        "bad1": {"is_embeddable": False},
        "bad2": {"is_embeddable": False},
    }
    kept, dropped = prefer_embeddable(videos, details)
    assert [v["video_id"] for v in kept] == ["bad1", "bad2"]
    assert dropped == []


def test_prefer_embeddable_treats_unknown_metadata_as_embeddable():
    # A video absent from details (metadata unavailable) is never penalised,
    # matching how the duration/portrait filters treat missing entries.
    videos = [_v("unknown"), _v("bad")]
    details = {"bad": {"is_embeddable": False}}
    kept, dropped = prefer_embeddable(videos, details)
    assert [v["video_id"] for v in kept] == ["unknown"]
    assert [v["video_id"] for v in dropped] == ["bad"]


def test_prefer_embeddable_keeps_all_when_all_embeddable():
    videos = [_v("a"), _v("b")]
    details = {"a": {"is_embeddable": True}, "b": {"is_embeddable": True}}
    kept, dropped = prefer_embeddable(videos, details)
    assert [v["video_id"] for v in kept] == ["a", "b"]
    assert dropped == []


# ── fetch_video_details parses status.embeddable ─────────────────────────────

class _MockQuota:
    def increment(self, cap: int) -> None:
        pass


def _details_item(video_id: str, *, embeddable: bool) -> dict:
    return {
        "id": video_id,
        "contentDetails": {"duration": "PT5M0S"},
        "snippet": {"thumbnails": {"high": {"width": 480, "height": 360}}},
        "status": {"embeddable": embeddable},
    }


def _http_ok(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = data
    return resp


def test_fetch_video_details_reports_embeddability():
    body = {"items": [
        _details_item("emb", embeddable=True),
        _details_item("noemb", embeddable=False),
    ]}
    with patch("highlights_common.requests.get", return_value=_http_ok(body)):
        out = fetch_video_details(["emb", "noemb"], "fake-key", _MockQuota(), 10**6)
    assert out["emb"]["is_embeddable"] is True
    assert out["noemb"]["is_embeddable"] is False
    # Existing fields still present.
    assert out["emb"]["duration_seconds"] == 300
    assert out["emb"]["is_portrait"] is False


def test_fetch_video_details_defaults_missing_status_to_embeddable():
    # If the status part is absent, default True (don't penalise unknowns).
    item = {
        "id": "nostatus",
        "contentDetails": {"duration": "PT5M0S"},
        "snippet": {"thumbnails": {"high": {"width": 480, "height": 360}}},
    }
    with patch("highlights_common.requests.get",
               return_value=_http_ok({"items": [item]})):
        out = fetch_video_details(["nostatus"], "fake-key", _MockQuota(), 10**6)
    assert out["nostatus"]["is_embeddable"] is True
