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

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from highlights_common import (
    BACKFILL_LOCK_PATH,
    COMPETITION_CODE_MAP,
    COMPETITION_SLUG_MAP,
    DOMESTIC_LEAGUE_COMPS,
    FD_BASE,
    FIXTURES_DIR,
    HIGHLIGHTS_DIR,
    FD_SLEEP_SECONDS,
    INCREMENTAL_CAP,
    QuotaCapReached,
    season_for_competition,
    QuotaTracker,
    _normalize,
    fd_get,
    generate_summary,
    gw_path,
    is_gameweek_complete,
    is_same_tournament_edition,
    load_json_file,
    load_sources,
    merge_into_gw,
    resolve_videos_for_fixture,
    stage_to_file_stem,
    team_tokens,
    utc_now_iso,
    write_json_atomic,
)
from fixture_providers import FootballDataProvider
import build_home_index

log = logging.getLogger(__name__)


# ── CLI args ──────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch highlights incrementally.")
    p.add_argument(
        "--debug-matching",
        action="store_true",
        help=(
            "Emit per-fixture candidate rejection reasons for every fixture that "
            "ends up with no highlights.  Quota-neutral: reuses already-fetched "
            "playlist items in memory, makes zero extra YouTube API calls.  "
            "Can also be enabled with REFOOT_DEBUG_MATCHING=1."
        ),
    )
    return p.parse_args()


# ── Debug output ──────────────────────────────────────────────────────────────

# Maximum number of candidate-reason lines shown per fixture in the debug block.
# Caps the display only — the debug_sink itself may hold more records.
_DEBUG_DISPLAY_CAP = 10


def _emit_debug_block(
    fix: dict,
    comp_name: str,
    stem: str,
    debug_sink: list,
) -> None:
    """
    Emit a structured diagnostic block for a fixture that ended up with no
    highlights.  Called only when debug_matching is True.

    Shows:
      - Fixture team names as received from football-data.org + their
        _normalize() forms (accent probe).
      - The token sets the matcher actually compares against (from
        team_tokens()).
      - Per-candidate rejection reasons collected in debug_sink, capped at
        _DEBUG_DISPLAY_CAP, grouped by tier / playlist_id.
    """
    home = fix["home_team"]
    away = fix["away_team"]
    h_norm = _normalize(home)
    a_norm = _normalize(away)

    log.info("=" * 72)
    log.info(f"DEBUG MATCH FAILURE  {comp_name} / {stem} / {fix['date']}")
    log.info(f"  fixture: {home!r}  vs  {away!r}")

    # ── Accent probe ─────────────────────────────────────────────────────────
    # Confirm whether diacritics (Alavés→alaves, Köln→koln, ø→ø) are stripped
    # by _normalize().  This is a read-only probe — no effect on acceptance.
    accent_tag = "diacritics stripped" if h_norm != home.lower() else "no diacritic change"
    log.info(f"  accent-probe home: {home!r}  →  {h_norm!r}  ({accent_tag})")
    accent_tag = "diacritics stripped" if a_norm != away.lower() else "no diacritic change"
    log.info(f"  accent-probe away: {away!r}  →  {a_norm!r}  ({accent_tag})")

    # ── Token sets ───────────────────────────────────────────────────────────
    h_toks = team_tokens(home, fix.get("home_short", home), fix.get("home_tla", ""))
    a_toks = team_tokens(away, fix.get("away_short", away), fix.get("away_tla", ""))
    log.info(f"  home tokens: {sorted(h_toks)}")
    log.info(f"  away tokens: {sorted(a_toks)}")

    if not debug_sink:
        log.info("  (no playlist candidates were evaluated — all tiers skipped or empty)")
        log.info("=" * 72)
        return

    # ── Per-candidate reason lines ───────────────────────────────────────────
    # Iterate in collection order (= tier priority order).
    # Print a tier/playlist header whenever the source changes.
    # Hard-stop at _DEBUG_DISPLAY_CAP individual candidate lines.
    total = len(debug_sink)
    shown = 0
    prev_key: tuple | None = None

    for rec in debug_sink:
        key = (rec.get("tier"), rec.get("playlist_id", "?"))
        if key != prev_key:
            # Count how many candidates belong to this source
            src_count = sum(
                1 for r in debug_sink
                if (r.get("tier"), r.get("playlist_id", "?")) == key
            )
            log.info(
                f"  [Tier {key[0]} / {key[1]}] — {src_count} candidate(s)"
            )
            prev_key = key

        if shown >= _DEBUG_DISPLAY_CAP:
            continue  # keep printing source headers but skip detail lines

        log.info(f"    [{rec['video_id']}]  {rec['reason']}")
        log.info(f"      title: {rec['title']!r}")
        nt = rec.get("norm_title", "")
        if nt and nt != rec["title"].lower():
            log.info(f"      norm:  {nt!r}")
        shown += 1

    if total > _DEBUG_DISPLAY_CAP:
        log.info(
            f"  (showing {_DEBUG_DISPLAY_CAP} of {total} total candidates; "
            f"increase _DEBUG_DISPLAY_CAP in fetch_highlights.py to see more)"
        )
    log.info("=" * 72)


