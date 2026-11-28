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

import json, os, sys, time, urllib.error
from datetime import datetime, timezone, timedelta

from season_utils import current_season  # canonical August-threshold rule (shared with fixtures pipeline)
from highlights_common import JOB_STANDINGS, override_crest  # crest overrides (per FD team id)
from fixture_providers import CAP_STANDINGS, iter_job_providers

# --if-recent-finish only refreshes a league whose fixtures show a match FINISHED
# within this window. utcDate is kickoff, so ~4h covers a ~2h match + buffer and
# keeps refreshing for a few cycles after the whistle. Idle cycles = 0 FD calls.
RECENT_FINISH_HOURS = 4


# Competitions that get a standings file: COMPETITION_CONFIG's "standings" job
# (highlights_common), each fetched through its provider's get_standings().


def _report_skip(line):
    print(line, file=sys.stderr)


def standings_providers(api_key):
    """(comp_name, comp_cfg, provider) for every competition in the standings job
    whose provider can supply standings; the others are skipped with one line."""
    return list(iter_job_providers(JOB_STANDINGS, CAP_STANDINGS, {"fd_key": api_key},
                                   report=_report_skip))


def _sync_one(provider, comp_name, comp_cfg, season, out_dir, suffix=""):
    """Fetch, flatten and write one competition's standings; errors are logged
    and skipped (the previous file stays)."""
    slug = comp_cfg["slug"]
    try:
        payload = provider.get_standings(comp_name, comp_cfg, season=season)
        rows = []
        for group in extract_total_table(payload):
            rows.extend(group.get("table", []))
        path = write_standings(comp_name, slug, rows, season, out_dir)
        print(f"✓ {comp_name}: {len(rows)} rows → {path}{suffix}")
    except urllib.error.HTTPError as e:
        print(f"✗ {comp_name}: HTTP {e.code} — skipping", file=sys.stderr)
    except Exception as e:
        print(f"✗ {comp_name}: {e} — skipping", file=sys.stderr)


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
    for i, (comp_name, comp_cfg, provider) in enumerate(standings_providers(api_key)):
        if i > 0:
            time.sleep(7)  # free tier: 10 req/min
        _sync_one(provider, comp_name, comp_cfg, season, out_dir)


def _finished_kickoffs(slug, season, out_dir="."):
    """Kickoff strings of the FINISHED matches this run's artifacts hold for one
    competition: fixtures/{slug}/{season}.json for the leagues, else
    tournament-groups/{slug}.json (league phase + knockouts) for UCL, which has no
    fixtures artifact. The tournament file is only used for the same season."""
    def load(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    data = load(os.path.join(out_dir, "fixtures", slug, f"{season}.json"))
    if data is not None:
        return [fx.get("utcDate") for fx in data.get("fixtures", [])
                if fx.get("status") == "FINISHED"]
    data = load(os.path.join(out_dir, "tournament-groups", f"{slug}.json"))
    if data is None or data.get("season") not in (None, season):
        return []
    return [m.get("utcDate")
            for m in (data.get("groupMatches") or []) + (data.get("matches") or [])
            if m.get("status") == "FINISHED"]


def had_recent_finish(slug, season, out_dir=".", now=None):
    """True if this competition has a FINISHED match whose kickoff (utcDate) is
    within RECENT_FINISH_HOURS of now. Reads the artifacts fetch_highlights.py and
    sync_tournaments.py wrote this run (no extra FD call): the league fixtures, or
    the tournament file for UCL - without it the UCL table waited for the daily
    job (on 2026-10-15 it was still pre-matchday at 10:45 UTC). Missing/unreadable
    -> False (the daily full sync is the backstop)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RECENT_FINISH_HOURS)
    for ko in _finished_kickoffs(slug, season, out_dir):
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
    """Smart-skip: refresh standings ONLY for competitions with a recent finish
    (leagues from their fixtures, UCL from its tournament file)."""
    season = current_season(now)
    eligible = [c for c in standings_providers(api_key)
                if had_recent_finish(c[1]["slug"], season, out_dir, now)]
    if not eligible:
        print("No competition had a recent finish - skipping standings (0 FD calls).")
        return
    for i, (comp_name, comp_cfg, provider) in enumerate(eligible):
        if i > 0:
            time.sleep(7)  # free tier: 10 req/min
        _sync_one(provider, comp_name, comp_cfg, season, out_dir, " (recent finish)")


if __name__ == "__main__":
    api_key = os.environ.get("FOOTBALL_DATA_API_KEY", "")
    if not api_key:
        print("ERROR: FOOTBALL_DATA_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    if "--if-recent-finish" in sys.argv:
        main_recent(api_key)
    else:
        main(api_key)
