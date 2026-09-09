#!/usr/bin/env python3
"""READ-ONLY detector for team-title ALIAS GAPS (Layer 2).

Finds FINISHED fixtures with no joined highlight where a candidate video is a
genuine highlight of the correct match, correctly dated, that passes
`is_highlight_title` — but is rejected solely because one team's name in the
title is a short form the token set doesn't cover (the `cross-match-guard:
*-missing` signal). For each, it PROPOSES a safe alias and writes them to
highlights/alias-candidates.json for a human to approve in the admin — exactly
like find_channel_candidates.py does for channels. It NEVER edits sources.json
or TEAM_TITLE_ALIASES and never auto-adopts.

Safety of a proposal:
  * The alias must be a contiguous word-subsequence of the missing team's
    official FD name that appears in the title (so it can propose "Ipswich" for
    "Ipswich Town FC" but can NEVER invent a nickname like "Tractor Boys").
  * length >= 4.
  * A single-word alias must be UNIQUE across all tracked teams (same collision
    gate Layer 1 uses) — ambiguous words are surfaced under `flags`, not proposed.
Titles whose short form is NOT part of the official name (pure nicknames /
alt-language) can't be safely auto-proposed; they are surfaced under `flags`
for a human to add manually.

READ-ONLY: playlistItems.list only (via the pipeline matcher), ReadOnlyQuota
(no quota-tracker.json write), no cache writes, no adoption. The only file it
writes is its own proposals file (+ an optional report via REPORT_OUT).
"""
import json, glob, os, io, sys
from datetime import datetime, timezone
from pathlib import Path

try:                                    # UTF-8 stdout so the report prints on Windows too
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

REPO = Path(__file__).resolve().parents[1]
SEASON = int(os.environ.get("SEASON_OVERRIDE", "2025"))
DOMESTIC = {
    "premier-league": "Premier League", "laliga": "LaLiga", "serie-a": "Serie A",
    "bundesliga": "Bundesliga", "ligue-1": "Ligue 1",
}
sys.path.insert(0, str(REPO / "scripts"))

from highlights_common import (          # noqa: E402
    load_sources, resolve_videos_for_fixture, QuotaTracker, QuotaCapReached,
    INCREMENTAL_CAP, team_tokens, _normalize, _team_word_index,
    _GENERIC_TEAM_WORDS,
)
from playlist_discovery import apply_discovered_overrides  # noqa: E402

CANDIDATES_PATH = REPO / "highlights" / "alias-candidates.json"


class ReadOnlyQuota(QuotaTracker):
    """QuotaTracker with zero disk I/O — counts units in memory only."""
    def __init__(self) -> None:
        self.date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.units_used = 0
    def _save(self) -> None:
        pass


def jload(p):
    with io.open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def highlight_have(slug):
    have = {}
    for p in glob.glob(str(REPO / "highlights" / slug / str(SEASON) / "*.json")):
        for m in jload(p).get("matches", []):
            mid = m.get("match_id")
            if mid is not None:
                have[mid] = have.get(mid, False) or bool(m.get("videos"))
    return have


def to_fix(f):
    h, a = f.get("homeTeam") or {}, f.get("awayTeam") or {}
    return {
        "match_id": f.get("match_id"),
        "home_team": h.get("name", ""), "home_short": h.get("shortName", ""),
        "home_tla": h.get("tla", ""),
        "away_team": a.get("name", ""), "away_short": a.get("shortName", ""),
        "away_tla": a.get("tla", ""),
        "date": (f.get("utcDate") or "")[:10], "matchday": f.get("matchday"),
    }


def is_alias_miss(reasons):
    """True iff EVERY sink record for a candidate is a cross-match team miss —
    i.e. it cleared date-window, comp-keyword and is_highlight_title, and only
    the team token failed."""
    return bool(reasons) and all(r.startswith("cross-match-guard:") for r in reasons)


def propose_alias(missing_fd_name, title, current_tokens):
    """Longest contiguous word-subsequence of the missing team's FD name that
    appears in the title but isn't already a token. Returns (alias, is_multi) or
    (None, False)."""
    nt = _normalize(title)
    words = _normalize(missing_fd_name).split()
    for size in range(len(words), 0, -1):
        for i in range(0, len(words) - size + 1):
            gram = " ".join(words[i:i + size])
            if len(gram) < 4 or gram in current_tokens or gram not in nt:
                continue
            if size == 1 and gram in _GENERIC_TEAM_WORDS:
                continue          # never propose a bare generic club-type word
            return gram, size > 1
    return None, False


def unique_alias(alias, missing_fd_name):
    """Single-word alias must belong to exactly one tracked team."""
    return _team_word_index().get(alias, set()) <= {missing_fd_name}


