#!/usr/bin/env python3
"""
scripts/backfill_euro_cup.py -- Euro Cup 2024 highlights backfill.

Manual trigger only (workflow_dispatch via backfill-euro-cup.yml).
Never wired into the scheduled 4-hour fetch Action.

Fetches all Euro Cup 2024 fixtures from API-Sports (free tier), then
runs the same YouTube tier-search as the main backfill to find highlight videos.
Reports per-fixture match success vs. "fixture OK but no video found" so you
can assess Euro Cup highlight coverage on YouTube.

Environment variables:
    APISPORTS_API_KEY   -- API-Sports key (100 req/day free tier)
    YOUTUBE_API_KEY     -- YouTube Data API v3 key

Security: APISPORTS_API_KEY must come from a GitHub Actions secret -- never hardcode it.
"""

import logging
import os
import subprocess
import sys

from fixture_providers import (
    APISPORTS_COMPETITIONS,
    APISPORTS_QUOTA_PATH,
    ApiSportsProvider,
    ApisportsQuotaTracker,
)
from highlights_common import (
    BACKFILL_CAP,
    COMPETITION_FILE_STEMS,
    FILE_STEM_LABEL,
    HIGHLIGHTS_DIR,
    QuotaCapReached,
    generate_summary,
    gw_path,
    is_same_tournament_edition,
    load_json_file,
    load_sources,
    merge_into_gw,
    resolve_videos_for_fixture,
    utc_now_iso,
    write_json_atomic,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

COMP_NAME = "Euro Cup"


# ── In-memory YouTube quota ───────────────────────────────────────────────────
#
# Euro Cup backfill is a manual one-shot run against a completed tournament.
# Its YouTube usage must NOT be added to highlights/quota-tracker.json to avoid
# skewing the incremental job's budget check on the same day.


class _InMemoryYtQuota:
    """YouTube quota stub that never reads or writes quota-tracker.json."""

    def __init__(self) -> None:
        self.units_used = 0

    def increment(self, cap: int) -> None:
        self.units_used += 1
        if self.units_used >= cap:
            raise QuotaCapReached(
                f"YouTube cap reached during Euro Cup backfill: "
                f"{self.units_used}/{cap}"
            )

    @property
    def over_incremental_cap(self) -> bool:
        return False


# ── Git helper ────────────────────────────────────────────────────────────────


def _git_commit(files: list, message: str) -> None:
    to_stage = [str(f) for f in files]
    if not to_stage:
        return
    try:
        subprocess.run(["git", "add", "--"] + to_stage, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            log.info("Git: nothing new to commit -- skipping")
            return
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push"], check=True)
        log.info(f"Git: committed and pushed -- {message!r}")
    except subprocess.CalledProcessError as exc:
        log.error(
            f"Git operation failed ({exc.cmd}): {exc} -- "
            "data is written locally; next commit will include it"
        )


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    yt_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not yt_key:
        log.error("YOUTUBE_API_KEY is not set")
        sys.exit(1)

    subprocess.run(
        ["git", "config", "user.email",
         "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        check=True,
    )

    # ── Fetch fixtures from API-Sports ────────────────────────────────────────
    as_quota    = ApisportsQuotaTracker()
    provider    = ApiSportsProvider(quota_tracker=as_quota)
    comp_config = APISPORTS_COMPETITIONS[COMP_NAME]

    log.info(
        f"Fetching {COMP_NAME} {comp_config['season']} fixtures from API-Sports..."
    )
    by_stem = provider.get_fixtures(COMP_NAME, comp_config)
    if not by_stem:
        log.error("API-Sports returned no fixtures -- aborting")
        sys.exit(1)

    total_fixtures = sum(len(v) for v in by_stem.values())
    log.info(
        f"{COMP_NAME}: {total_fixtures} fixture(s) across {len(by_stem)} stage(s)"
    )

    # ── YouTube resolution ────────────────────────────────────────────────────
    config          = load_sources()
    yt_quota        = _InMemoryYtQuota()
    all_stems       = COMPETITION_FILE_STEMS.get(COMP_NAME, [])
    written_files   = []
    gw_playlist_cache = {}

    fetched_total = 0
    matched_total = 0
    no_video_total = 0

    ordered_stems = [s for s in all_stems if s in by_stem]
    extra_stems   = [s for s in by_stem  if s not in all_stems]

    for stem in ordered_stems + extra_stems:
        fixtures = by_stem[stem]
        path     = gw_path(COMP_NAME, stem)
        existing = load_json_file(path)
        label    = FILE_STEM_LABEL.get(stem, stem)

        if not is_same_tournament_edition(existing, fixtures, COMP_NAME):
            existing = None

        existing_by_id = {
            m["match_id"]: m for m in (existing or {}).get("matches", [])
        }

        log.info(f"  {COMP_NAME} {label} ({len(fixtures)} fixture(s))...")
        enriched = []

        for fix in fixtures:
            fetched_total += 1
            prior = existing_by_id.get(fix["match_id"])
            if prior and prior.get("videos"):
                enriched.append({**fix, "videos": prior["videos"]})
                matched_total += 1
                continue

            try:
                videos = resolve_videos_for_fixture(
                    fix, COMP_NAME, config, yt_key, yt_quota, BACKFILL_CAP,
                    gw_playlist_cache=gw_playlist_cache,
                )
            except QuotaCapReached as exc:
                log.info(f"YouTube cap reached: {exc}")
                if enriched:
                    gw_data, changed = merge_into_gw(existing, COMP_NAME, stem, enriched)
                    if changed:
                        write_json_atomic(path, gw_data)
                        written_files.append(path)
                _finish(fetched_total, matched_total, no_video_total, written_files, as_quota)
                return

            if videos:
                matched_total += 1
                tiers = sorted({v["tier_used"] for v in videos})
                log.info(
                    f"    + {fix['home_team']} vs {fix['away_team']}: "
                    f"{len(videos)} video(s) via tier(s) {tiers}"
                )
            else:
                no_video_total += 1
                log.warning(
                    f"    - No highlights: {fix['home_team']} vs "
                    f"{fix['away_team']} ({fix['date']})"
                )

            enriched.append({**fix, "videos": videos})

        if enriched:
            gw_data, changed = merge_into_gw(existing, COMP_NAME, stem, enriched)
            if changed:
                write_json_atomic(path, gw_data)
                written_files.append(path)
                log.info(f"    -> Wrote {path.name}")

    _finish(fetched_total, matched_total, no_video_total, written_files, as_quota)


def _finish(
    fetched: int,
    matched: int,
    no_video: int,
    written_files: list,
    as_quota: ApisportsQuotaTracker,
) -> None:
    log.info("=" * 60)
    log.info("Euro Cup 2024 backfill complete")
    log.info(f"  Fixtures fetched from API-Sports : {fetched}")
    log.info(f"  YouTube highlight found          : {matched}")
    log.info(f"  Fixture OK, no video found       : {no_video}")
    log.info(f"  API-Sports calls used today      : {as_quota.calls_used}")
    log.info(f"  YouTube units used               : (isolated -- not tracked in quota-tracker.json)")
    log.info("=" * 60)

    if not written_files:
        log.info("No new data -- nothing to commit")
        generate_summary()
        return

    commit_files = list(written_files) + [APISPORTS_QUOTA_PATH]
    _git_commit(
        commit_files,
        "chore: backfill Euro Cup 2024 highlights [skip ci]",
    )

    generate_summary()
    summary_path = HIGHLIGHTS_DIR / "summary.json"
    _git_commit([summary_path], "chore: update summary.json [skip ci]")


if __name__ == "__main__":
    main()
