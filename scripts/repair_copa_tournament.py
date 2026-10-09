#!/usr/bin/env python3
"""Rebuild tournament-groups/copa-america.json offline, from data already in
this repo - no API calls (the API-Sports account is suspended).

Why: the only successful Copa sync (2026-07-01) used an old script, so the
committed file has the 4 group tables and 8 knockout scores but no group
matches, no match ids, no highlight ids and no match status. Without a FINAL
marked FINISHED, the app treats the 3-month-old file as stale and shows an
empty Copa screen.

Inputs (all committed):
  * tournament-groups/copa-america.json   - group tables + knockout scores
  * highlights/copa-america/2024/*.json   - every match's id, teams, crests,
                                             date and highlight videos
  * manual-data/copa-america-2024-results.json - the 24 group scores and the
                                             Final's extra-time result (Wikipedia)

Output: the same file with standings unchanged, all 24 group matches
(groupMatches: group, matchday, id, teams, score, FINISHED, video_id) and the
8 knockout matches with id, FINISHED status, video_id and the Final's
after-extra-time score.

Refuses to write (exit 1) unless every check passes:
  * the group scores reproduce the official group tables exactly (played,
    W/D/L, goals for/against, points) for all 16 teams;
  * every result pairs with exactly one highlight entry, in the same home/away
    order and on the same matchday;
  * 24 group matches and 8 knockout matches.

Usage:  python scripts/repair_copa_tournament.py [--check]   (--check: verify only)
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOURNAMENT = ROOT / "tournament-groups" / "copa-america.json"
HIGHLIGHTS = ROOT / "highlights" / "copa-america" / "2024"
RESULTS = ROOT / "manual-data" / "copa-america-2024-results.json"

KNOCKOUT_STEMS = {
    "QUARTER_FINALS": "quarter-final",
    "SEMI_FINALS": "semi-final",
    "THIRD_PLACE": "third-place",
    "FINAL": "final",
}

_CREST_ID = re.compile(r"/teams/(\d+)\.png$")


class RepairError(Exception):
    pass


def _team_id(crest: str) -> int:
    m = _CREST_ID.search(crest or "")
    return int(m.group(1)) if m else 0


def _video_id(entry: dict) -> str | None:
    vids = entry.get("videos") or []
    return vids[0].get("video_id") if vids else None


def recompute_tables(results: list[dict]) -> dict[str, dict]:
    """{team: {p, w, d, l, gf, ga, pts}} from the group results."""
    t: dict[str, dict] = collections.defaultdict(
        lambda: dict(p=0, w=0, d=0, l=0, gf=0, ga=0, pts=0))
    for r in results:
        hs, as_ = r["score"]
        for team, f, a in ((r["home"], hs, as_), (r["away"], as_, hs)):
            row = t[team]
            row["p"] += 1
            row["gf"] += f
            row["ga"] += a
            if f > a:
                row["w"] += 1
                row["pts"] += 3
            elif f == a:
                row["d"] += 1
                row["pts"] += 1
            else:
                row["l"] += 1
    return dict(t)


def check_tables(standings: list[dict], results: list[dict]) -> None:
    computed = recompute_tables(results)
    problems = []
    for grp in standings:
        for row in grp["table"]:
            name = row["team"]["name"]
            official = dict(p=row["playedGames"], w=row["won"], d=row["draw"],
                            l=row["lost"], gf=row["goalsFor"], ga=row["goalsAgainst"],
                            pts=row["points"])
            if computed.get(name) != official:
                problems.append(f"{grp['group']} {name}: official {official}, "
                                f"from scores {computed.get(name)}")
    if problems:
        raise RepairError("group scores don't match the official tables:\n  "
                          + "\n  ".join(problems))


def build_group_matches(results: list[dict], highlights: dict[int, list[dict]]) -> list[dict]:
    """groupMatches entries; highlights = {matchday: [highlight entries]}."""
    out = []
    for r in results:
        hits = [e for e in highlights.get(r["matchday"], [])
                if {e["home_team"], e["away_team"]} == {r["home"], r["away"]}]
        if len(hits) != 1:
            raise RepairError(f"{r['home']} v {r['away']} (matchday {r['matchday']}): "
                              f"{len(hits)} highlight entries")
        e = hits[0]
        if e["home_team"] != r["home"]:
            raise RepairError(f"{r['home']} v {r['away']}: home/away order differs "
                              f"from the highlight entry")
        out.append({
            "match_id": e["match_id"],
            "video_id": _video_id(e),
            "group": r["group"],
            "matchday": r["matchday"],
            "sourceRound": f"Group Stage - {r['matchday']}",
            "homeTeam": {"id": _team_id(e.get("home_crest", "")), "name": r["home"],
                         "tla": "", "crest": e.get("home_crest", "")},
            "awayTeam": {"id": _team_id(e.get("away_crest", "")), "name": r["away"],
                         "tla": "", "crest": e.get("away_crest", "")},
            "score": {"fullTime": {"home": r["score"][0], "away": r["score"][1]}},
            "status": "FINISHED",
        })
    return out


def repair_knockout(matches: list[dict], highlights: dict[str, list[dict]],
                    extra_time: list[dict]) -> list[dict]:
    """Knockout entries with id, video_id, FINISHED and extra-time scores.
    highlights = {stage: [highlight entries]}."""
    aet = {(x["stage"], x["home"], x["away"]): x["after_extra_time"] for x in extra_time}
    out = []
    for m in matches:
        stage, home, away = m["stage"], m["homeTeam"]["name"], m["awayTeam"]["name"]
        hits = [e for e in highlights.get(stage, [])
                if {e["home_team"], e["away_team"]} == {home, away}]
        if len(hits) != 1:
            raise RepairError(f"{stage} {home} v {away}: {len(hits)} highlight entries")
        score = json.loads(json.dumps(m.get("score") or {}))
        if (stage, home, away) in aet:
            h, a = aet[(stage, home, away)]
            score["fullTime"] = {"home": h, "away": a}
        out.append({**m, "id": m.get("id") or hits[0]["match_id"],
                    "video_id": _video_id(hits[0]), "status": "FINISHED",
                    "score": score})
    if len(out) != 8:
        raise RepairError(f"expected 8 knockout matches, got {len(out)}")
    return out


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build(now_iso: str) -> dict:
    current = _load(TOURNAMENT)
    manual = _load(RESULTS)
    results = manual["group_stage"]
    if len(results) != 24:
        raise RepairError(f"expected 24 group results, got {len(results)}")
    check_tables(current["standings"], results)

    group_hl = {md: _load(HIGHLIGHTS / f"matchday-{md}.json")["matches"] for md in (1, 2, 3)}
    ko_hl = {stage: _load(HIGHLIGHTS / f"{stem}.json")["matches"]
             for stage, stem in KNOCKOUT_STEMS.items()}
    return {
        "generated_at": now_iso,
        "slug": "copa-america",
        "standings": current["standings"],
        "matches": repair_knockout(current["matches"], ko_hl,
                                   manual.get("knockout_extra_time", [])),
        "groupMatches": build_group_matches(results, group_hl),
        "_repaired": "Rebuilt offline by scripts/repair_copa_tournament.py from committed "
                     "data + manual-data/copa-america-2024-results.json (Wikipedia).",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify only, don't write")
    args = ap.parse_args(argv)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        data = build(now)
    except RepairError as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    hl = sum(1 for m in data["groupMatches"] + data["matches"] if m.get("video_id"))
    print(f"OK: {len(data['standings'])} group tables, {len(data['groupMatches'])} group "
          f"matches, {len(data['matches'])} knockout matches, {hl} with highlights")
    if not args.check:
        TOURNAMENT.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
        print(f"wrote {TOURNAMENT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
