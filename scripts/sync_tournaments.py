#!/usr/bin/env python3
"""Rebuild tournament-groups/{slug}.json for football-data.org tournaments.

Owns the FULL tournament cache for the FD-sourced tournaments (Euro Cup, World
Cup, Champions League): standings, knockout `matches`, and group-stage
`groupMatches` — plus the matched YouTube `video_id` graft.

Cadence: runs every ~4 hours inside fetch-highlights.yml (the single tournament
-cache refresher), immediately after fetch_highlights.py writes highlights/.
This keeps knockout SCORES and STATUS fresh on the ~4-hour cadence instead of
the old weekly roster cron — a game that finishes now populates its score
within one highlights cycle rather than up to a week later.

Verbatim FD passthrough: knockout match objects are written unchanged
(`{**m, "video_id": ...}`) so the exact score.fullTime / status / stage shape
the Flutter app already parses is preserved.  Scores are never transformed —
an unplayed (TIMED) game keeps its null score, a finished game keeps its real
one, including penalty-shootout / extra-time cases.

API isolation:
    football-data.org ONLY.  This script makes NO API-Sports calls.  Copa
    América's scores are owned by scripts/sync_copa_tournament.py (weekly,
    API-Sports) — this script only GRAFTS video_ids into copa-america.json
    from the local highlights cache (a local read, no API call) and never
    rewrites Copa's scores or forces FD fields onto its schema.

FD quota: 2 calls per tournament (standings + matches) × 3 tournaments = 6
    calls/run, throttled at FD_SLEEP_SECONDS between requests to respect the
    free-tier 10 req/min limit.  These are the same calls the weekly roster job
    used to make; they add no YouTube or API-Sports quota.
"""

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from highlights_common import season_for_competition, COMPETITION_SLUG_MAP

FD_BASE = "https://api.football-data.org/v4"
FD_SLEEP_SECONDS = 6  # pause between football-data.org requests (10 req/min free tier)

# FD-sourced tournaments whose full cache this script rebuilds. Each entry is
# (FD competition id, display name, output slug).
#
# Copa América is deliberately absent: it is an API-Sports competition owned by
# scripts/sync_copa_tournament.py.  Adding a future FD tournament is a one-line
# change here (kept generic — no WC-only special-casing).
TOURNAMENT_COMPETITIONS = [
    (2018, "Euro Cup",         "euro-cup"),
    (2000, "World Cup",        "world-cup"),
    (2001, "Champions League", "ucl"),
]

# Stage values football-data.org uses for knockout rounds. LAST_32 is included
# so Round-of-32 fixtures (48-team World Cup) flow through like any other round.
KNOCKOUT_STAGES = {
    "LAST_32", "LAST_16", "QUARTER_FINALS", "SEMI_FINALS", "THIRD_PLACE", "FINAL",
}

# Reverse of COMPETITION_SLUG_MAP — used in graft_video_ids() to resolve
# a tournament-groups slug back to a competition name for season lookup.
_SLUG_TO_COMP_NAME: dict = {v: k for k, v in COMPETITION_SLUG_MAP.items()}


# ── football-data.org fetch (urllib; base_url/season overridable for tests) ────


