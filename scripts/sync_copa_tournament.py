#!/usr/bin/env python3
"""
scripts/sync_copa_tournament.py -- Copa América tournament data writer.

Fetches Copa América 2024 (API-Sports league=9, season=2024):
  /standings?league=9&season=2024  → group standings (W/D/L/GD/pts/form)
  /fixtures?league=9&season=2024   → knockout matches + group-stage fixtures

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

import json
import logging
import os
import re
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

# Highlight file stems for Copa knockout stages (single-leg only).
_COPA_STAGE_STEMS: dict = {
    "QUARTER_FINALS": "quarter-final",
    "SEMI_FINALS":    "semi-final",
    "THIRD_PLACE":    "third-place",
    "FINAL":          "final",
}


def _lookup_copa_video_id(match_id) -> "str | None":
    """Return a YouTube video_id for match_id from the Copa highlights files."""
    if match_id is None:
        return None
    for stage, stem in _COPA_STAGE_STEMS.items():
        path = REPO_ROOT / "highlights" / SLUG / f"{stem}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("matches", [])
            for e in items:
                if e.get("match_id") == match_id and e.get("videos"):
                    return e["videos"][0]["video_id"]
        except Exception:
            pass
    return None

# Maps verbatim API-Sports league.round values → FD-compatible stage keys.
# Group-stage rounds are intentionally absent; they go into groupMatches instead.
KNOCKOUT_STAGE_MAP: dict = {
    "Quarter-finals":  "QUARTER_FINALS",
    "Semi-finals":     "SEMI_FINALS",
    "3rd Place Final": "THIRD_PLACE",
    "Final":           "FINAL",
}

# Regex that extracts the matchday integer from "Group Stage - N".
# Matches any spacing/dash variant defensively (e.g. "Group Stage - 2", "Group Stage-3").
_GROUP_STAGE_RE = re.compile(r'Group\s+Stage\s*[-–]\s*(\d+)', re.IGNORECASE)


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


def _parse_matchday(round_str: str) -> "int | None":
    """Parse matchday integer from an API-Sports group-stage round string.

    "Group Stage - 1" → 1, "Group Stage - 3" → 3.
    Returns None when the pattern doesn't match (unknown/non-group-stage round).
    """
    m = _GROUP_STAGE_RE.search(round_str)
    return int(m.group(1)) if m else None


def _build_team_group_map(standings: list) -> dict:
    """Build {team_id: group_key} from normalized standings entries.

    standings is the output of normalize_standings() — each entry has
    {"group": "GROUP_A", "table": [{"team": {"id": ...}, ...}, ...]}.
    """
    mapping: dict = {}
    for entry in standings:
        group = entry.get("group", "")
        for row in entry.get("table", []):
            team_id = row.get("team", {}).get("id")
            if team_id is not None:
                mapping[team_id] = group
    return mapping


def normalize_group(body: dict, team_group_map: dict) -> list:
    """
    Normalise API-Sports /fixtures response body to groupMatches entries.

    Only group-stage rounds (matching "Group Stage - N") are included.
    Knockout rounds (present in KNOCKOUT_STAGE_MAP) are excluded.

    team_group_map: {team_id: "GROUP_A", ...} — built via _build_team_group_map()
    from normalize_standings() output; used to assign each fixture to its group.

    Returns list[dict] — one entry per group-stage fixture:
        {"group":       "GROUP_A",
         "matchday":    1,
         "sourceRound": "Group Stage - 1",
         "homeTeam":    {"id": ..., "name": ..., "tla": "", "crest": "…png"},
         "awayTeam":    {...},
         "score":       {"fullTime": {"home": N, "away": N}},
         "status":      "FT"}

    Returns [] on any empty or missing structure, and silently skips fixtures
    whose round string cannot be parsed to a matchday integer.
    """
    matches = []
    for fix in body.get("response", []):
        league_round = fix.get("league", {}).get("round", "")

        # Exclude knockout rounds that belong in the separate matches array.
        if KNOCKOUT_STAGE_MAP.get(league_round) is not None:
            continue

        matchday = _parse_matchday(league_round)
        if matchday is None:
            continue  # round label not recognised as group-stage — skip silently

        teams   = fix.get("teams", {})
        home    = teams.get("home", {})
        away    = teams.get("away", {})
        score   = fix.get("score", {})
        ft      = score.get("fulltime", {})  # API-Sports: lowercase key

        home_id = home.get("id")
        away_id = away.get("id")

        # Derive group from standings map; fall back to "" if team not found.
        group = team_group_map.get(home_id) or team_group_map.get(away_id) or ""

        status = (
            fix.get("fixture", {}).get("status", {}).get("short", "")
        )

        matches.append({
            "group":       group,
            "matchday":    matchday,
            "sourceRound": league_round,
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
                "fullTime": {"home": ft.get("home"), "away": ft.get("away")},
            },
            "status": status,
        })

    return matches


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

        fixture_id = fix.get("fixture", {}).get("id")
        matches.append({
            "id":    fixture_id,  # API-Sports fixture ID; used for highlight lookup
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


def write_tournament(
    groups: list,
    matches: list,
    group_matches: list,
    out_path=None,
) -> None:
    """Write tournament-groups/copa-america.json atomically."""
    path = out_path or OUT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": utc_now_iso(),
        "slug":         SLUG,
        "standings":    groups,
        "matches":      matches,       # knockout matches — Knockout tab
        "groupMatches": group_matches, # group-stage fixtures — Groups tab
    }
    write_json_atomic(path, data)
    log.info(
        f"Wrote {path} ({len(groups)} group(s), {len(matches)} knockout "
        f"match(es), {len(group_matches)} group match(es))"
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

    # Fetch fixtures once; both knockout and group normalizers read this body.
    # This keeps the call count at exactly 2 (standings + fixtures).
    log.info(f"Copa América {season}: fetching fixtures (league={league_id})…")
    fixtures_body = provider._get("/fixtures", {"league": league_id, "season": season})

    matches = [] if fixtures_body is None else normalize_knockout(fixtures_body)
    matches = [{**m, "video_id": _lookup_copa_video_id(m.get("id"))} for m in matches]
    log.info(f"  {len(matches)} knockout match(es)")

    team_group_map = _build_team_group_map(groups)
    group_matches  = [] if fixtures_body is None else normalize_group(fixtures_body, team_group_map)
    log.info(f"  {len(group_matches)} group match(es)")

    write_tournament(groups, matches, group_matches)
    log.info(f"API-Sports calls used today: {as_quota.calls_used}")


if __name__ == "__main__":
    main()