# ── Fixture fetching ──────────────────────────────────────────────────────────


def fetch_all_fixtures(
    fd_key: str,
) -> tuple[dict[str, dict[str, list[dict]]], dict[str, list[dict]]]:
    """
    Fetch fixtures across every configured competition via FootballDataProvider.

    Each competition uses its own season year via season_for_competition(): domestic
    leagues / UCL / UEL follow the August-July convention; summer tournaments (World Cup,
    Euro Cup) use the current calendar year.

    Sleeps FD_SLEEP_SECONDS between requests to respect football-data.org's 10 req/min limit.
    The raw FD response is cached inside FootballDataProvider, so get_fixtures() and
    get_full_season() share one HTTP request per competition — no double-fetch.

    Returns a 2-tuple:
      highlights  — {comp_name: {file_stem: [fixture_dict, ...]}}  (FINISHED only)
      artifacts   — {comp_name: [GroupMatch-compatible dicts]}     (all statuses, domestic leagues only)
    """
    provider   = FootballDataProvider(fd_key)
    highlights: dict[str, dict[str, list[dict]]] = {}
    artifacts:  dict[str, list[dict]]             = {}

    for i, (code, comp_name) in enumerate(COMPETITION_CODE_MAP.items()):
        if i > 0:
            time.sleep(FD_SLEEP_SECONDS)

        season  = season_for_competition(comp_name)
        by_stem = provider.get_fixtures(code, comp_name, season)
        if by_stem:
            highlights[comp_name] = by_stem

        if comp_name in DOMESTIC_LEAGUE_COMPS:
            full = provider.get_full_season(code, comp_name, season)
            if full:
                artifacts[comp_name] = full

    return highlights, artifacts


