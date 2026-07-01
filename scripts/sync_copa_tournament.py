#!/usr/bin/env python3
"""
scripts/sync_copa_tournament.py -- Copa América tournament data writer.

Fetches Copa América 2024 (API-Sports league=9, season=2024):
  /standings?league=9&season=2024  → group standings (W/D/L/GD/pts/form)
  /fixtures?league=9&season=2024   → knockout matches (stage + scores + crests)

Normalises both responses to tournament-groups/{slug}.json — the same shape that
TeamCacheService.fetchTournamentData() already reads for WC and Euro Cup.
No Copa-specific branch is needed in the app.

Security: APISPORTS_API_KEY must come from a GitHub Actions secret — never hardcode.
Copa 2024 is within the API-Sports free tier (seasons 2022–2024).
A future Copa edition outside that window is a separate paid-plan decision.

API-Sports quota discipline (hard invariants):
  This script is the ONLY source of API-Sports tournament calls.
  It is NEVER triggered by the 4-hour incremental highlights workflow.
  The Flutter app NEVER calls API-Sports directly.
"""

import logging
import os
import sys

from fixture_providers import (
    APISPORTS_COMPETITIONS,
    ApiSportsProvider,
    ApisportsQuotaTracker,
)
from highlights_common import (
    REPO_ROOT,
    utc_now_iso,
    write_json_atomic,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

SLUG     = "copa-america"
OUT_PATH = REPO_ROOT / "tournament-groups" / f"{SLUG}.json"

# Maps verbatim API-Sports league.round values → FD-compatible stage keys.
# Group-stage rounds are intentionally absent — filtered out of the knockout list.
KNOCKOUT_STAGE_MAP: dict = {
    "Quarter-finals":  "QUARTER_FINALS",
    "Semi-finals":     "SEMI_FINALS",
    "3rd Place Final": "THIRD_PLACE",
    "Final":           "FINAL",
}


# ── Pure normalisation helpers (exported for tests) ────────────────────────────


def normalize_standings(body: dict) -> list:
    """
    Normalise an API-Sports /standings response body to FD-compatible group entries.

    Returns list[dict] — one entry per group:
        {"group": "GROUP_A", "type": "TOTAL", "table": [row, ...]}

    Each row matches GroupStanding.fromJson field names exactly.
    Returns [] on any empty or missing structure.
    """
    response = body.get("response", [])
    if not response:
        return []

    raw_standings = response[0].get("league", {}).get("standings", [])
    groups = []

    for group_rows in raw_standings:
        if not group_rows:
            continue

        # API-Sports gives the group name per row (e.g. "Group A").
        # Normalise to "GROUP_A" so GroupEntry.fromJson produces label "A".
        raw_group = group_rows[0].get("group", "")
        group_key = raw_group.upper().replace(" ", "_")  # "Group A" → "GROUP_A"

        table = []
        for row in group_rows:
            team    = row.get("team", {})
            all_    = row.get("all", {})
            goals   = all_.get("goals", {})
            team_id = team.get("id", 0)
            table.append({
                "position":       row.get("rank", 0),
                "team": {
                    "id":    team_id,
                    "name":  team.get("name", ""),
                    "tla":   "",  # API-Sports provides no TLA
                    "crest": team.get("logo", ""),
                },
                "playedGames":    all_.get("played", 0),
                "won":            all_.get("win", 0),
                "draw":           all_.get("draw", 0),
                "lost":           all_.get("lose", 0),
                "goalsFor":       goals.get("for", 0),
                "goalsAgainst":   goals.get("against", 0),
                "goalDifference": row.get("goalsDiff", 0),
                "points":         row.get("points", 0),
                "form":           row.get("form") or "",
            })

        groups.append({"group": group_key, "type": "TOTAL", "table": table})

    return groups


def normalize_knockout(body: dict) -> list:
    """
    Normalise an API-Sports /fixtures response body to FD-compatible match entries.

    Returns list[dict] — one entry per knockout match (group-stage rounds skipped):
        {"stage": "QUARTER_FINALS",
         "homeTeam": {"id": ..., "name": ..., "tla": "", "crest": "…png"},
         "awayTeam": {...},
         "score": {"fullTime": {"home": N, "away": N}, "penalties": null | {home, away}}}

    Key translations:
      API-Sports "fulltime" (lowercase)  → FD-compatible "fullTime" (camelCase)
      API-Sports "penalty"  (singular)   → FD-compatible "penalties" (plural)
    Field names match KnockoutMatch.fromJson exactly.
    """
    matches = []
    for fix in body.get("response", []):
        league_round = fix.get("league", {}).get("round", "")
        stage = KNOCKOUT_STAGE_MAP.get(league_round)
        if stage is None:
            continue  # group-stage or unmapped round — skip

        teams   = fix.get("teams", {})
        home    = teams.get("home", {})
        away    = teams.get("away", {})
        score   = fix.get("score", {})
        ft      = score.get("fulltime", {})   # API-Sports: lowercase
        pens    = score.get("penalty", {})    # API-Sports: singular

        home_id = home.get("id")
        away_id = away.get("id")

        penalties = None
        if pens and pens.get("home") is not None and pens.get("away") is not None:
            penalties = {"home": pens["home"], "away": pens["away"]}

        matches.append({
            "stage": stage,
            "homeTeam": {
                "id":    home_id or 0,
                "name":  home.get("name", ""),
                "tla":   "",
                "crest": (
                    f"https://media.api-sports.io/football/teams/{home_id}.png"
                    if home_id else ""
                ),
            },
            "awayTeam": {
                "id":    away_id or 0,
                "name":  away.get("name", ""),
                "tla":   "",
                "crest": (
                    f"https://media.api-sports.io/football/teams/{away_id}.png"
                    if away_id else ""
                ),
            },
            "score": {
                "fullTime":   {"home": ft.get("home"), "away": ft.get("away")},
                "penalties":  penalties,
            },
        })

    return matches


def write_tournament(groups: list, matches: list, out_path=None) -> None:
    """Write tournament-groups/copa-america.json atomically."""
    path = out_path or OUT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": utc_now_iso(),
        "slug":         SLUG,
        "standings":    groups,
        "matches":      matches,
    }
    write_json_atomic(path, data)
    log.info(
        f"Wrote {path} ({len(groups)} group(s), {len(matches)} knockout match(es))"
    )


