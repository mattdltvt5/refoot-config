#!/usr/bin/env python3
"""Write standings/{slug}.json for domestic leagues and the UCL league phase.

Each file is a flat list of GroupStanding-compatible row objects (one per team),
matching the football-data.org /standings table row shape so the Flutter app can
parse them with GroupStanding.fromJson() without any transformation.

Cadence: daily at 07:00 UTC via sync-standings.yml.
TTL in the Flutter app: 2 days (StandingsCacheService._staleDays = 2).
"""

import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

from season_utils import current_season  # canonical August-threshold rule (shared with fixtures pipeline)

FD_BASE = "https://api.football-data.org/v4"


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
    """Return only the TOTAL-type standings groups from a /standings payload."""
    return [
        s for s in standings_payload.get("standings", [])
        if s.get("type") == "TOTAL"
    ]


def write_standings(comp_name, slug, rows, out_dir="."):
    """Write standings/{slug}.json and return the path.

    Uses an atomic tmp→rename to avoid half-written files.
    rows is a flat list of FD table-row dicts (one per team).
    """
    os.makedirs(os.path.join(out_dir, "standings"), exist_ok=True)
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "competition":  comp_name,
        "slug":         slug,
        "standings":    rows,
    }
    path = os.path.join(out_dir, "standings", f"{slug}.json")
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
            path = write_standings(comp_name, slug, rows, out_dir)
            print(f"✓ {comp_name}: {len(rows)} rows → {path}")
        except urllib.error.HTTPError as e:
            print(f"✗ {comp_name}: HTTP {e.code} — skipping", file=sys.stderr)
        except Exception as e:
            print(f"✗ {comp_name}: {e} — skipping", file=sys.stderr)


if __name__ == "__main__":
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    if not api_key:
        print("ERROR: FOOTBALL_DATA_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    main(api_key)
