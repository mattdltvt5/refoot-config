#!/usr/bin/env python3
"""
One-time cleanup script — removes false-positive videos from existing
gameweek JSON files using the multilingual title filter from highlights_common.py.

Run via the clean-highlights GitHub Actions workflow (workflow_dispatch).
Safe to re-run after any future filter update.
"""
import logging
import subprocess
from pathlib import Path

from highlights_common import (
    HIGHLIGHTS_DIR,
    COMPETITION_FILE_STEMS,
    COMPETITION_SLUG_MAP,
    is_highlight_title,
    load_json_file,
    write_json_atomic,
    generate_summary,
    utc_now_iso,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def clean_gameweek_file(path: Path) -> tuple[int, int]:
    """
    Remove false-positive videos from a single gameweek JSON file.

    Returns (videos_removed, matches_affected).
    The file is rewritten atomically only when at least one video is removed.
    """
    data = load_json_file(path)
    if not data:
        return 0, 0

    videos_removed   = 0
    matches_affected = 0

    for match in data.get("matches", []):
        original = match.get("videos", [])
        cleaned  = [v for v in original if is_highlight_title(v.get("title", ""))]
        removed  = len(original) - len(cleaned)
        if removed > 0:
            match["videos"]   = cleaned
            videos_removed   += removed
            matches_affected += 1
            log.info(
                f"  {match['home_team']} vs {match['away_team']}: "
                f"removed {removed} false-positive(s)"
            )

    if videos_removed > 0:
        data["generated_at"] = utc_now_iso()
        write_json_atomic(path, data)

    return videos_removed, matches_affected


def main() -> None:
    total_videos_removed   = 0
    total_matches_affected = 0
    changed_files: list[Path] = []

    for comp_name, slug in COMPETITION_SLUG_MAP.items():
        comp_dir = HIGHLIGHTS_DIR / slug
        if not comp_dir.exists():
            continue

        stems = COMPETITION_FILE_STEMS.get(comp_name, [])
        files = [comp_dir / f"{stem}.json" for stem in stems
                 if (comp_dir / f"{stem}.json").exists()]
        if not files:
            continue

        log.info(f"── {comp_name} ({len(files)} file(s)) ──")
        for f in files:
            removed, affected = clean_gameweek_file(f)
            total_videos_removed   += removed
            total_matches_affected += affected
            if removed > 0:
                changed_files.append(f)

    log.info(
        f"\nCleanup complete: {total_videos_removed} video(s) removed "
        f"from {total_matches_affected} match(es) across "
        f"{len(changed_files)} file(s)."
    )

    generate_summary()

    if not changed_files:
        log.info("No files changed — nothing to commit.")
        return

    changed_files.append(HIGHLIGHTS_DIR / "summary.json")

    try:
        subprocess.run(
            ["git", "config", "user.email",
             "github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            check=True,
        )
        subprocess.run(
            ["git", "add"] + [str(f) for f in changed_files],
            check=True,
        )
        result = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if result.returncode == 0:
            log.info("No staged changes — skipping commit.")
        else:
            subprocess.run(
                ["git", "commit", "-m",
                 "chore: clean false-positive highlights [skip ci]"],
                check=True,
            )
            subprocess.run(["git", "pull", "--rebase"], check=True)
            subprocess.run(["git", "push"], check=True)
            log.info("Committed and pushed.")
    except subprocess.CalledProcessError as e:
        log.error(f"Git operation failed: {e} — data written locally")


if __name__ == "__main__":
    main()