def write_fixtures_artifacts(artifacts: dict[str, list[dict]]) -> None:
    """Write one fixtures/{slug}/{season}.json per domestic league."""
    for comp_name, fixtures in artifacts.items():
        slug   = COMPETITION_SLUG_MAP[comp_name]
        season = season_for_competition(comp_name)
        path   = FIXTURES_DIR / slug / f"{season}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {
            "competition":  comp_name,
            "season":       season,
            "generated_at": utc_now_iso(),
            "fixtures":     fixtures,
        })
        log.info(f"Wrote fixtures artifact: fixtures/{slug}/{season}.json ({len(fixtures)} fixture(s))")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    args = _parse_args()
    debug_matching = args.debug_matching or bool(
        os.environ.get("REFOOT_DEBUG_MATCHING", "").strip()
    )
    if debug_matching:
        log.info(
            "REFOOT_DEBUG_MATCHING enabled — per-fixture candidate reasons will be "
            "logged for every fixture with no highlights.  Zero extra API calls."
        )

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

    # ── Fetch FD data: shared by fixtures artifacts + YouTube matching ─────────
    # Runs before quota/YT-key guards so fixtures/{slug}.json artifacts are always
    # written (FD-only, no YouTube quota consumed) even when over the daily YouTube
    # budget.  The raw FD response is cached inside FootballDataProvider so
    # get_fixtures() and get_full_season() share one HTTP request per competition.
    all_highlights, all_artifacts = fetch_all_fixtures(fd_key)
    write_fixtures_artifacts(all_artifacts)

    # Regenerate the cross-competition, date-indexed Home artifact (home-index/).
    # Derives purely from the just-written fixtures + existing cached highlights and
    # tournament-groups files — NO external API calls. Ties index freshness to the
    # existing fixtures cadence rather than adding a new cron. Content-driven writes
    # keep re-runs diff-free.
    try:
        build_home_index.regenerate()
    except Exception as e:  # never let a derived-artifact failure block highlights
        log.warning(f"home-index regeneration failed (non-fatal): {e}")

    # ── Quota guard: skip YouTube work if daily budget already spent ──────────
    quota = QuotaTracker()
    if quota.over_incremental_cap:
        log.info(
            f"Daily budget consumed ({quota.units_used}/{INCREMENTAL_CAP} units) "
            "— regenerating summary only"
        )
        generate_summary()
        write_json_atomic(
            HIGHLIGHTS_DIR / "fetch-log.json",
            {"last_run": datetime.now(timezone.utc).isoformat(), "files_updated": 0, "additions": []},
        )
        sys.exit(0)

    # ── No YouTube key: regenerate summary and exit ───────────────────────────
    if not yt_key:
        generate_summary()
        write_json_atomic(
            HIGHLIGHTS_DIR / "fetch-log.json",
            {"last_run": datetime.now(timezone.utc).isoformat(), "files_updated": 0, "additions": []},
        )
        return

    config = load_sources()

    if not all_highlights:
        log.info("No finished fixtures found.")
        generate_summary()
        write_json_atomic(
            HIGHLIGHTS_DIR / "fetch-log.json",
            {"last_run": datetime.now(timezone.utc).isoformat(), "files_updated": 0, "additions": []},
        )
        return

    total_written = 0
    new_additions: list[dict] = []
    gw_playlist_cache: dict = {}  # shared across all fixtures to avoid redundant playlists.list calls
    try:
        for comp_name, by_stem in sorted(all_highlights.items()):
            season = season_for_competition(comp_name)
            for stem, fixtures in sorted(by_stem.items()):
                path     = gw_path(comp_name, stem, season)
                existing = load_json_file(path)

                # Detect new tournament edition for non-annual competitions
                # (World Cup, Euro Cup). If the on-disk file belongs to a
                # different year, discard it so the new edition starts clean.
                if not is_same_tournament_edition(existing, fixtures, comp_name):
                    existing = None

                if is_gameweek_complete(existing, fixtures):
                    log.info(f"{stem} {comp_name}: complete — skipping")
                    continue

                # Build per-match lookup so already-covered fixtures cost 0 quota
                existing_by_id: dict[int, dict] = {}
                if existing:
                    existing_by_id = {
                        m["match_id"]: m for m in existing.get("matches", [])
                    }

                log.info(
                    f"Processing {comp_name} {stem} ({len(fixtures)} fixture(s))…"
                )
                enriched: list[dict] = []

                for fix in fixtures:
                    prior = existing_by_id.get(fix["match_id"])
                    if prior and prior.get("videos"):
                        # Already covered — preserve videos without any API calls
                        enriched.append({**fix, "videos": prior["videos"]})
                        continue

                    # Allocate a fresh sink per fixture so each debug block is
                    # self-contained.  None when debug_matching is off (default).
                    _sink: list | None = [] if debug_matching else None
                    videos = resolve_videos_for_fixture(
                        fix, comp_name, config, yt_key, quota, INCREMENTAL_CAP,
                        gw_playlist_cache=gw_playlist_cache,
                        debug_sink=_sink,
                    )
                    enriched.append({**fix, "videos": videos})

                    if not videos:
                        if debug_matching:
                            _emit_debug_block(fix, comp_name, stem, _sink or [])
                        log.warning(
                            f"No highlights — {comp_name} {stem}: "
                            f"{fix['home_team']} vs {fix['away_team']} ({fix['date']})"
                        )
                    else:
                        new_additions.append({
                            "comp":   comp_name,
                            "home":   fix["home_team"],
                            "away":   fix["away_team"],
                            "date":   fix["date"],
                            "videos": len(videos),
                        })
                        tiers = sorted({v["tier_used"] for v in videos})
                        log.info(
                            f"  ✓ {fix['home_team']} vs {fix['away_team']}: "
                            f"{len(videos)} video(s) via tier(s) {tiers}"
                        )

                gw_data, changed = merge_into_gw(existing, comp_name, stem, enriched)
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
    write_json_atomic(
        HIGHLIGHTS_DIR / "fetch-log.json",
        {
            "last_run":      datetime.now(timezone.utc).isoformat(),
            "files_updated": total_written,
            "additions":     new_additions,
        },
    )
    generate_summary()


if __name__ == "__main__":
    main()