def fetch_matches(comp_id, api_key, base_url=FD_BASE, season=None):
    """Fetch /matches for a competition. Returns the parsed JSON dict.

    No status filter — the tournament cache needs ALL matches (TIMED knockout
    slots as well as FINISHED ones) so unplayed fixtures appear with null
    scores.  Raises urllib.error.HTTPError on non-200 responses.
    """
    url = f"{base_url}/competitions/{comp_id}/matches"
    if season is not None:
        url += f"?season={season}"
    req = urllib.request.Request(url, headers={"X-Auth-Token": api_key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def fetch_standings(comp_id, api_key, base_url=FD_BASE, season=None):
    """Fetch /standings for a competition. Returns the parsed JSON dict."""
    url = f"{base_url}/competitions/{comp_id}/standings"
    if season is not None:
        url += f"?season={season}"
    req = urllib.request.Request(url, headers={"X-Auth-Token": api_key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


# ── Cache construction (pure functions) ────────────────────────────────────────


def build_group_matches(matches_payload):
    """Re-project group-stage fixtures into the `groupMatches` array shape.

    Group matches are those with a non-null "group" field that are NOT in a
    knockout stage.  Score is copied faithfully via score.fullTime and the FD
    status is carried through unchanged (a TIMED group game stays null).
    UCL league-phase matches have group=None, so they produce an empty list.
    """
    group_matches = []
    for m in matches_payload.get("matches", []):
        if m.get("stage") in KNOCKOUT_STAGES:
            continue
        raw_group = m.get("group")
        if not raw_group:
            continue  # no group field → league phase or qualifying; skip
        matchday = m.get("matchday")
        if not matchday:
            continue
        group_key = raw_group.upper().replace(" ", "_")  # "Group A" → "GROUP_A"
        ht    = m.get("homeTeam", {}) or {}
        at    = m.get("awayTeam", {}) or {}
        score = m.get("score", {}) or {}
        ft    = score.get("fullTime", {}) or {}
        group_matches.append({
            "match_id":    m.get("id"),
            # utcDate is present on every FD /matches object (knockout siblings carry
            # it verbatim); copy it here so group games get a date and appear on the
            # date-driven Home feed. Null-safe: an unscheduled fixture with no date
            # yields None, which normalize_match treats as "don't place on Home".
            "utcDate":     m.get("utcDate"),
            "group":       group_key,
            "matchday":    matchday,
            "sourceRound": f"Matchday {matchday}",
            "homeTeam": {
                "id":    ht.get("id"),
                "name":  ht.get("name", ""),
                "tla":   ht.get("tla", ""),
                "crest": ht.get("crest", ""),
            },
            "awayTeam": {
                "id":    at.get("id"),
                "name":  at.get("name", ""),
                "tla":   at.get("tla", ""),
                "crest": at.get("crest", ""),
            },
            "score": {
                "fullTime": {"home": ft.get("home"), "away": ft.get("away")},
            },
            "status": m.get("status", ""),
        })
    return group_matches


def build_tournament_data(slug, standings_payload, matches_payload,
                          existing_video_ids=None):
    """Build the full tournament-groups dict for one competition.

    Knockout `matches` are a VERBATIM FD passthrough: every FD match object is
    spread unchanged and only a video_id field is grafted on top, so
    score.fullTime, score.duration, score.winner, and top-level status keep the
    exact shape the Flutter app parses.  No score is transformed.
    """
    existing_video_ids = existing_video_ids or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "slug": slug,
        "standings": [
            s for s in standings_payload.get("standings", [])
            if s.get("type") == "TOTAL"
        ],
        "matches": [
            {**m, "video_id": existing_video_ids.get(m.get("id"))}
            for m in matches_payload.get("matches", [])
            if m.get("stage") in KNOCKOUT_STAGES
        ],
        "groupMatches": [
            {**gm, "video_id": existing_video_ids.get(gm.get("match_id"))}
            for gm in build_group_matches(matches_payload)
        ],
    }


def read_existing_video_ids(path):
    """Return {match_id: video_id} for already-resolved video_ids on disk.

    Preserving these across a rebuild means the frequent tournament refresh
    never wipes a matched highlight link even before the graft step re-runs.
    """
    result = {}
    p = pathlib.Path(path)
    if not p.exists():
        return result
    try:
        prev = json.loads(p.read_text(encoding="utf-8"))
        for m in prev.get("matches", []):
            if m.get("video_id") is not None:
                result[m.get("id")] = m["video_id"]
        for gm in prev.get("groupMatches", []):
            if gm.get("video_id") is not None and gm.get("match_id") is not None:
                result[gm.get("match_id")] = gm["video_id"]
    except Exception:
        pass
    return result


def write_tournament(slug, data, out_dir="."):
    """Write tournament-groups/{slug}.json atomically and return the path."""
    os.makedirs(os.path.join(out_dir, "tournament-groups"), exist_ok=True)
    path = os.path.join(out_dir, "tournament-groups", f"{slug}.json")
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return path


# ── video_id graft (from the local highlights cache — no API calls) ────────────


def _stage_stem(stage, matchday):
    """Map an FD knockout stage (+ leg matchday) to a highlights file stem."""
    fixed = {"LAST_32": "round-of-32", "THIRD_PLACE": "third-place", "FINAL": "final"}
    if stage in fixed:
        return fixed[stage]
    legged = {"LAST_16": "round-of-16", "QUARTER_FINALS": "quarter-final",
              "SEMI_FINALS": "semi-final"}
    if stage in legged:
        base = legged[stage]
        if matchday in (1, 2):
            return f"{base}-leg-{matchday}"
        return base
    return None


def _read_video_from_stem(slug, stem, match_id, season, out_dir="."):
    """Return the first matched video_id in highlights/{slug}/{season}/{stem}.json."""
    if match_id is None:
        return None
    p = pathlib.Path(out_dir) / "highlights" / slug / str(season) / f"{stem}.json"
    if not p.exists():
        return None
    try:
        data  = json.loads(p.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else data.get("matches", [])
        for e in items:
            if e.get("match_id") == match_id and e.get("videos"):
                return e["videos"][0]["video_id"]
    except Exception:
        pass
    return None


def _lookup_video_id(slug, stage, matchday, match_id, season, out_dir="."):
    """Find the first matched video_id for a knockout match in highlights/."""
    stem = _stage_stem(stage, matchday)
    if not stem:
        return None
    return _read_video_from_stem(slug, stem, match_id, season, out_dir)


def _lookup_group_video_id(slug, matchday, match_id, season, out_dir="."):
    """Find the first matched video_id for a group match in highlights/."""
    if matchday is None:
        return None
    return _read_video_from_stem(slug, f"matchday-{matchday}", match_id, season, out_dir)


def graft_video_ids(out_dir="."):
    """Graft matched video_ids into EVERY tournament-groups/*.json.

    Generic across all slugs — including copa-america, whose scores come from
    its own scheduled API-Sports job but whose video_ids are refreshed here.
    Reads only local files (highlights/), makes no API calls.
    Grafts both knockout matches (matches[]) and group-stage matches (groupMatches[]).
    """
    tg_dir = pathlib.Path(out_dir) / "tournament-groups"
    if not tg_dir.exists():
        print("No tournament-groups/ directory — nothing to graft.")
        return
    for tg_path in sorted(tg_dir.glob("*.json")):
        slug = tg_path.stem
        comp_name = _SLUG_TO_COMP_NAME.get(slug)
        if comp_name is None:
            print(f"  {slug}: unknown slug — skipping video_id graft", file=sys.stderr)
            continue
        season = season_for_competition(comp_name)
        try:
            tg = json.loads(tg_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  {slug}: parse error — {e}", file=sys.stderr)
            continue

        changed = False
        for m in tg.get("matches", []):
            if m.get("stage") not in KNOCKOUT_STAGES:
                continue
            vid = _lookup_video_id(
                slug, m.get("stage"), m.get("matchday"), m.get("id"), season, out_dir
            )
            if vid is not None and vid != m.get("video_id"):
                m["video_id"] = vid
                changed = True

        for gm in tg.get("groupMatches", []):
            vid = _lookup_group_video_id(
                slug, gm.get("matchday"), gm.get("match_id"), season, out_dir
            )
            if vid is not None and vid != gm.get("video_id"):
                gm["video_id"] = vid
                changed = True

        if changed:
            tg_path.write_text(
                json.dumps(tg, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"✓ {slug}: updated tournament-groups video IDs")
        else:
            print(f"  {slug}: no new video IDs")


# ── Orchestration ──────────────────────────────────────────────────────────────


def main(api_key, out_dir="."):
    # 1) Rebuild the FD tournament caches (scores + status + all FD fields).
    for i, (comp_id, comp_name, slug) in enumerate(TOURNAMENT_COMPETITIONS):
        if i > 0:
            time.sleep(FD_SLEEP_SECONDS)
        try:
            standings_payload = fetch_standings(comp_id, api_key)
            time.sleep(FD_SLEEP_SECONDS)
            matches_payload = fetch_matches(comp_id, api_key)
        except urllib.error.HTTPError as e:
            print(f"✗ {comp_name}: HTTP {e.code} — keeping existing cache",
                  file=sys.stderr)
            continue
        except Exception as e:
            print(f"✗ {comp_name}: {e} — keeping existing cache", file=sys.stderr)
            continue

        existing_path = os.path.join(out_dir, "tournament-groups", f"{slug}.json")
        existing_video_ids = read_existing_video_ids(existing_path)
        data = build_tournament_data(
            slug, standings_payload, matches_payload, existing_video_ids
        )
        path = write_tournament(slug, data, out_dir)
        print(f"✓ {comp_name}: {path} written ({len(data['matches'])} knockout matches)")

    # 2) Graft matched video_ids into every tournament cache (incl. Copa) from
    #    the freshly-written local highlights.  No API calls.
    graft_video_ids(out_dir)


if __name__ == "__main__":
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    if not api_key:
        print("ERROR: FOOTBALL_DATA_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    main(api_key)