def main():
    # ---- gather NO-HIGHLIGHT fixtures ------------------------------------
    no_hl = []   # (comp, slug, fix)
    for slug, comp in DOMESTIC.items():
        fp = REPO / "fixtures" / slug / f"{SEASON}.json"
        if not fp.exists():
            continue
        finished = [f for f in jload(fp).get("fixtures", []) if f.get("status") == "FINISHED"]
        have = highlight_have(slug)
        for f in finished:
            if not have.get(f.get("match_id"), False):
                no_hl.append((comp, slug, to_fix(f)))

    # ---- run the matcher, detect alias gaps ------------------------------
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    candidates: dict = {}   # comp -> team -> [ {alias, evidence, occurrences} ]
    flags: list = []        # ambiguous / non-official-name short forms
    quota_used = 0
    quota_note = ""
    if yt_key and no_hl:
        quota = ReadOnlyQuota()
        gw_cache: dict = {}
        config = apply_discovered_overrides(load_sources())
        agg: dict = {}         # team -> {alias -> set(video "id | title")}
        seen_flag = set()
        try:
            for comp, slug, fix in no_hl:
                sink = []
                resolve_videos_for_fixture(fix, comp, config, yt_key, quota,
                                           INCREMENTAL_CAP, gw_playlist_cache=gw_cache,
                                           debug_sink=sink)
                by_vid = {}
                for rec in sink:
                    v = rec.get("video_id")
                    by_vid.setdefault(v, {"title": rec.get("title", ""), "reasons": []})
                    by_vid[v]["reasons"].append(rec.get("reason", ""))
                for vid, info in by_vid.items():
                    if not is_alias_miss(info["reasons"]):
                        continue
                    side = "home" if any("home-missing" in r for r in info["reasons"]) else "away"
                    fd_name = fix[f"{side}_team"]
                    cur = set(team_tokens(fd_name, fix[f"{side}_short"], fix[f"{side}_tla"]))
                    alias, is_multi = propose_alias(fd_name, info["title"], cur)
                    ev = f"{vid} | {info['title']}"
                    if alias and (is_multi or unique_alias(alias, fd_name)):
                        agg.setdefault(comp, {}).setdefault(fd_name, {}) \
                           .setdefault(alias, set()).add(ev)
                    else:
                        key = (fd_name, info["title"])
                        if key not in seen_flag:
                            seen_flag.add(key)
                            reason = ("ambiguous single-word (collides with another team)"
                                      if alias else
                                      "title short-form is not part of the official name")
                            flags.append({"competition": comp, "team": fd_name,
                                          "video": ev, "proposed": alias, "reason": reason})
        except QuotaCapReached as e:
            quota_note = f"Stopped early — quota cap reached: {e}. Results are partial."
        quota_used = quota.units_used
        for comp, teams in agg.items():
            for team, aliases in teams.items():
                rows = []
                for alias, evs in sorted(aliases.items(), key=lambda kv: -len(kv[1])):
                    evl = sorted(evs)
                    rows.append({
                        "alias": alias,
                        "occurrences": len(evl),
                        "current_tokens": team_tokens(team, "", ""),
                        "evidence": evl[:5],
                    })
                candidates.setdefault(comp, {})[team] = rows

    # ---- write proposals file (mirror channel-candidates.json) -----------
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "season": SEASON,
        "estimated_youtube_units": quota_used,
        "candidates": candidates,
        "flags": flags,
        "note": quota_note or ("blocked: no YOUTUBE_API_KEY" if not yt_key else ""),
    }
    with io.open(CANDIDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # ---- human-readable report -------------------------------------------
    out = [f"# Alias-gap detector — season {SEASON}\n",
           f"**Read-only. YouTube units: {quota_used}.** "
           f"Proposals -> `highlights/alias-candidates.json` (approve in the admin; "
           f"nothing auto-adopted).\n"]
    if not yt_key:
        out.append("BLOCKED: `YOUTUBE_API_KEY` not set — steps needing "
                   "`playlistItems.list` cannot run. Proposals empty.")
    elif not candidates and not flags:
        out.append("No alias gaps detected — every no-highlight fixture failed for "
                   "reasons other than a team-token short form (or there are none).")
    else:
        n = sum(len(rows) for teams in candidates.values() for rows in teams.values())
        out.append(f"## {n} safe proposal(s)\n")
        for comp, teams in sorted(candidates.items()):
            for team, rows in sorted(teams.items()):
                for r in rows:
                    out.append(f"- **{comp} · {team}** -> add alias `{r['alias']}` "
                               f"({r['occurrences']} video(s)); current: {r['current_tokens']}")
                    for e in r["evidence"]:
                        out.append(f"    - {e}")
        if flags:
            out.append(f"\n## {len(flags)} flag(s) needing manual judgement\n")
            for fl in flags:
                out.append(f"- **{fl['competition']} · {fl['team']}** — {fl['reason']}"
                           + (f" (proposed `{fl['proposed']}`)" if fl['proposed'] else "")
                           + f"\n    - {fl['video']}")
        if quota_note:
            out.append(f"\n> {quota_note}")

    report = "\n".join(out)
    report_out = os.environ.get("REPORT_OUT") or str(REPO / f"alias_gaps_{SEASON}.md")
    with io.open(report_out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    print(f"\n[proposals written to {CANDIDATES_PATH}]")
    print(f"[report written to {report_out}]")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with io.open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")


if __name__ == "__main__":
    main()
