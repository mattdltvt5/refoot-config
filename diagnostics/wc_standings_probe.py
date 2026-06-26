"""
Access probe: World Cup 2026 standings reachability
----------------------------------------------------
Checks whether football-data.org (free tier) and API-Sports (free tier)
can serve WC 2026 group standings.  Run before scaffolding any UI.

Usage:
    python diagnostics/wc_standings_probe.py

API key for football-data.org is the same one used by the Flutter app
(already public in lib/services/football_data_service.dart).
API-Sports key is read from APISPORTS_API_KEY env-var; missing key → that
section is marked SKIP rather than ERROR.

Verdict printed at the end:
  PASS  — at least one provider returns standings data
  BLOCK — no provider returns standings; do not scaffold UI
"""

import json
import os
import sys
import time

import requests

FD_KEY  = "f732e07d95494f34a625a18468afd3ba"
FD_BASE = "https://api.football-data.org/v4"

APISPORTS_KEY  = os.environ.get("APISPORTS_API_KEY", "")
APISPORTS_BASE = "https://v3.football.api-sports.io"

# WC 2026: football-data.org competition id=2000, code="WC"
FD_WC_ID    = 2000
FD_WC_CODE  = "WC"

# World Cup on API-Sports: league_id=1, season=2026
AS_WC_LEAGUE = 1
AS_WC_SEASON = 2026

SEP = "-" * 60


