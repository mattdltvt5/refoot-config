#!/usr/bin/env python3
"""
backfill_highlights.py — Full-season highlights backfill.

Manual trigger only (workflow_dispatch via backfill-highlights.yml).
Fetches all FINISHED fixtures for the current football season from
football-data.org and searches YouTube playlists in tier-priority order.

Progress is checkpointed after every gameweek and committed to git after
every competition, so re-triggering always resumes from the correct position.

Exits cleanly (exit 0) when:
  - The 9,500-unit daily YouTube cap is reached (BACKFILL_CAP)
  - YouTube returns HTTP 403 (its own quota exhaustion)
  - The entire season is already complete

Environment variables required:
    FOOTBALL_DATA_API_KEY   — football-data.org personal access token
    YOUTUBE_API_KEY         — YouTube Data API v3 key
"""

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from highlights_common import (
    BACKFILL_CAP,
    BACKFILL_LOCK_PATH,
    BACKFILL_PROGRESS_PATH,
    COMPETITION_CODE_MAP,
    COMPETITION_FILE_STEMS,
    COMPETITION_SLUG_MAP,
    FILE_STEM_LABEL,
    FD_BASE,
    FD_SLEEP_SECONDS,
    HIGHLIGHTS_DIR,
    QUOTA_TRACKER_PATH,
    QuotaCapReached,
    QuotaTracker,
    STAGE_AWARE_COMPS,
    current_season,
    season_for_competition,
    fd_get,
    generate_summary,
    gw_path,
    load_json_file,
    load_sources,
    is_same_tournament_edition,
    merge_into_gw,
    resolve_videos_for_fixture,
    stage_to_file_stem,
    utc_now_iso,
    write_json_atomic,
)
from fixture_providers import FootballDataProvider

log = logging.getLogger(__name__)


# ── Git helper ────────────────────────────────────────────────────────────────


