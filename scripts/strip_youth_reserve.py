#!/usr/bin/env python3
"""
strip_youth_reserve.py — remove youth / reserve / academy clips wrongly linked
to senior fixtures.

Youth clips (e.g. "Leeds United U21 2-2 Brighton U21", "Fulham 1-2 Nottingham
Forest B-Team", "Monaco 3-5 Man City - Youth League") name both senior clubs and
even the senior competition ("Premier League 2"), so the matcher linked them to
the senior fixture. highlights_common.is_youth_reserve_title (the same rule the
fetch pipeline now applies on every tier) identifies them; this one-off pass
strips them from EXISTING data. Matches keep any legitimate senior clip; matches
left with none are re-resolved by the scheduled fetch (now youth-aware).

Zero API calls / zero quota. Reads and atomically rewrites JSON only.
Run with --dry-run to audit without writing.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from highlights_common import (
    HIGHLIGHTS_DIR,
    generate_summary,
    is_youth_reserve_title,
    load_json_file,
    utc_now_iso,
    write_json_atomic,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


def data_files() -> list[Path]:
    return sorted(
        p for p in HIGHLIGHTS_DIR.glob("*/*/*.json") if p.name != "summary.json"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Audit only; do not write any files.")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if not HIGHLIGHTS_DIR.exists():
        log.error(f"Highlights directory not found: {HIGHLIGHTS_DIR}")
        sys.exit(1)

    total_removed = 0
    emptied: list[str] = []
    changed_files: list[Path] = []

    print("=" * 72)
    print("Strip youth / reserve / academy clips (keyless, 0 API quota)")
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
            kept = [v for v in videos if not is_youth_reserve_title(v.get("title", ""))]
            removed = [v for v in videos if is_youth_reserve_title(v.get("title", ""))]
            if not removed:
                continue

            home = match.get("home_team", "?")
            away = match.get("away_team", "?")
            print(f"\n{rel} — {home} vs {away} ({match.get('date', '?')}):")
            for v in removed:
                print(f"  REMOVE  [{v.get('video_id')}]  \"{v.get('title', '')}\"")
            for v in kept:
                print(f"  keep    [{v.get('video_id')}]  \"{v.get('title', '')}\"")
            if not kept:
                emptied.append(f"{rel}: {home} vs {away}")
                print("  >> fixture now has no videos (will re-resolve on next fetch)")

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
    print(f"  Youth/reserve clips removed:   {total_removed}")
    print(f"  Files {'that would change' if args.dry_run else 'rewritten'}:     "
          f"{len(changed_files)}")
    if emptied:
        print(f"  Fixtures now with no videos ({len(emptied)}):")
        for e in emptied:
            print(f"    - {e}")
    print("=" * 72)

    if changed_files and not args.dry_run:
        generate_summary()
        log.info(f"summary.json regenerated ({len(changed_files)} file(s) changed)")


if __name__ == "__main__":
    main()
