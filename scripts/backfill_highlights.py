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
    COMPETITION_SLUG_MAP,
    FD_BASE,
    FD_SLEEP_SECONDS,
    QUOTA_TRACKER_PATH,
    QuotaCapReached,
    QuotaTracker,
    fd_get,
    generate_summary,
    gw_path,
    load_json_file,
    load_sources,
    merge_into_gw,
    resolve_videos_for_fixture,
    utc_now_iso,
    write_json_atomic,
)

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


# ── Season helper ─────────────────────────────────────────────────────────────


def current_season() -> int:
    """Return the current football season start year (e.g. 2025 for the 2025-26 season)."""
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


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
        self.last_completed_gameweek    = int(data.get("last_completed_gameweek", 0))
        self.competitions_done          = list(data.get("competitions_done", []))
        self.started_at                 = data.get("started_at") or utc_now_iso()
        self.last_resumed_at            = utc_now_iso()
        self._save()

    def _save(self) -> None:
        write_json_atomic(BACKFILL_PROGRESS_PATH, {
            "status":                     self.status,
            "season":                     self.season,
            "last_completed_competition": self.last_completed_competition,
            "last_completed_gameweek":    self.last_completed_gameweek,
            "competitions_done":          self.competitions_done,
            "started_at":                 self.started_at,
            "last_resumed_at":            self.last_resumed_at,
        })
        log.debug(
            f"Checkpoint saved: competitions_done={self.competitions_done}, "
            f"last_gw={self.last_completed_gameweek}"
        )

    def mark_gameweek_done(self, comp_name: str, matchday: int) -> None:
        self.last_completed_competition = comp_name
        self.last_completed_gameweek    = matchday
        self._save()

    def mark_competition_done(self, comp_name: str) -> None:
        if comp_name not in self.competitions_done:
            self.competitions_done.append(comp_name)
        self.last_completed_competition = comp_name
        self.last_completed_gameweek    = 0   # reset per-competition cursor
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
) -> dict[int, list[dict]]:
    """
    Fetch all FINISHED fixtures for a competition and season from football-data.org.

    Returns: {matchday: [fixture_dict, ...]}
    """
    resp = fd_get(
        f"{FD_BASE}/competitions/{code}/matches",
        fd_key,
        {"status": "FINISHED", "season": str(season)},
    )

    if resp.status_code == 404:
        log.warning(f"Competition {code} season {season} not found (404) — skipping")
        return {}
    if not resp.ok:
        log.warning(
            f"football-data.org HTTP {resp.status_code} for {code} — skipping"
        )
        return {}

    by_matchday: dict[int, list[dict]] = {}
    for m in resp.json().get("matches", []):
        matchday = m.get("matchday")
        utc_str  = m.get("utcDate", "")
        if matchday is None or not utc_str:
            continue

        home = m.get("homeTeam", {})
        away = m.get("awayTeam", {})
        by_matchday.setdefault(matchday, []).append({
            "match_id":   m["id"],
            "home_team":  home.get("name", ""),
            "home_short": home.get("shortName") or home.get("name", ""),
            "away_team":  away.get("name", ""),
            "away_short": away.get("shortName") or away.get("name", ""),
            "date":       utc_str[:10],
            "matchday":   matchday,
        })

    total = sum(len(v) for v in by_matchday.values())
    log.info(
        f"{comp_name} {season}: {total} finished fixture(s) "
        f"across {len(by_matchday)} matchday(s)"
    )
    return by_matchday


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    fd_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    yt_key = os.environ.get("YOUTUBE_API_KEY", "").strip()

    if not fd_key:
        log.error("FOOTBALL_DATA_API_KEY is not set")
        sys.exit(1)
    if not yt_key:
        log.error("YOUTUBE_API_KEY is not set — backfill requires YouTube access")
        sys.exit(1)

    season   = current_season()
    progress = BackfillProgress(season)

    # ── Startup log: always reflects the state loaded from disk ───────────────
    log.info(
        f"Backfill season {season} | "
        f"competitions done: {progress.competitions_done or '(none)'} | "
        f"resuming after GW{progress.last_completed_gameweek} in "
        f"{progress.last_completed_competition or 'start'}"
    )

    if progress.status == "complete":
        log.info(f"Season {season} backfill is already complete — nothing to do")
        generate_summary()
        return

    quota  = QuotaTracker()
    config = load_sources()

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
    # These track the current competition's context for the post-loop commit
    written_this_comp: list[Path] = []
    current_slug                  = ""

    try:
        for code, comp_name in COMPETITION_CODE_MAP.items():
            if quota_cap_hit:
                break

            slug = COMPETITION_SLUG_MAP[comp_name]

            # ── Step 3: resume check — must happen before any API calls ───────
            if comp_name in progress.competitions_done:
                log.info(f"INFO: {comp_name} already complete — skipping")
                continue

            log.info(f"── {comp_name} (season {season}) ──")
            written_this_comp = []
            current_slug      = slug
            time.sleep(FD_SLEEP_SECONDS)

            by_matchday = fetch_season_fixtures(code, comp_name, season, fd_key)
            if not by_matchday:
                progress.mark_competition_done(comp_name)
                run_git_commit(
                    [BACKFILL_PROGRESS_PATH, QUOTA_TRACKER_PATH],
                    f"chore: backfill {slug} complete [skip ci]",
                )
                continue

            # Determine which gameweek to resume from within this competition
            start_after = (
                progress.last_completed_gameweek
                if progress.last_completed_competition == comp_name
                else 0
            )

            for matchday in sorted(by_matchday.keys()):
                if quota_cap_hit:
                    break

                if matchday <= start_after:
                    log.info(f"  GW{matchday}: already checkpointed — skipping")
                    continue

                fixtures = by_matchday[matchday]
                path     = gw_path(comp_name, matchday)
                existing = load_json_file(path)

                # Build per-match lookup so already-covered fixtures are skipped
                existing_by_id: dict[int, dict] = {}
                if existing:
                    existing_by_id = {
                        m["match_id"]: m for m in existing.get("matches", [])
                    }

                log.info(f"  {comp_name} GW{matchday} ({len(fixtures)} fixture(s))…")
                enriched: list[dict] = []

                for fix in fixtures:
                    prior = existing_by_id.get(fix["match_id"])
                    if prior and prior.get("videos"):
                        # Already covered in a previous run — preserve the videos
                        enriched.append({**fix, "videos": prior["videos"]})
                        continue

                    try:
                        videos = resolve_videos_for_fixture(
                            fix, comp_name, config, yt_key, quota, BACKFILL_CAP
                        )
                    except QuotaCapReached as exc:
                        # Raised for both internal cap hits AND YouTube 403 responses
                        log.info(f"  Cap/quota reached: {exc}")
                        quota_cap_hit = True
                        break

                    enriched.append({**fix, "videos": videos})

                    if not videos:
                        log.warning(
                            f"No highlights — {comp_name} GW{matchday}: "
                            f"{fix['home_team']} vs {fix['away_team']} ({fix['date']})"
                        )
                    else:
                        tiers = sorted({v["tier_used"] for v in videos})
                        log.info(
                            f"    ✓ {fix['home_team']} vs {fix['away_team']}: "
                            f"{len(videos)} video(s) via tier(s) {tiers}"
                        )

                # Write whatever we have for this gameweek
                # (full set when normal; partial set when quota was hit mid-gameweek)
                if enriched:
                    gw_data, changed = merge_into_gw(
                        existing, comp_name, matchday, enriched
                    )
                    if changed:
                        write_json_atomic(path, gw_data)
                        total_written += 1
                        written_this_comp.append(path)
                        log.info(f"    → Wrote {path.name}")

                # Only advance the checkpoint when the full gameweek was processed
                if not quota_cap_hit:
                    progress.mark_gameweek_done(comp_name, matchday)

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
                f"GW{progress.last_completed_gameweek}. "
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
