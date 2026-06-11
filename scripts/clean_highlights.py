#!/usr/bin/env python3
"""
clean_highlights.py — Source-scoped LaLiga highlight cleanup.

Removes videos from the LaLiga highlights cache that were fetched from
the official LaLiga competition channel (tier_used == 2) and do not
contain 'HIGHLIGHTS LALIGA' in their title.

Provenance: each cached video records ``tier_used`` (1, 2, or 4).
``tier_used == 2`` identifies videos sourced from the LaLiga channel —
either via per-matchday playlists (tier 2a) or the channel uploads feed
(tier 2b).  Both sub-sources apply the same strict title gate.

Videos from team-channel playlists (tier 1) or broadcaster playlists
(tier 4) are never touched by this pass, regardless of title content.

Zero API calls.  Reads and atomically rewrites JSON files only.
Prints a per-gameweek audit summary before writing any changes.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from highlights_common import (
    HIGHLIGHTS_DIR,
    generate_summary,
    is_laliga_highlight_title,
    load_json_file,
    utc_now_iso,
    write_json_atomic,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

LALIGA_DIR = HIGHLIGHTS_DIR / "laliga"


def main() -> None:
    if not LALIGA_DIR.exists():
        log.error(f"LaLiga highlights directory not found: {LALIGA_DIR}")
        sys.exit(1)

    gw_files = sorted(
        LALIGA_DIR.glob("gameweek-*.json"),
        key=lambda p: int(p.stem.split("-")[1]),
    )
    if not gw_files:
        log.info("No LaLiga gameweek files found — nothing to do.")
        generate_summary()
        return

    total_removed = 0
    total_other_source = 0  # tier != 2 — untouched, shown for auditability
    empty_after: list[str] = []
    changed_files: list[Path] = []

    print("=" * 70)
    print("LaLiga source-scoped cleanup")
    print("Scope: tier_used == 2 (LaLiga competition channel)")
    print("Gate:  title must contain 'highlights laliga' (case-insensitive)")
    print("Other sources (tier 1, tier 4): untouched regardless of title")
    print("=" * 70)

    for gw_file in gw_files:
        data = load_json_file(gw_file)
        if not data:
            continue

        file_changed = False
        gw_label = data.get("gameweek", gw_file.stem)

        for match in data.get("matches", []):
            home   = match.get("home_team", "?")
            away   = match.get("away_team", "?")
            date   = match.get("date", "?")
            videos = match.get("videos", [])

            kept:    list[dict] = []
            removed: list[dict] = []

            for v in videos:
                tier = v.get("tier_used")
                if tier != 2:
                    # Non-competition-channel source — never touched by this pass
                    total_other_source += 1
                    kept.append(v)
                    continue
                if is_laliga_highlight_title(v["title"]):
                    kept.append(v)
                else:
                    removed.append(v)

            if removed:
                print(f"\nGW{gw_label} — {home} vs {away} ({date}):")
                for v in removed:
                    print(
                        f"  REMOVE  tier={v.get('tier_used')}  "
                        f"[{v['video_id']}]  \"{v['title']}\""
                    )
                if not kept:
                    empty_after.append(f"GW{gw_label}: {home} vs {away}")
                    print(f"  >> fixture now has no videos (degraded state)")
                match["videos"] = kept
                file_changed = True
                total_removed += len(removed)

        if file_changed:
            data["generated_at"] = utc_now_iso()
            write_json_atomic(gw_file, data)
            changed_files.append(gw_file)

    print()
    print("=" * 70)
    print("Cleanup summary")
    print("=" * 70)
    print(f"  Files rewritten:                                 {len(changed_files)}")
    print(f"  Videos removed (tier 2, failed gate):           {total_removed}")
    print(f"  Videos untouched (tier 1 / tier 4 — non-LaLiga-channel): {total_other_source}")
    if empty_after:
        print(f"  Fixtures with no videos after cleanup ({len(empty_after)}):")
        for label in empty_after:
            print(f"    - {label}")
    else:
        print(f"  Fixtures with no videos after cleanup:           0")
    print("=" * 70)

    generate_summary()
    log.info(f"summary.json regenerated ({len(changed_files)} file(s) changed)")


if __name__ == "__main__":
    main()