# ── Fetchers (wrap normalisation with live API calls) ──────────────────────────


def fetch_standings(provider: ApiSportsProvider, league_id: int, season: int) -> list:
    """Fetch and normalise group standings from API-Sports."""
    body = provider._get("/standings", {"league": league_id, "season": season})
    return [] if body is None else normalize_standings(body)


def fetch_knockout(provider: ApiSportsProvider, league_id: int, season: int) -> list:
    """Fetch and normalise knockout fixtures from API-Sports."""
    body = provider._get("/fixtures", {"league": league_id, "season": season})
    return [] if body is None else normalize_knockout(body)


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    comp_config = APISPORTS_COMPETITIONS["Copa America"]
    league_id   = comp_config["league_id"]
    season      = comp_config["season"]

    as_quota = ApisportsQuotaTracker()
    provider = ApiSportsProvider(quota_tracker=as_quota)

    log.info(f"Copa América {season}: fetching standings (league={league_id})…")
    groups = fetch_standings(provider, league_id, season)
    if not groups:
        log.error("No standings returned — aborting")
        sys.exit(1)
    log.info(f"  {len(groups)} group(s) fetched")

    log.info(f"Copa América {season}: fetching fixtures (league={league_id})…")
    matches = fetch_knockout(provider, league_id, season)
    log.info(f"  {len(matches)} knockout match(es) fetched")

    write_tournament(groups, matches)
    log.info(f"API-Sports calls used today: {as_quota.calls_used}")


if __name__ == "__main__":
    main()
