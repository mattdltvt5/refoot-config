#!/usr/bin/env python3
"""
prune_non_embeddable.py — remove embedding-disabled clips from already-linked
highlights when an embeddable alternative exists for the same match.

Why: many linked clips are public but have embedding disabled by the uploader.
They play on youtube.com but cannot load in the app's YouTube IFrame player
(error 101/150 → a "Video unavailable" card; the app now shows a "Watch on
YouTube" fallback for them). The pipeline was picking such a clip even when the
SAME match also had an embeddable upload. `highlights_common.prefer_embeddable`
now avoids that for NEW resolutions; this one-off pass applies the same rule to
EXISTING data so the app immediately picks the embeddable clip.

Rule (mirrors prefer_embeddable): per match, if there is ≥1 embeddable AND ≥1
non-embeddable clip, drop the non-embeddable one(s). If ALL clips are
non-embeddable, KEEP them — a clip that still plays on youtube.com (via the
app fallback) beats no highlight, and dropping would only empty the match.

Embeddability is probed via YouTube's public **oEmbed** endpoint — NO API key
and NO quota. HTTP 200 = embeddable; 401 = embedding-disabled or private;
404 = removed. Anything uncertain (network error, 429, 5xx) is treated as
embeddable so a transient failure never strips a good clip.

Zero YouTube Data API quota. Reads and atomically rewrites JSON files only.
Run with --dry-run first to audit without writing.
"""

import argparse
import logging
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

OEMBED = "https://www.youtube.com/oembed"


def is_embeddable(video_id: str, session: requests.Session, cache: dict) -> bool:
    """True if the clip can be embedded (oEmbed 200). Unknown → True (never strip
    on uncertainty). Cached per video id."""
    if video_id in cache:
        return cache[video_id]
    url = f"https://www.youtube.com/watch?v={video_id}"
    result = True  # default: treat as embeddable unless we get a definitive 200-vs-4xx
    for attempt in range(3):
        try:
            resp = session.get(
                OEMBED, params={"url": url, "format": "json"}, timeout=10
            )
        except requests.RequestException as exc:
            log.warning(f"  oEmbed network error for {video_id}: {exc} — keeping")
            result = True
            break
        if resp.status_code == 200:
            result = True
            break
        if resp.status_code in (401, 404):
            # 401 = embedding disabled or private; 404 = removed. Not embeddable.
            result = False
            break
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 2 ** attempt
            log.warning(
                f"  oEmbed HTTP {resp.status_code} for {video_id} — retry in {wait}s"
            )
            time.sleep(wait)
            continue
        # Unexpected status — be conservative and keep.
        log.warning(f"  oEmbed HTTP {resp.status_code} for {video_id} — keeping")
        result = True
        break
    cache[video_id] = result
    return result


def data_files() -> list[Path]:
    """All per-season highlight JSON files (highlights/{slug}/{season}/{stem}.json),
    excluding the top-level summary.json."""
    return sorted(
        p for p in HIGHLIGHTS_DIR.glob("*/*/*.json") if p.name != "summary.json"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Audit only; do not write any files.")
    ap.add_argument("--sleep", type=float, default=0.1,
                    help="Seconds between unique oEmbed checks (politeness).")
    args = ap.parse_args()

    # Match/title text and the summary glyphs may be non-ASCII; force UTF-8 so a
    # cp1252 console (Windows) doesn't crash on an accented name or a symbol.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if not HIGHLIGHTS_DIR.exists():
        log.error(f"Highlights directory not found: {HIGHLIGHTS_DIR}")
        sys.exit(1)

    files = data_files()
    if not files:
        log.info("No highlight data files found — nothing to do.")
        return

    session = requests.Session()
    embed_cache: dict[str, bool] = {}
    total_removed = 0
    changed_files: list[Path] = []
    checked = 0

    print("=" * 72)
    print("Prune embedding-disabled clips (keyless - YouTube oEmbed, 0 API quota)")
    print("Rule: drop non-embeddable clips only when the match keeps >=1 embeddable")
    print(f"Mode: {'DRY RUN (no writes)' if args.dry_run else 'APPLY'}")
    print("=" * 72)

    for path in files:
        data = load_json_file(path)
        if not data or "matches" not in data:
            continue

        file_changed = False
        rel = path.relative_to(HIGHLIGHTS_DIR)

        for match in data.get("matches", []):
            videos = match.get("videos", []) or []
            if len(videos) < 2:
                # A single clip is never dropped (see rule); nothing to compare.
                # Still, count its check lazily below only when there are ≥2.
                continue

            # Probe embeddability for each clip (cached).
            flags = []
            for v in videos:
                vid = v.get("video_id", "")
                if not vid:
                    flags.append(True)  # malformed — keep
                    continue
                if vid not in embed_cache:
                    checked += 1
                    ok = is_embeddable(vid, session, embed_cache)
                    if args.sleep:
                        time.sleep(args.sleep)
                else:
                    ok = embed_cache[vid]
                flags.append(ok)

            has_embeddable = any(flags)
            has_blocked = not all(flags)
            if not (has_embeddable and has_blocked):
                continue  # all-good or all-blocked → leave untouched

            kept = [v for v, ok in zip(videos, flags) if ok]
            removed = [v for v, ok in zip(videos, flags) if not ok]

            home = match.get("home_team", "?")
            away = match.get("away_team", "?")
            date = match.get("date", "?")
            print(f"\n{rel} — {home} vs {away} ({date}):")
            for v in removed:
                print(f"  REMOVE  [{v.get('video_id')}]  \"{v.get('title', '')}\"")
            for v in kept:
                print(f"  keep    [{v.get('video_id')}]  \"{v.get('title', '')}\"")

            match["videos"] = kept
            file_changed = True
            total_removed += len(removed)

        if file_changed and not args.dry_run:
            data["generated_at"] = utc_now_iso()
            write_json_atomic(path, data)
            changed_files.append(path)
        elif file_changed:
            changed_files.append(path)  # would-change, for the dry-run tally

    print()
    print("=" * 72)
    print("Summary")
    print("=" * 72)
    print(f"  Unique videos probed (oEmbed):   {checked}")
    print(f"  Non-embeddable clips removed:    {total_removed}")
    print(f"  Files {'that would change' if args.dry_run else 'rewritten'}:       "
          f"{len(changed_files)}")
    print("=" * 72)

    if changed_files and not args.dry_run:
        generate_summary()
        log.info(f"summary.json regenerated ({len(changed_files)} file(s) changed)")


if __name__ == "__main__":
    main()