def run_git_commit(files: list[Path], message: str) -> None:
    """
    Stage the given files, commit, and push.

    - backfill.lock is silently excluded (must never be committed)
    - If nothing is staged after git add, the commit is skipped silently
    - If git push fails (e.g. remote conflict), the error is logged but the
      script continues — data is safe on disk and the next commit will include it
    """
    to_stage = [str(f) for f in files if f != BACKFILL_LOCK_PATH]
    if not to_stage:
        return
    try:
        subprocess.run(["git", "add", "--"] + to_stage, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            log.info("Git: nothing new to commit — skipping")
            return
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push"], check=True)
        log.info(f"Git: committed and pushed — {message!r}")
    except subprocess.CalledProcessError as exc:
        log.error(
            f"Git operation failed ({exc.cmd}): {exc} — "
            "data is written locally; next commit will include it"
        )



# ── Progress tracker ──────────────────────────────────────────────────────────


class BackfillProgress:
    """
    Persists backfill checkpoint state to highlights/backfill-progress.json.
    Automatically resets when the season changes so each season starts fresh.
    """

    def __init__(self, season: int) -> None:
        data = load_json_file(BACKFILL_PROGRESS_PATH) or {}
        if data.get("season") != season:
            log.info(
                f"Season changed ({data.get('season')} → {season}) — resetting backfill progress"
            )
            data = {}

        self.season                     = season
        self.status                     = data.get("status", "in_progress")
        self.last_completed_competition = data.get("last_completed_competition")
        self.last_completed_file_stem   = data.get("last_completed_file_stem") or None
        self.competitions_done          = list(data.get("competitions_done", []))
        self.started_at                 = data.get("started_at") or utc_now_iso()
        self.last_resumed_at            = utc_now_iso()
        self._save()

    def _save(self) -> None:
        write_json_atomic(BACKFILL_PROGRESS_PATH, {
            "status":                     self.status,
            "season":                     self.season,
            "last_completed_competition": self.last_completed_competition,
            "last_completed_file_stem":   self.last_completed_file_stem,
            "competitions_done":          self.competitions_done,
            "started_at":                 self.started_at,
            "last_resumed_at":            self.last_resumed_at,
        })
        log.debug(
            f"Checkpoint saved: competitions_done={self.competitions_done}, "
            f"last_stem={self.last_completed_file_stem}"
        )

    def mark_file_stem_done(self, comp_name: str, stem: str) -> None:
        self.last_completed_competition = comp_name
        self.last_completed_file_stem   = stem
        self._save()

    def mark_competition_done(self, comp_name: str) -> None:
        if comp_name not in self.competitions_done:
            self.competitions_done.append(comp_name)
        self.last_completed_competition = comp_name
        self.last_completed_file_stem   = None   # reset per-competition cursor
        self._save()

    def mark_complete(self) -> None:
        self.status = "complete"
        self._save()


# ── Fixture fetching ──────────────────────────────────────────────────────────


def fetch_season_fixtures(
    code: str,
    comp_name: str,
    season: int,
    fd_key: str,
) -> dict[str, list[dict]]:
    """
    Fetch all FINISHED fixtures for a competition and season via FootballDataProvider.
    Returns: {file_stem: [fixture_dict, ...]}
    """
    return FootballDataProvider(fd_key).get_fixtures(code, comp_name, season)


# ── Tier-4 re-check helpers ───────────────────────────────────────────────────


def _stem_to_stage(stem: str, comp_name: str) -> tuple[int | None, str]:
    """
    Infer (matchday, stage) from a file stem for use in fixture reconstruction.

    Domestic leagues are never STAGE_AWARE_COMPS, so their stage value is
    irrelevant to resolution logic — we use "REGULAR_SEASON" as a safe default.

    For stage-aware competitions (UCL/UEL/Euro Cup/World Cup) we distinguish
    league-stage files (stem ends in a digit, implying a matchday) from
    knockout files (stage unknown → empty string, which gracefully skips GW
    playlist discovery without breaking anything else).
    """
    import re as _re
    m = _re.search(r"(\d+)$", stem)
    matchday = int(m.group(1)) if m else None

    if comp_name not in STAGE_AWARE_COMPS:
        stage = "REGULAR_SEASON"
    elif matchday is not None:
        stage = "LEAGUE_STAGE"
    else:
        stage = ""   # knockout round — GW playlist discovery will be skipped gracefully

    return matchday, stage


def tier4_recheck_mode(comp_name: str, yt_key: str, config: dict) -> None:
    """
    Re-check every stored match for *comp_name* that either has no videos yet
    or whose videos were all sourced from tier 4 (broadcaster playlists).

    Resolution is re-run from scratch using the full tier hierarchy, so a
    higher-priority source can win.  Empty fixtures are filled in if a match
    is now found.  Files are only updated when the resulting video-ID set
    actually changes.  A single git commit is made at the end.
    """
    slug = COMPETITION_SLUG_MAP.get(comp_name)
    if not slug:
        log.error(f"Unknown competition for tier-4 re-check: {comp_name!r}")
        sys.exit(1)

    comp_dir = HIGHLIGHTS_DIR / slug
    if not comp_dir.exists():
        log.info(f"No highlights directory for {comp_name} — nothing to re-check")
        return

    quota            = QuotaTracker()
    gw_playlist_cache: dict = {}
    changed_files: list[Path] = []

    json_files = sorted(comp_dir.glob("*.json"))
    log.info(f"Tier-4 re-check for {comp_name}: scanning {len(json_files)} file(s)…")

    for path in json_files:
        if path.name in ("summary.json",):
            continue

        stem = path.stem
        data = load_json_file(path)
        if not data:
            continue

        matchday, stage = _stem_to_stage(stem, comp_name)
        matches   = data.get("matches", [])
        any_changed = False

        for i, match in enumerate(matches):
            videos = match.get("videos", [])
            # Process matches with no videos yet (empty fixtures) and matches
            # where every stored video came from tier 4.  Skip matches that
            # already have a higher-tier result — those are considered settled.
            # (All videos for a single match always share the same tier because
            # resolve_videos_for_fixture() stops at the first successful tier.)
            if videos and not all(v.get("tier_used") == 4 for v in videos):
                continue

            # Reconstruct the fixture dict that resolve_videos_for_fixture() expects.
            # home_short/away_short/home_tla/away_tla are not stored in the JSON —
            # fall back gracefully (tla="" is safe; team_tokens handles it).
            fix = {
                "match_id":   match["match_id"],
                "home_team":  match["home_team"],
                "home_short": match.get("home_short", match["home_team"]),
                "home_tla":   match.get("home_tla", ""),
                "away_team":  match["away_team"],
                "away_short": match.get("away_short", match["away_team"]),
                "away_tla":   match.get("away_tla", ""),
                "date":       match["date"],
                "matchday":   matchday,
                "stage":      stage,
            }

            try:
                new_videos = resolve_videos_for_fixture(
                    fix, comp_name, config, yt_key, quota, BACKFILL_CAP,
                    gw_playlist_cache=gw_playlist_cache,
                )
            except QuotaCapReached as exc:
                log.info(f"Quota cap reached during tier-4 re-check: {exc}")
                if any_changed:
                    write_json_atomic(path, data)
                    changed_files.append(path)
                break   # exit inner match loop; outer file loop exits at next iteration

            old_ids = {v["video_id"] for v in videos}
            new_ids = {v["video_id"] for v in new_videos} if new_videos else set()

            if new_ids and new_ids != old_ids:
                log.info(
                    f"  ✓ Replacing tier-4 result for "
                    f"{match['home_team']} vs {match['away_team']} ({match['date']}): "
                    f"{old_ids} → {new_ids}"
                )
                matches[i] = {**match, "videos": new_videos}
                any_changed = True
            else:
                tiers = sorted({v["tier_used"] for v in new_videos}) if new_videos else []
                log.debug(
                    f"  — No change for {match['home_team']} vs {match['away_team']}"
                    + (f" (tier(s) {tiers})" if tiers else " (still no result)")
                )

        if any_changed:
            write_json_atomic(path, data)
            changed_files.append(path)
            log.info(f"  → Updated {path.name}")

    if changed_files:
        run_git_commit(
            changed_files + [QUOTA_TRACKER_PATH],
            f"chore: tier-4 recheck {slug} [skip ci]",
        )
        log.info(f"Tier-4 re-check complete. {len(changed_files)} file(s) updated.")
    else:
        log.info(f"Tier-4 re-check complete. No changes needed for {comp_name}.")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    fd_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    yt_key = os.environ.get("YOUTUBE_API_KEY", "").strip()

    # ── Optional overrides from workflow_dispatch inputs ──────────────────────
    # SEASON_OVERRIDE: force a specific season year for all (or the filtered)
    #   competition, e.g. "2024" to backfill Euro 2024 or WC 2022.
    # COMPETITION_FILTER: restrict the run to one competition by exact name,
    #   e.g. "Euro Cup" or "World Cup".  Useful for targeted historical runs.
    season_override_str  = os.environ.get("SEASON_OVERRIDE", "").strip()
    competition_filter   = os.environ.get("COMPETITION_FILTER", "").strip() or None
    tier4_recheck        = os.environ.get("TIER4_RECHECK", "").strip().lower() == "true"

    if not tier4_recheck and not fd_key:
        log.error("FOOTBALL_DATA_API_KEY is not set")
        sys.exit(1)
    if not yt_key:
        log.error("YOUTUBE_API_KEY is not set — backfill requires YouTube access")
        sys.exit(1)

    season_override: int | None = None
    if season_override_str:
        try:
            season_override = int(season_override_str)
            log.info(f"Season override active: will query season={season_override} for all competitions")
        except ValueError:
            log.error(f"SEASON_OVERRIDE={season_override_str!r} is not a valid integer — ignoring")

    if competition_filter:
        log.info(f"Competition filter active: only processing {competition_filter!r}")

    season   = current_season()
    progress = BackfillProgress(season)

    # ── Startup log: always reflects the state loaded from disk ───────────────
    log.info(
        f"Backfill season {season} | "
        f"competitions done: {progress.competitions_done or '(none)'} | "
        f"resuming after {progress.last_completed_file_stem or '(start)'} in "
        f"{progress.last_completed_competition or 'start'}"
    )

    # When a season or competition override is active we skip the "already
    # complete" check so targeted historical runs always execute.
    if progress.status == "complete" and not season_override and not competition_filter:
        log.info(f"Season {season} backfill is already complete — nothing to do")
        generate_summary()
        return

    quota  = QuotaTracker()
    config = load_sources()

    # ── Tier-4 broadcaster re-check mode (triggered by admin tool after reorder) ──
    if tier4_recheck:
        if not competition_filter:
            log.error("TIER4_RECHECK requires COMPETITION_FILTER to be set")
            sys.exit(1)
        log.info(f"Tier-4 re-check mode: {competition_filter!r}")
        subprocess.run(
            ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=True)
        tier4_recheck_mode(competition_filter, yt_key, config)
        generate_summary()
        return

    BACKFILL_LOCK_PATH.write_text(utc_now_iso(), encoding="utf-8")

    # GitHub Actions runners have no default git identity — configure it now,
    # before run_git_commit() is called for the first time.
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        check=True,
    )
    log.info("Git identity configured")

    total_written  = 0
    quota_cap_hit  = False
    gw_playlist_cache: dict = {}  # shared across the run; avoids redundant playlists.list calls per GW
    # These track the current competition's context for the post-loop commit
    written_this_comp: list[Path] = []
    current_slug                  = ""

    try:
        for code, comp_name in COMPETITION_CODE_MAP.items():
            if quota_cap_hit:
                break

            # ── Competition filter ────────────────────────────────────────────
            if competition_filter and comp_name != competition_filter:
                continue

            slug = COMPETITION_SLUG_MAP[comp_name]

            # ── Resume check — skip if already done (unless override active) ─
            if comp_name in progress.competitions_done and not season_override and not competition_filter:
                log.info(f"INFO: {comp_name} already complete — skipping")
                continue

            # Season: explicit override wins; otherwise per-competition logic
            comp_season = season_override if season_override else season_for_competition(comp_name)
            log.info(f"── {comp_name} (season {comp_season}) ──")
            written_this_comp = []
            current_slug      = slug
            time.sleep(FD_SLEEP_SECONDS)

            by_stem = fetch_season_fixtures(code, comp_name, comp_season, fd_key)
            if not by_stem:
                progress.mark_competition_done(comp_name)
                run_git_commit(
                    [BACKFILL_PROGRESS_PATH, QUOTA_TRACKER_PATH],
                    f"chore: backfill {slug} complete [skip ci]",
                )
                continue

            # Determine which stem to resume from within this competition
            all_stems = COMPETITION_FILE_STEMS.get(comp_name, [])
            resume_after_stem = (
                progress.last_completed_file_stem
                if progress.last_completed_competition == comp_name
                else None
            )
            # Build a set of stems that precede or equal the resume point
            skippable: set[str] = set()
            if resume_after_stem and resume_after_stem in all_stems:
                skip_idx = all_stems.index(resume_after_stem)
                skippable = set(all_stems[:skip_idx + 1])

            # Iterate stems in canonical order (COMPETITION_FILE_STEMS defines order)
            # then process any stems not in the ordered list last
            ordered_stems = [s for s in all_stems if s in by_stem]
            extra_stems   = [s for s in by_stem if s not in all_stems]

            for stem in ordered_stems + extra_stems:
                if quota_cap_hit:
                    break

                if stem in skippable:
                    label = FILE_STEM_LABEL.get(stem, stem)
                    log.info(f"  {label}: already checkpointed — skipping")
                    continue

                fixtures = by_stem[stem]
                path     = gw_path(comp_name, stem, comp_season)
                existing = load_json_file(path)

                # Detect new tournament edition for non-annual competitions
                if not is_same_tournament_edition(existing, fixtures, comp_name):
                    existing = None

                # Build per-match lookup so already-covered fixtures are skipped
                existing_by_id: dict[int, dict] = {}
                if existing:
                    existing_by_id = {
                        m["match_id"]: m for m in existing.get("matches", [])
                    }

                label = FILE_STEM_LABEL.get(stem, stem)
                log.info(f"  {comp_name} {label} ({len(fixtures)} fixture(s))…")
                enriched: list[dict] = []

                for fix in fixtures:
                    prior = existing_by_id.get(fix["match_id"])
                    if prior and prior.get("videos"):
                        # Already covered in a previous run — preserve the videos
                        enriched.append({**fix, "videos": prior["videos"]})
                        continue

                    try:
                        videos = resolve_videos_for_fixture(
                            fix, comp_name, config, yt_key, quota, BACKFILL_CAP,
                            gw_playlist_cache=gw_playlist_cache,
                        )
                    except QuotaCapReached as exc:
                        # Raised for both internal cap hits AND YouTube 403 responses
                        log.info(f"  Cap/quota reached: {exc}")
                        quota_cap_hit = True
                        break

                    enriched.append({**fix, "videos": videos})

                    if not videos:
                        log.warning(
                            f"No highlights — {comp_name} {label}: "
                            f"{fix['home_team']} vs {fix['away_team']} ({fix['date']})"
                        )
                    else:
                        tiers = sorted({v["tier_used"] for v in videos})
                        log.info(
                            f"    ✓ {fix['home_team']} vs {fix['away_team']}: "
                            f"{len(videos)} video(s) via tier(s) {tiers}"
                        )

                # Write whatever we have for this stem
                # (full set when normal; partial set when quota was hit mid-stem)
                if enriched:
                    gw_data, changed = merge_into_gw(
                        existing, comp_name, stem, enriched
                    )
                    if changed:
                        write_json_atomic(path, gw_data)
                        total_written += 1
                        written_this_comp.append(path)
                        log.info(f"    → Wrote {path.name}")

                # Only advance the checkpoint when the full stem was processed
                if not quota_cap_hit:
                    progress.mark_file_stem_done(comp_name, stem)

            # ── Competition complete ───────────────────────────────────────────
            if not quota_cap_hit:
                progress.mark_competition_done(comp_name)
                # Step 2: commit after every competition so the checkpoint is
                # persisted in git before the next competition starts
                run_git_commit(
                    written_this_comp + [BACKFILL_PROGRESS_PATH, QUOTA_TRACKER_PATH],
                    f"chore: backfill {slug} complete [skip ci]",
                )

        # ── Post-loop: handle quota cap / 403 exit ────────────────────────────
        if quota_cap_hit:
            # Commit whatever the cap-hit competition managed to write,
            # plus the checkpoint so the next run resumes correctly
            run_git_commit(
                written_this_comp + [BACKFILL_PROGRESS_PATH, QUOTA_TRACKER_PATH],
                f"chore: backfill checkpoint — quota cap reached [skip ci]",
            )
            log.info(
                f"Backfill paused at daily cap. {total_written} file(s) written. "
                f"Checkpoint: {progress.last_completed_competition} "
                f"{progress.last_completed_file_stem}. "
                "Re-trigger this workflow tomorrow to continue."
            )
        else:
            progress.mark_complete()
            log.info(f"Backfill complete. {total_written} file(s) written.")

    finally:
        BACKFILL_LOCK_PATH.unlink(missing_ok=True)
        generate_summary()

    log.info(f"Quota: {quota.units_used} units used today.")


if __name__ == "__main__":
    main()
