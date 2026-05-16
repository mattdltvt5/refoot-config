#!/usr/bin/env python3
"""
fetch_highlights.py — Highlights cache update.

Runs every 4 hours via the fetch-highlights GitHub Action.
Fetches all FINISHED fixtures for the current season from football-data.org,
searches configured YouTube playlists in tier-priority order for any fixture
not yet covered, and merges results into per-gameweek JSON files under
highlights/{competition-slug}/.

Smart-skip logic keeps quota consumption low:
  - Complete gameweeks (every fixture has ≥1 video) are skipped with 0 API calls.
  - Within incomplete gameweeks, fixtures that already have videos are preserved
    without making any YouTube API calls.

This means adding a new broadcaster to sources.json automatically fills in
any historical gaps on the next scheduled run, with no manual backfill needed.

Budget: exits cleanly at 8,000 YouTube units/day (INCREMENTAL_CAP).
Yields to the backfill job when highlights/backfill.lock is present and recent.

Environment variables required:
    FOOTBALL_DATA_API_KEY   — football-data.org personal access token
    YOUTUBE_API_KEY         — YouTube Data API v3 key (optional; skips searches if absent)
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

from highlights_common import (
    BACKFILL_LOCK_PATH,
    COMPETITION_CODE_MAP,
    FD_BASE,
    FD_SLEEP_SECONDS,
    INCREMENTAL_CAP,
    QuotaCapReached,
    QuotaTracker,
    current_season,
    fd_get,
    generate_summary,
    gw_path,
    is_gameweek_complete,
    load_json_file,
    load_sources,
    merge_into_gw,
    resolve_videos_for_fixture,
    write_json_atomic,
)

log = logging.getLogger(__name__)


# ── Fixture fetching ──────────────────────────────────────────────────────────


def fetch_all_fixtures(fd_key: str, season: int) -> dict[str, dict[int, list[dict]]]:
    """
    Fetch all FINISHED fixtures for the given season across every configured
    competition. Sleeps FD_SLEEP_SECONDS between requests to respect
    football-data.org's 10 req/min free-tier limit.

    Returns: {competition_name: {matchday: [fixture_dict, ...]}}
    """
    result: dict[str, dict[int, list[dict]]] = {}

    for i, (code, comp_name) in enumerate(COMPETITION_CODE_MAP.items()):
        if i > 0:
            time.sleep(FD_SLEEP_SECONDS)

        try:
            resp = fd_get(
                f"{FD_BASE}/competitions/{code}/matches",
                fd_key,
                {"status": "FINISHED", "season": str(season)},
            )
        except SystemExit:
            raise
        except Exception as exc:
            log.warning(f"Network error fetching {code}: {exc}")
            continue

        if resp.status_code == 404:
            log.warning(f"Competition {code} season {season} not found (404) — skipping")
            continue
        if not resp.ok:
            log.warning(
                f"football-data.org HTTP {resp.status_code} for {code} — skipping"
            )
            continue

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

        if by_matchday:
            result[comp_name] = by_matchday
            total = sum(len(v) for v in by_matchday.values())
            log.info(
                f"{comp_name}: {total} finished fixture(s) "
                f"across {len(by_matchday)} matchday(s)"
            )

    return result


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    fd_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    yt_key = os.environ.get("YOUTUBE_API_KEY", "").strip()

    if not fd_key:
        log.error("FOOTBALL_DATA_API_KEY is not set")
        sys.exit(1)
    if not yt_key:
        log.warning(
            "YOUTUBE_API_KEY is not set — YouTube playlist searches will be skipped. "
            "summary.json will still be regenerated from existing highlight files."
        )

    # ── Lockfile guard: yield to backfill if it's actively running ────────────
    if BACKFILL_LOCK_PATH.exists():
        try:
            lock_age = (
                datetime.now(timezone.utc)
                - datetime.fromtimestamp(
                    BACKFILL_LOCK_PATH.stat().st_mtime, tz=timezone.utc
                )
            ).total_seconds()
        except OSError:
            lock_age = 0

        if lock_age < 3 * 3600:
            log.info(
                f"Backfill in progress (lock is {lock_age / 60:.0f} min old) "
                "— skipping incremental run"
            )
            sys.exit(0)

        log.warning(
            f"Stale backfill lock ({lock_age / 3600:.1f}h old) — removing and continuing"
        )
        BACKFILL_LOCK_PATH.unlink(missing_ok=True)

    # ── Quota guard: skip YouTube work if daily budget already spent ──────────
    quota = QuotaTracker()
    if quota.over_incremental_cap:
        log.info(
            f"Daily budget consumed ({quota.units_used}/{INCREMENTAL_CAP} units) "
            "— regenerating summary only"
        )
        generate_summary()
        sys.exit(0)

    # ── No YouTube key: regenerate summary and exit ───────────────────────────
    if not yt_key:
        generate_summary()
        return

    season       = current_season()
    config       = load_sources()
    all_fixtures = fetch_all_fixtures(fd_key, season)

    if not all_fixtures:
        log.info(f"No finished fixtures found for season {season}.")
        generate_summary()
        return

    total_written = 0
    try:
        for comp_name, by_matchday in sorted(all_fixtures.items()):
            for matchday, fixtures in sorted(by_matchday.items()):
                path     = gw_path(comp_name, matchday)
                existing = load_json_file(path)

                if is_gameweek_complete(existing, fixtures):
                    log.info(f"GW{matchday} {comp_name}: complete — skipping")
                    continue

                # Build per-match lookup so already-covered fixtures cost 0 quota
                existing_by_id: dict[int, dict] = {}
                if existing:
                    existing_by_id = {
                        m["match_id"]: m for m in existing.get("matches", [])
                    }

                log.info(
                    f"Processing {comp_name} GW{matchday} ({len(fixtures)} fixture(s))…"
                )
                enriched: list[dict] = []

                for fix in fixtures:
                    prior = existing_by_id.get(fix["match_id"])
                    if prior and prior.get("videos"):
                        # Already covered — preserve videos without any API calls
                        enriched.append({**fix, "videos": prior["videos"]})
                        continue

                    videos = resolve_videos_for_fixture(
                        fix, comp_name, config, yt_key, quota, INCREMENTAL_CAP
                    )
                    enriched.append({**fix, "videos": videos})

                    if not videos:
                        log.warning(
                            f"No highlights — {comp_name} GW{matchday}: "
                            f"{fix['home_team']} vs {fix['away_team']} ({fix['date']})"
                        )
                    else:
                        tiers = sorted({v["tier_used"] for v in videos})
                        log.info(
                            f"  ✓ {fix['home_team']} vs {fix['away_team']}: "
                            f"{len(videos)} video(s) via tier(s) {tiers}"
                        )

                gw_data, changed = merge_into_gw(existing, comp_name, matchday, enriched)
                if changed:
                    write_json_atomic(path, gw_data)
                    total_written += 1
                    log.info(f"  → Wrote {path.relative_to(path.parent.parent.parent)}")
                else:
                    log.info(f"  → No changes to {path.name}")

    except QuotaCapReached as exc:
        log.info(
            f"Daily cap reached: {exc} — "
            f"committing {total_written} file(s) and stopping"
        )

    log.info(
        f"Done. {total_written} file(s) updated. "
        f"Quota: {quota.units_used} units used today."
    )
    generate_summary()


if __name__ == "__main__":
    main()