def probe_fd_standings() -> dict:
    """Probe /v4/competitions/2000/standings on football-data.org."""
    print(f"\n{'='*60}")
    print("PROBE 1 — football-data.org /standings (free tier)")
    print(SEP)

    url = f"{FD_BASE}/competitions/{FD_WC_ID}/standings"
    try:
        resp = requests.get(url, headers={"X-Auth-Token": FD_KEY}, timeout=15)
    except requests.RequestException as exc:
        print(f"  Network error: {exc}")
        return {"provider": "football-data.org", "status": "NETWORK_ERROR", "pass": False}

    print(f"  HTTP {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        standings = data.get("standings", [])
        total_entries = [s for s in standings if s.get("type") == "TOTAL"]
        groups = [s.get("group", "?") for s in total_entries]
        print(f"  Competition : {data.get('competition', {}).get('name', '?')}")
        print(f"  Season      : {data.get('season', {}).get('startDate', '?')} – "
              f"{data.get('season', {}).get('endDate', '?')}")
        print(f"  Stage       : {data.get('season', {}).get('currentMatchday', '?')}")
        print(f"  Groups found: {len(total_entries)}")
        if total_entries:
            print(f"  Group names : {', '.join(groups[:6])}{'...' if len(groups) > 6 else ''}")
            sample = total_entries[0].get("table", [])
            if sample:
                t = sample[0]
                print(f"  Sample row  : {t['team']['name']} ({t['team']['tla']}) "
                      f"Pts={t['points']} GD={t['goalDifference']} "
                      f"crest={t['team'].get('crest', 'n/a')[:60]}")
        return {
            "provider": "football-data.org",
            "status": "OK",
            "groups": len(total_entries),
            "group_names": groups,
            "pass": len(total_entries) > 0,
        }
    elif resp.status_code == 403:
        body = {}
        try:
            body = resp.json()
        except Exception:
            pass
        msg = body.get("message", body.get("errorCode", "Forbidden"))
        print(f"  403 Forbidden — {msg}")
        print("  → Free tier does not cover this endpoint for WC 2026.")
        return {"provider": "football-data.org", "status": "FORBIDDEN", "message": msg, "pass": False}
    elif resp.status_code == 404:
        print("  404 Not Found — competition or season absent on this plan.")
        return {"provider": "football-data.org", "status": "NOT_FOUND", "pass": False}
    else:
        print(f"  Unexpected status {resp.status_code}: {resp.text[:120]}")
        return {"provider": "football-data.org", "status": str(resp.status_code), "pass": False}


def probe_fd_matches() -> dict:
    """Probe /v4/competitions/2000/matches?stage=GROUP_STAGE as a fallback."""
    print(f"\n{'='*60}")
    print("PROBE 2 — football-data.org /matches?stage=GROUP_STAGE (fallback)")
    print(SEP)

    url = f"{FD_BASE}/competitions/{FD_WC_ID}/matches"
    params = {"stage": "GROUP_STAGE"}
    try:
        resp = requests.get(url, headers={"X-Auth-Token": FD_KEY},
                            params=params, timeout=15)
    except requests.RequestException as exc:
        print(f"  Network error: {exc}")
        return {"provider": "football-data.org/matches", "status": "NETWORK_ERROR", "pass": False}

    print(f"  HTTP {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        matches = data.get("matches", [])
        finished = [m for m in matches if m.get("status") == "FINISHED"]
        scheduled = [m for m in matches if m.get("status") == "SCHEDULED"]
        # Extract groups from homeTeam.name if stage field present
        groups_seen = {m.get("group", "?") for m in matches if m.get("group")}
        print(f"  Total matches   : {len(matches)}")
        print(f"  FINISHED        : {len(finished)}")
        print(f"  SCHEDULED       : {len(scheduled)}")
        print(f"  Groups seen     : {sorted(groups_seen)[:6]}")
        if finished:
            m = finished[0]
            ht = m.get("homeTeam", {})
            at = m.get("awayTeam", {})
            sc = m.get("score", {}).get("fullTime", {})
            print(f"  Sample match    : {ht.get('name')} {sc.get('home')}–{sc.get('away')} {at.get('name')}")
        return {
            "provider": "football-data.org/matches",
            "status": "OK",
            "total_matches": len(matches),
            "finished": len(finished),
            "groups_seen": sorted(groups_seen),
            "pass": len(matches) > 0,
        }
    elif resp.status_code == 403:
        body = {}
        try:
            body = resp.json()
        except Exception:
            pass
        msg = body.get("message", "Forbidden")
        print(f"  403 Forbidden — {msg}")
        return {"provider": "football-data.org/matches", "status": "FORBIDDEN", "message": msg, "pass": False}
    else:
        print(f"  HTTP {resp.status_code}: {resp.text[:120]}")
        return {"provider": "football-data.org/matches", "status": str(resp.status_code), "pass": False}


def probe_apisports() -> dict:
    """Probe API-Sports /fixtures for WC 2026 season."""
    print(f"\n{'='*60}")
    print("PROBE 3 — API-Sports /fixtures (WC 2026, free tier)")
    print(SEP)

    if not APISPORTS_KEY:
        print("  SKIP — APISPORTS_API_KEY not set in environment.")
        return {"provider": "API-Sports", "status": "SKIPPED", "pass": False}

    headers = {"x-apisports-key": APISPORTS_KEY}
    url = f"{APISPORTS_BASE}/fixtures"
    params = {"league": AS_WC_LEAGUE, "season": AS_WC_SEASON}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
    except requests.RequestException as exc:
        print(f"  Network error: {exc}")
        return {"provider": "API-Sports", "status": "NETWORK_ERROR", "pass": False}

    print(f"  HTTP {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        errors = data.get("errors", {})
        if errors:
            print(f"  Errors: {errors}")
            return {"provider": "API-Sports", "status": "API_ERROR", "errors": str(errors), "pass": False}
        fixtures = data.get("response", [])
        print(f"  Fixtures returned: {len(fixtures)}")
        if fixtures:
            f = fixtures[0]
            ht = f.get("teams", {}).get("home", {})
            at = f.get("teams", {}).get("away", {})
            print(f"  Sample: {ht.get('name')} vs {at.get('name')}")
        return {
            "provider": "API-Sports",
            "status": "OK",
            "fixtures": len(fixtures),
            "pass": len(fixtures) > 0,
        }
    else:
        print(f"  HTTP {resp.status_code}: {resp.text[:200]}")
        return {"provider": "API-Sports", "status": str(resp.status_code), "pass": False}


def main() -> None:
    print("ReFoot — World Cup 2026 standings access probe")
    print(f"Date: 2026-06-26  |  football-data.org key: ...{FD_KEY[-6:]}")

    r1 = probe_fd_standings()
    time.sleep(1)
    r2 = probe_fd_matches()
    time.sleep(1)
    r3 = probe_apisports()

    results = [r1, r2, r3]

    print(f"\n{'='*60}")
    print("VERDICT")
    print(SEP)
    for r in results:
        icon = "PASS" if r["pass"] else "FAIL"
        status = r["status"]
        print(f"  [{icon}] {r['provider']:<35} {status}")

    any_pass = any(r["pass"] for r in results)
    print()
    if any_pass:
        passing = [r["provider"] for r in results if r["pass"]]
        print(f"  OVERALL: PASS — {', '.join(passing)} can feed the WC tournament view.")
        print("  Proceed with UI scaffolding.")
    else:
        print("  OVERALL: BLOCK — no provider returns WC standings/matches.")
        print("  Do not scaffold UI; resolve provider access first.")
    print(SEP)

    sys.exit(0 if any_pass else 1)


if __name__ == "__main__":
    main()
