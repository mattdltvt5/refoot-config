#!/usr/bin/env python3
"""
quality_recheck_tier2.py — Clear tier-2 (competition-channel) videos that fail
the quality thresholds introduced in commit 5879927.

Tier 2 is the broadest channel-based search (the LaLiga / Premier League /
Serie A / etc. official uploads feed).  It is the most likely source of
vertical social clips and sub-2-minute highlights-adjacent content.

For each stored match resolved via tier 2 the script calls YouTube's
videos.list API to verify:
  • duration ≥ 120 s  (MIN_VIDEO_DURATION_SECONDS)
  • thumbnail is landscape (width ≥ height)

If any video in a match fails either check the whole match's videos list is
cleared to [] so the next fetch-highlights run re-resolves it fresh — with the
quality filters now in place.

Run via the quality-recheck-tier2 GitHub Actions workflow (workflow_dispatch).
Optional env var:  COMPETITION_FILTER  — restrict to one competition by name.

Safe to re-run: if no tier-2 videos fail quality checks nothing is changed.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from highlights_common import (
    COMPETITION_SLUG_MAP,
    HIGHLIGHTS_DIR,
    MIN_VIDEO_DURATION_SECONDS,
    QUOTA_TRACKER_PATH,
    QuotaCapReached,
    QuotaTracker,
    fetch_video_details,
    generate_summary,
    load_json_file,
    utc_now_iso,
    write_json_atomic,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# Generous cap — the audit itself uses very few units (1 per 50 video IDs).
_QUOTA_CAP = 9_000


def main() -> None:
    yt_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not yt_key:
        log.error("YOUTUBE_API_KEY is not set")
        sys.exit(1)

    comp_filter = os.environ.get("COMPETITION_FILTER", "").strip() or None
    if comp_filter:
        log.info(f"Competition filter active: only checking {comp_filter!r}")

    quota = QuotaTracker()
    changed_files: list[Path] = []
    total_cleared = 0

    for comp_name, slug in COMPETITION_SLUG_MAP.items():
        if comp_filter and comp_name != comp_filter:
            continue

        comp_dir = HIGHLIGHTS_DIR / slug
        if not comp_dir.exists():
            continue

        json_files = sorted(comp_dir.glob("*.json"))
        tier2_files = 0

        for path in json_files:
            if path.name == "summary.json":
                continue

            data = load_json_file(path)
            if not data:
                continue

            matches = data.get("matches", [])

            # Identify matches whose entire video set came from tier 2.
            # (All videos for one match always share the same tier because
            # resolve_videos_for_fixture() stops at the first successful tier.)
            tier2_matches: list[tuple[int, list[str]]] = []
            for i, match in enumerate(matches):
                videos = match.get("videos", [])
                if videos and all(v.get("tier_used") == 2 for v in videos):
                    tier2_matches.append((i, [v["video_id"] for v in videos]))

            if not tier2_matches:
                continue

            tier2_files += 1
            all_ids = [vid_id for _, ids in tier2_matches for vid_id in ids]
            log.info(
                f"  {comp_name}/{path.name}: "
                f"{len(tier2_matches)} tier-2 match(es), "
                f"{len(all_ids)} video(s) to check"
            )

            # Batch-fetch video details (1 quota unit per 50 IDs).
            details: dict = {}
            try:
                for start in range(0, len(all_ids), 50):
                    details.update(
                        fetch_video_details(all_ids[start:start + 50], yt_key, quota, _QUOTA_CAP)
                    )
            except QuotaCapReached:
                log.warning("Daily quota cap reached — stopping early. Re-run tomorrow.")
                break

            # Evaluate each tier-2 match.
            any_changed = False
            for i, video_ids in tier2_matches:
                match = matches[i]
                failing: list[str] = []

                for vid_id in video_ids:
                    d = details.get(vid_id)
                    if d is None:
                        continue  # could not fetch — leave untouched
                    if d["duration_seconds"] < MIN_VIDEO_DURATION_SECONDS:
                        failing.append(
                            f"{vid_id} ({d['duration_seconds']}s "
                            f"< {MIN_VIDEO_DURATION_SECONDS}s)"
                        )
                    elif d["is_portrait"]:
                        failing.append(f"{vid_id} (portrait thumbnail)")

                if failing:
                    log.info(
                        f"    Clearing {match['home_team']} vs {match['away_team']} "
                        f"({match['date']}): {', '.join(failing)}"
                    )
                    matches[i] = {**match, "videos": []}
                    any_changed = True
                    total_cleared += 1
                else:
                    log.info(
                        f"    OK  {match['home_team']} vs {match['away_team']} "
                        f"({match['date']})"
                    )

            if any_changed:
                data["generated_at"] = utc_now_iso()
                write_json_atomic(path, data)
                changed_files.append(path)
                log.info(f"    → Updated {path.name}")

        if tier2_files:
            log.info(f"── {comp_name}: {tier2_files} file(s) with tier-2 matches checked")

    log.info(
        f"\nAudit complete: {total_cleared} match(es) cleared across "
        f"{len(changed_files)} file(s)."
    )
    if total_cleared:
        log.info(
            "Cleared matches will be re-resolved (with quality filters) "
            "on the next fetch-highlights run."
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
                 f"chore: quality-recheck tier-2 — {total_cleared} match(es) cleared [skip ci]"],
                check=True,
            )
            subprocess.run(["git", "pull", "--rebase"], check=True)
            subprocess.run(["git", "push"], check=True)
            log.info("Committed and pushed.")
            log.info(f"Quota used: {quota.units_used} unit(s).")
    except subprocess.CalledProcessError as exc:
        log.error(f"Git operation failed: {exc} — data written locally")


if __name__ == "__main__":
    main()
