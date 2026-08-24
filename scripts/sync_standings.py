#!/usr/bin/env python3
"""Write standings/{slug}.json for domestic leagues and the UCL league phase.

Each file is a flat list of GroupStanding-compatible row objects (one per team),
matching the football-data.org /standings table row shape so the Flutter app can
parse them with GroupStanding.fromJson() without any transformation.

Cadence:
  - Full refresh (all competitions): daily at 07:00 UTC via sync-standings.yml.
  - Recent-finish refresh (--if-recent-finish): folded into fetch-highlights.yml
    (~5 min) so a league's standings refresh within one cycle of a match finishing,
    making 0 football-data calls on cycles where nothing finished. sync-standings.yml
    remains the daily full backstop.
TTL in the Flutter app: 2 days (StandingsCacheService._staleDays = 2).
"""

import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

from season_utils import current_season  # canonical August-threshold rule (shared with fixtures pipeline)
from highlights_common import override_crest  # manual crest overrides (per FD team id)

FD_BASE = "https://api.football-data.org/v4"

# --if-recent-finish only refreshes a league whose fixtures show a match FINISHED
# within this window. utcDate is kickoff, so ~4h covers a ~2h match + buffer and
# keeps refreshing for a few cycles after the whistle. Idle cycles = 0 FD calls.
RECENT_FINISH_HOURS = 4


# Competitions that get a standings file. Each entry is (FD id, display name, slug).
COMPETITIONS = [
    (2021, "Premier League",   "premier-league"),
    (2014, "LaLiga",           "laliga"),
    (2019, "Serie A",          "serie-a"),
    (2002, "Bundesliga",       "bundesliga"),
    (2015, "Ligue 1",          "ligue-1"),
    (2001, "Champions League", "ucl"),
]


def fetch_standings(comp_id, api_key, base_url=FD_BASE, season=None):
    """Fetch /standings for a competition. Returns parsed JSON dict.

    Raises urllib.error.HTTPError on non-200 responses.
    base_url and season are overridable for unit tests.
    Passing season avoids FD defaulting to a not-yet-started future season.
    """
    url = f"{base_url}/competitions/{comp_id}/standings"
    if season is not None:
        url += f"?season={season}"
    req = urllib.request.Request(url, headers={"X-Auth-Token": api_key})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def extract_total_table(standings_payload):
    """Return only the TOTAL-type standings groups from a /standings payload,
    applying manual per-team crest overrides (see highlights_common.CREST_OVERRIDES)
    so a team whose FD crest is broken in the app (e.g. Le Mans) shows the fixed
    crest here too."""
    groups = [
        s for s in standings_payload.get("standings", [])
        if s.get("type") == "TOTAL"
    ]
    for g in groups:
        for row in g.get("table", []) or []:
            team = row.get("team")
            if isinstance(team, dict):
                team["crest"] = override_crest(team.get("id"), team.get("crest", ""))
    return groups


def write_standings(comp_name, slug, rows, season, out_dir="."):
    """Write standings/{slug}/{season}.json and return the path.

    Uses an atomic tmp→rename to avoid half-written files.
    rows is a flat list of FD table-row dicts (one per team).
    """
    os.makedirs(os.path.join(out_dir, "standings", slug), exist_ok=True)
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "competition":  comp_name,
        "slug":         slug,
        "season":       season,
        "standings":    rows,
    }
    path = os.path.join(out_dir, "standings", slug, f"{season}.json")
    tmp  = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return path


def main(api_key, out_dir="."):
    season = current_season()
    for i, (comp_id, comp_name, slug) in enumerate(COMPETITIONS):
        if i > 0:
            time.sleep(7)  # free tier: 10 req/min
        try:
            payload = fetch_standings(comp_id, api_key, season=season)
            rows = []
            for group in extract_total_table(payload):
                rows.extend(group.get("table", []))
            path = write_standings(comp_name, slug, rows, season, out_dir)
            print(f"✓ {comp_name}: {len(rows)} rows → {path}")
        except urllib.error.HTTPError as e:
            print(f"✗ {comp_name}: HTTP {e.code} — skipping", file=sys.stderr)
        except Exception as e:
            print(f"✗ {comp_name}: {e} — skipping", file=sys.stderr)


def had_recent_finish(slug, season, out_dir=".", now=None):
    """True if fixtures/{slug}/{season}.json has a FINISHED match whose kickoff
    (utcDate) is within RECENT_FINISH_HOURS of now. Reads the fixtures artifact
    that fetch_highlights.py wrote this run (no extra FD call). Missing/unreadable
    fixtures -> False (the daily full sync is the backstop)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RECENT_FINISH_HOURS)
    path = os.path.join(out_dir, "fixtures", slug, f"{season}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    for fx in data.get("fixtures", []):
        if fx.get("status") != "FINISHED":
            continue
        ko = fx.get("utcDate")
        if not ko:
            continue
        try:
            dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= cutoff:
            return True
    return False


def main_recent(api_key, out_dir=".", now=None):
    """Smart-skip: refresh standings ONLY for leagues with a recent finish.
    Competitions without a fixtures artifact (e.g. UCL) are left to the daily job."""
    season = current_season(now)
    eligible = [c for c in COMPETITIONS
                if had_recent_finish(c[2], season, out_dir, now)]
    if not eligible:
        print("No competition had a recent finish - skipping standings (0 FD calls).")
        return
    for i, (comp_id, comp_name, slug) in enumerate(eligible):
        if i > 0:
            time.sleep(7)  # free tier: 10 req/min
        try:
            payload = fetch_standings(comp_id, api_key, season=season)
            rows = []
            for group in extract_total_table(payload):
                rows.extend(group.get("table", []))
            path = write_standings(comp_name, slug, rows, season, out_dir)
            print(f"✓ {comp_name}: {len(rows)} rows -> {path} (recent finish)")
        except urllib.error.HTTPError as e:
            print(f"✗ {comp_name}: HTTP {e.code} - skipping", file=sys.stderr)
        except Exception as e:
            print(f"✗ {comp_name}: {e} - skipping", file=sys.stderr)


if __name__ == "__main__":
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    if not api_key:
        print("ERROR: FOOTBALL_DATA_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if "--if-recent-finish" in sys.argv:
        main_recent(api_key)
    else:
        main(api_key)
