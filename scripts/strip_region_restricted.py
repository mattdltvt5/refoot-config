#!/usr/bin/env python3
"""
strip_region_restricted.py — drop geo-locked clips from already-linked highlights
when a globally-available alternative exists for the same match.

A geo-locked clip (e.g. a US-only broadcaster upload like CBS Sports Golazo) does
not play for viewers outside its allowed region — in the app it is a silent black
box that YouTube doesn't even flag, so no "Watch on YouTube" fallback fires. The
fetch pipeline now prefers globally-available clips (highlights_common.
prefer_unrestricted); this one-off pass applies the same rule to EXISTING data.

Rule (mirrors prefer_unrestricted): per match, if there is ≥1 global AND ≥1
region-locked clip, drop the region-locked one(s). If a match has ONLY
region-locked clips, it is LEFT untouched (removing its only clip would strand
in-region viewers who can watch it, and no global alternative is present to swap
in — a future backfill with the region-aware pipeline is the path there).

Region availability is probed KEYLESS from each clip's public watch page
(schema.org regionsAllowed meta) — NO API key, NO Data-API quota. A clip whose
allow-list omits a meaningful number of countries is treated as restricted; a
list of ~all countries (or no meta) is global. Uncertain results (network error)
are treated as global so a transient failure never strips a good clip.

Zero YouTube Data API quota. Reads and atomically rewrites JSON only.
Run with --dry-run to audit without writing.
"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from highlights_common import (
    HIGHLIGHTS_DIR,
    generate_summary,
    load_json_file,
    utc_now_iso,
    write_json_atomic,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

_REGIONS_META = re.compile(r'itemprop="regionsAllowed"\s+content="([^"]*)"')
# A truly-global video lists ~all countries (~249). Treat an allow-list shorter
# than this as a real geo-restriction (blocked somewhere that matters).
_GLOBAL_MIN = 245


def is_region_restricted(video_id: str, session: requests.Session,
                         cache: dict) -> bool:
    """True if the clip is geo-restricted (short regionsAllowed list). Unknown →
    False (never strip on uncertainty). Cached per video id."""
    if video_id in cache:
        return cache[video_id]
    result = False
    try:
        html = session.get(
            f"https://www.youtube.com/watch?v={video_id}", timeout=10
        ).text
        m = _REGIONS_META.search(html)
        if m:
            allowed = [c for c in m.group(1).split(",") if c]
            result = 0 < len(allowed) < _GLOBAL_MIN
    except requests.RequestException as exc:
        log.warning(f"  network error for {video_id}: {exc} — treating as global")
        result = False
    cache[video_id] = result
    return result


def data_files() -> list[Path]:
    return sorted(
        p for p in HIGHLIGHTS_DIR.glob("*/*/*.json") if p.name != "summary.json"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Audit only; do not write any files.")
    ap.add_argument("--sleep", type=float, default=0.05,
                    help="Seconds between unique watch-page probes (politeness).")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if not HIGHLIGHTS_DIR.exists():
        log.error(f"Highlights directory not found: {HIGHLIGHTS_DIR}")
        sys.exit(1)

    session = requests.Session()
    cache: dict[str, bool] = {}
    total_removed = 0
    changed_files: list[Path] = []
    probed = 0

    print("=" * 72)
    print("Strip geo-locked clips where a global alternative exists (keyless)")
    print("Rule: drop region-locked clips only when the match keeps >=1 global")
    print(f"Mode: {'DRY RUN (no writes)' if args.dry_run else 'APPLY'}")
    print("=" * 72)

    for path in data_files():
        data = load_json_file(path)
        if not data or "matches" not in data:
            continue
        file_changed = False
        rel = path.relative_to(HIGHLIGHTS_DIR)

        for match in data.get("matches", []):
            videos = match.get("videos", []) or []
            if len(videos) < 2:
                continue  # a single clip is never dropped (see rule)

            flags = []
            for v in videos:
                vid = v.get("video_id", "")
                if not vid:
                    flags.append(False)
                    continue
                if vid not in cache:
                    probed += 1
                    r = is_region_restricted(vid, session, cache)
                    if args.sleep:
                        time.sleep(args.sleep)
                else:
                    r = cache[vid]
                flags.append(r)

            has_global = not all(flags)
            has_locked = any(flags)
            if not (has_global and has_locked):
                continue

            kept = [v for v, locked in zip(videos, flags) if not locked]
            removed = [v for v, locked in zip(videos, flags) if locked]
            home = match.get("home_team", "?")
            away = match.get("away_team", "?")
            print(f"\n{rel} — {home} vs {away} ({match.get('date', '?')}):")
            for v in removed:
                print(f"  REMOVE  [{v.get('video_id')}]  \"{v.get('title', '')}\"")
            for v in kept:
                print(f"  keep    [{v.get('video_id')}]  \"{v.get('title', '')}\"")
            match["videos"] = kept
            file_changed = True
            total_removed += len(removed)

        if file_changed:
            changed_files.append(path)
            if not args.dry_run:
                data["generated_at"] = utc_now_iso()
                write_json_atomic(path, data)

    print()
    print("=" * 72)
    print("Summary")
    print("=" * 72)
    print(f"  Unique videos probed:            {probed}")
    print(f"  Geo-locked clips removed:        {total_removed}")
    print(f"  Files {'that would change' if args.dry_run else 'rewritten'}:       "
          f"{len(changed_files)}")
    print("=" * 72)

    if changed_files and not args.dry_run:
        generate_summary()
        log.info(f"summary.json regenerated ({len(changed_files)} file(s) changed)")


if __name__ == "__main__":
    main()
