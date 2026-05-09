#!/usr/bin/env python3
"""
backfill_highlights.py — Full-season highlights backfill.

Manual trigger only (workflow_dispatch via backfill-highlights.yml).
Fetches all FINISHED fixtures for the current football season from
football-data.org and searches YouTube playlists in tier-priority order.

Progress is checkpointed after every gameweek so re-triggering resumes
exactly where it stopped. Exits cleanly at 9,500 YouTube units/day
(BACKFILL_CAP) with the checkpoint saved for the next run.

If a gameweek was partially processed when quota was hit, the partial
results are written before exiting so the per-match skip avoids redundant
API calls on the next run.

Environment variables required:
    FOOTBALL_DATA_API_KEY   — football-data.org personal access token
    YOUTUBE_API_KEY         — YouTube Data API v3 key
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

from highlights_common import (
    BACKFILL_CAP,
    BACKFILL_LOCK_PATH,
    BACKFILL_PROGRESS_PATH,
    COMPETITION_CODE_MAP,
    FD_BASE,
    FD_SLEEP_SECONDS,
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
            data = {}  # season rollover — start fresh

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

    def mark_gameweek_done(self, comp_name: str, matchday: int) -> None:
        self.last_completed_competition = comp_name
        self.last_completed_gameweek    = matchday
        self._save()

    def mark_competition_done(self, comp_name: str) -> None:
        if comp_name not in self.competitions_done:
            self.competitions_done.append(comp_name)
        self.last_completed_gameweek = 0  # reset per-competition cursor
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

    if progress.status == "complete":
        log.info(f"Season {season} backfill is already complete — nothing to do")
        generate_summary()
        return

    log.info(
        f"Backfill season {season} | "
        f"competitions done: {progress.competitions_done or '(none)'} | "
        f"resuming after GW{progress.last_completed_gameweek} in "
        f"{progress.last_completed_competition or '(start)'}"
    )

    quota  = QuotaTracker()
    config = load_sources()

    BACKFILL_LOCK_PATH.write_text(utc_now_iso(), encoding="utf-8")
    total_written = 0
    quota_cap_hit = False

    try:
        for code, comp_name in COMPETITION_CODE_MAP.items():
            if quota_cap_hit:
                break

            if comp_name in progress.competitions_done:
                log.info(f"{comp_name}: already complete — skipping")
                continue

            log.info(f"── {comp_name} (season {season}) ──")
            time.sleep(FD_SLEEP_SECONDS)

            by_matchday = fetch_season_fixtures(code, comp_name, season, fd_key)
            if not by_matchday:
                progress.mark_competition_done(comp_name)
                continue

            # Resume past already-checkpointed gameweeks for this competition
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

                # Per-match skip: build lookup of already-covered matches
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
                        # Already has videos from a previous partial run — preserve them
                        enriched.append({**fix, "videos": prior["videos"]})
                        continue

                    try:
                        videos = resolve_videos_for_fixture(
                            fix, comp_name, config, yt_key, quota, BACKFILL_CAP
                        )
                    except QuotaCapReached as exc:
                        log.info(f"Daily cap reached: {exc}")
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

                # Write whatever we have (full gameweek or partial if quota hit)
                if enriched:
                    gw_data, changed = merge_into_gw(
                        existing, comp_name, matchday, enriched
                    )
                    if changed:
                        write_json_atomic(path, gw_data)
                        total_written += 1
                        log.info(f"    → Wrote {path.name}")

                # Only checkpoint as done when all fixtures were processed
                if not quota_cap_hit:
                    progress.mark_gameweek_done(comp_name, matchday)

            if not quota_cap_hit:
                progress.mark_competition_done(comp_name)

        if not quota_cap_hit:
            progress.mark_complete()
            log.info(f"Backfill complete. {total_written} file(s) written.")
        else:
            log.info(
                f"Backfill paused at daily cap. {total_written} file(s) written. "
                f"Checkpoint: {progress.last_completed_competition} GW{progress.last_completed_gameweek}. "
                "Re-trigger this workflow tomorrow to continue."
            )

    finally:
        BACKFILL_LOCK_PATH.unlink(missing_ok=True)
        generate_summary()

    log.info(f"Quota: {quota.units_used} units used today.")


if __name__ == "__main__":
    main()
