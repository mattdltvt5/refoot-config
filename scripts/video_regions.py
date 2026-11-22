"""Per-video region availability in the highlights cache.

Each cached video carries ``region`` so the app can pick a clip that plays in
the viewer's country (country-aware selection, refoot_flutter):

  * ``{}``                       - plays everywhere
  * ``{"allowed": ["US", ...]}`` - plays only in these countries ([] = nowhere)
  * ``{"blocked": ["US", ...]}`` - plays everywhere except these
  * key absent                   - not checked yet

New matches get it from the matcher's existing videos.list call (no extra
quota). ``fill_missing_regions`` checks already-cached videos that lack it,
50 per videos.list unit, within a small per-run budget, newest seasons first.
A video the API no longer returns (deleted or made private) plays nowhere and
is stored as ``{"allowed": []}``, but only when the same batch returned other
videos, so a failed call never marks anything.

Uploaders can change availability later; this records what was true when
checked (re-checks are a possible follow-up).
"""
from __future__ import annotations

import logging
from pathlib import Path

from highlights_common import (
    HIGHLIGHTS_DIR,
    compact_region,
    fetch_video_details,
    load_json_file,
    write_json_atomic,
)

log = logging.getLogger(__name__)

# 10 units = up to 500 videos per run; the ~3,000 videos cached when this
# shipped are filled in about 7 runs (~35 minutes), then only new gaps remain.
REGION_FILL_UNITS_PER_RUN = 10


def _gameweek_files(root: Path) -> list[Path]:
    """highlights/{slug}/{season}/*.json, newest season first."""
    files = [p for p in root.glob("*/*/*.json") if p.parent.name.isdigit()]
    return sorted(files, key=lambda p: (-int(p.parent.name), str(p)))


def missing_region_ids(root: Path = HIGHLIGHTS_DIR) -> list[str]:
    """Video IDs without ``region``, in fill order (newest season first), unique."""
    seen: dict[str, None] = {}
    for path in _gameweek_files(root):
        data = load_json_file(path)
        if not isinstance(data, dict):
            continue
        for match in data.get("matches", []):
            for video in match.get("videos") or []:
                vid = video.get("video_id")
                if vid and "region" not in video:
                    seen.setdefault(vid, None)
    return list(seen)


def apply_regions(regions: dict[str, dict], root: Path = HIGHLIGHTS_DIR) -> list[Path]:
    """Write ``region`` onto every cached copy of these videos; returns changed files."""
    changed: list[Path] = []
    for path in _gameweek_files(root):
        data = load_json_file(path)
        if not isinstance(data, dict):
            continue
        touched = False
        for match in data.get("matches", []):
            for video in match.get("videos") or []:
                vid = video.get("video_id")
                if vid in regions and video.get("region") != regions[vid]:
                    video["region"] = regions[vid]
                    touched = True
        if touched:
            write_json_atomic(path, data)  # generated_at untouched: not new content
            changed.append(path)
    return changed


def fill_missing_regions(
    yt_key: str,
    quota,
    cap: int,
    budget_units: int = REGION_FILL_UNITS_PER_RUN,
    root: Path = HIGHLIGHTS_DIR,
) -> list[Path]:
    """Check up to ``budget_units`` x 50 cached videos lacking ``region``.

    Raises QuotaCapReached (from fetch_video_details) like the matcher; regions
    found before that are still written.
    """
    ids = missing_region_ids(root)
    if not ids or budget_units <= 0:
        return []
    regions: dict[str, dict] = {}
    try:
        for start in range(0, min(len(ids), budget_units * 50), 50):
            batch = ids[start:start + 50]
            details = fetch_video_details(batch, yt_key, quota, cap)
            if not details:
                continue  # failed call (or every video gone): mark nothing
            for vid in batch:
                if vid in details:
                    regions[vid] = compact_region(details[vid].get("region_restriction"))
                else:
                    regions[vid] = {"allowed": []}  # deleted / private: plays nowhere
    finally:
        changed = apply_regions(regions, root) if regions else []
        if regions:
            log.info(f"Regions: recorded for {len(regions)} cached video(s) "
                     f"in {len(changed)} file(s); {max(len(ids) - len(regions), 0)} still unchecked")
    return changed
