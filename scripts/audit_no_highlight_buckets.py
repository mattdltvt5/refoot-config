#!/usr/bin/env python3
"""READ-ONLY three-bucket classifier for FINISHED-but-no-highlight fixtures.

ONE-OFF diagnostic. Designed to run either locally or via the
`audit-no-highlight-buckets.yml` workflow (which supplies YOUTUBE_API_KEY from
the repo secret). WRITES NOTHING to the repo: no cache files, no
quota-tracker.json, no commit, no workflow dispatch, no channel adoption. The
only output is a markdown report printed to stdout and written to REPORT_OUT
(a path OUTSIDE the cache tree; defaults to the repo root, which in CI is the
ephemeral checkout and is never committed).

Steps 1-2 (cache-only): enumerate FINISHED fixtures for SEASON across the 5
domestic leagues and split HAS-HIGHLIGHT vs NO-HIGHLIGHT via the
  match_id -> non-empty videos[]
join over highlights/{slug}/{SEASON}/*.json.

Steps 3-4 (YouTube, gated on YOUTUBE_API_KEY): for each NO-HIGHLIGHT fixture,
re-run the pipeline's own matcher `resolve_videos_for_fixture(...)` with a
per-fixture debug_sink, then classify the fixture into EXACTLY ONE bucket from
the sink's rejection reasons. The matcher is reused verbatim so the per-source
title scoping (e.g. the LaLiga "HIGHLIGHTS LALIGA" filter that only applies on
LaLiga-channel gameweek playlists) is faithful by construction -- we do NOT
re-implement or loosen it.

READ-ONLY enforcement:
  * The real QuotaTracker persists highlights/quota-tracker.json on construction;
    this script substitutes ReadOnlyQuota, which never touches disk and only
    counts units in memory (printed, never saved).
  * playlistItems.list only (inherited from search_playlist); search.list is
    never called by the pipeline. Stops cleanly and reports partial results if
    the cap is reached. No account multiplication.

BUCKET MAPPING (from search_playlist / resolve_videos_for_fixture sink reasons):
  matched-but-filtered  <- a real candidate was matched then dropped by a filter:
        title-filter:blocked:*        (is_highlight_title blocklist: press conf /
                                        reaction / preview / analysis, 11 langs)
        too-short:* / portrait-video / region-restricted / comp-exclusion:* /
        youth-reserve                 (passed title+team, dropped downstream)
        passed-search-filter          (cleared title+team but dropped later, e.g.
                                        embeddability) for a NO-HIGHLIGHT fixture
  no-title-match        <- a candidate video exists in the playlist but was not
        joined because the title/parse/join did not resolve to THIS fixture:
        title-filter:no-allowlist-match  (title isn't a recognisable highlight)
        cross-match-guard:* / no-token-overlap  (match_id/team join failed)
        outside-date-window:* / no-comp-keyword
  truly-absent          <- no candidate video for this fixture in ANY source
        playlist tier (the sink recorded nothing at all).

Fixture-level precedence when candidates disagree: matched-but-filtered >
no-title-match > truly-absent (a genuine-but-dropped highlight is the most
actionable signal, and every candidate disposition is printed as evidence).
"""
import json, glob, os, io, sys
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
SEASON = int(os.environ.get("SEASON_OVERRIDE", "2025"))
DOMESTIC = {
    "premier-league": "Premier League", "laliga": "LaLiga", "serie-a": "Serie A",
    "bundesliga": "Bundesliga", "ligue-1": "Ligue 1",
}
sys.path.insert(0, str(REPO / "scripts"))

from highlights_common import (          # noqa: E402
    load_sources, resolve_videos_for_fixture, QuotaTracker, QuotaCapReached,
    INCREMENTAL_CAP,
)
from playlist_discovery import apply_discovered_overrides  # noqa: E402


class ReadOnlyQuota(QuotaTracker):
    """QuotaTracker interface with ZERO disk I/O (no read of, no write to
    highlights/quota-tracker.json). Counts units in memory only."""
    def __init__(self) -> None:
        self.date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.units_used = 0
    def _save(self) -> None:            # belt-and-suspenders: never persist
        pass


def jload(p):
    with io.open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def highlight_have(slug):
    """match_id -> True iff it has >=1 joined video across the season files."""
    have = {}
    for p in glob.glob(str(REPO / "highlights" / slug / str(SEASON) / "*.json")):
        for m in jload(p).get("matches", []):
            mid = m.get("match_id")
            if mid is None:
                continue
            have[mid] = have.get(mid, False) or bool(m.get("videos"))
    return have


def to_fix(f):
    """Cache fixture -> provider `fix` dict resolve_videos_for_fixture expects."""
    h, a = f.get("homeTeam") or {}, f.get("awayTeam") or {}
    return {
        "match_id":   f.get("match_id"),
        "home_team":  h.get("name", ""),  "home_short": h.get("shortName", ""),
        "home_tla":   h.get("tla", ""),
        "away_team":  a.get("name", ""),  "away_short": a.get("shortName", ""),
        "away_tla":   a.get("tla", ""),
        "date":       (f.get("utcDate") or "")[:10],
        "matchday":   f.get("matchday"),
    }


FILTERED_PREFIXES = ("title-filter:blocked", "too-short", "portrait-video",
                     "region-restricted", "comp-exclusion", "youth-reserve")
NOTITLE_EXACT = {"title-filter:no-allowlist-match", "no-token-overlap",
                 "no-comp-keyword"}
NOTITLE_PREFIXES = ("cross-match-guard", "outside-date-window")


def disposition(reasons):
    """Reduce one candidate video's set of sink reasons to a bucket signal."""
    rs = list(reasons)
    if any(any(r.startswith(p) for p in FILTERED_PREFIXES) for r in rs):
        return "matched-but-filtered"
    if "passed-search-filter" in rs:
        return "matched-but-filtered"
    if any(r in NOTITLE_EXACT or any(r.startswith(p) for p in NOTITLE_PREFIXES)
           for r in rs):
        return "no-title-match"
    return "no-title-match"


def classify(sink):
    """sink (list of records) -> (bucket, per-candidate evidence list)."""
    if not sink:
        return "truly-absent", []
    by_vid = {}
    for rec in sink:
        vid = rec.get("video_id")
        by_vid.setdefault(vid, {"title": rec.get("title", ""), "reasons": []})
        by_vid[vid]["reasons"].append(rec.get("reason", ""))
    evidence, dispo = [], []
    for vid, info in by_vid.items():
        d = disposition(info["reasons"])
        dispo.append(d)
        evidence.append({"video_id": vid, "title": info["title"],
                         "reasons": info["reasons"], "dispo": d})
    if "matched-but-filtered" in dispo:
        bucket = "matched-but-filtered"
    elif "no-title-match" in dispo:
        bucket = "no-title-match"
    else:
        bucket = "truly-absent"
    return bucket, evidence


# ---- steps 1-2 -----------------------------------------------------------
totals, no_hl_fixtures = {}, []
for slug, comp in DOMESTIC.items():
    fp = REPO / "fixtures" / slug / f"{SEASON}.json"
    if not fp.exists():
        totals[comp] = (0, 0, 0, "no fixtures cache")
        continue
    finished = [f for f in jload(fp).get("fixtures", []) if f.get("status") == "FINISHED"]
    have = highlight_have(slug)
    no_hl = [f for f in finished if not have.get(f.get("match_id"), False)]
    totals[comp] = (len(finished), len(finished) - len(no_hl), len(no_hl), "")
    for f in no_hl:
        no_hl_fixtures.append((comp, slug, to_fix(f), f.get("match_id") in have))

# ---- steps 3-4 (gated) ---------------------------------------------------
yt_key = os.environ.get("YOUTUBE_API_KEY")
classified = []
quota_used = 0
quota_note = ""
if yt_key and no_hl_fixtures:
    quota = ReadOnlyQuota()
    gw_cache = {}
    config = apply_discovered_overrides(load_sources())
    try:
        for comp, slug, fix, in_file in no_hl_fixtures:
            sink = []
            resolve_videos_for_fixture(fix, comp, config, yt_key, quota,
                                       INCREMENTAL_CAP, gw_playlist_cache=gw_cache,
                                       debug_sink=sink)
            bucket, evidence = classify(sink)
            classified.append((comp, slug, fix, in_file, bucket, evidence))
    except QuotaCapReached as e:
        quota_note = f"\n\n> **Stopped early — YouTube quota cap reached: {e}.** " \
                     f"Fixtures below this point are unclassified."
    quota_used = quota.units_used

# ---- report --------------------------------------------------------------
out = []
out.append(f"# FINISHED-but-no-highlight audit — 3-bucket classification (season {SEASON})\n")
out.append(f"**Read-only. Zero cache writes. No workflow triggered. "
           f"YouTube quota consumed: {quota_used} units.**\n")

out.append("## Steps 1-2 — FINISHED vs joined-highlight (cache-only, domestic 5)\n")
out.append("| Competition | FINISHED | has-highlight | **NO-HIGHLIGHT** | note |")
out.append("|---|--:|--:|--:|---|")
tf = th = tn = 0
for comp in DOMESTIC.values():
    fin, hl, no, note = totals[comp]
    tf += fin; th += hl; tn += no
    out.append(f"| {comp} | {fin} | {hl} | **{no}** | {note} |")
out.append(f"| **TOTAL** | **{tf}** | **{th}** | **{tn}** | |")
out.append("")

if not no_hl_fixtures:
    out.append("_No NO-HIGHLIGHT fixtures — nothing to classify._")
elif not yt_key:
    out.append("## Steps 3-4 — bucket classification: **BLOCKED (no `YOUTUBE_API_KEY`)**\n")
    out.append("`YOUTUBE_API_KEY` is not set in this environment. Steps 3-4 need "
               "`playlistItems.list`; quota consumed = 0 and no bucket is assigned.\n")
    out.append(f"### The {tn} NO-HIGHLIGHT fixtures awaiting classification\n")
    out.append("| Competition | GW | Date | Match | match_id | cache signal |")
    out.append("|---|--:|---|---|--:|---|")
    for comp, slug, fix, in_file in sorted(
            no_hl_fixtures, key=lambda x: (x[0], x[2]["date"] or "", x[2]["match_id"] or 0)):
        sig = "present, videos[] empty" if in_file else "absent (not processed)"
        out.append(f"| {comp} | {fix['matchday']} | {fix['date']} | "
                   f"{fix['home_team']} vs {fix['away_team']} | {fix['match_id']} | {sig} |")
else:
    buckets = {"matched-but-filtered": [], "no-title-match": [], "truly-absent": []}
    for row in classified:
        buckets[row[4]].append(row)
    out.append("## Steps 3-4 — bucket totals\n")
    out.append("| Bucket | Count |")
    out.append("|---|--:|")
    for b in ("no-title-match", "matched-but-filtered", "truly-absent"):
        out.append(f"| {b} | {len(buckets[b])} |")
    out.append(f"| **classified** | **{len(classified)}** of {tn} |")
    out.append(quota_note)
    for b in ("no-title-match", "matched-but-filtered", "truly-absent"):
        rows = buckets[b]
        out.append(f"\n## Bucket: {b} ({len(rows)})\n")
        for comp, slug, fix, in_file, bucket, evidence in sorted(
                rows, key=lambda x: (x[0], x[2]["date"] or "")):
            out.append(f"### {comp} GW{fix['matchday']} · {fix['date']} · "
                       f"{fix['home_team']} vs {fix['away_team']} (match_id {fix['match_id']})")
            if not evidence:
                out.append("- _no candidate video in any source playlist tier._")
            for ev in evidence:
                out.append(f"- `{ev['dispo']}` — [{ev['video_id']}] \"{ev['title']}\"  "
                           f"→ reasons: {', '.join(ev['reasons'])}")
            out.append("")

report = "\n".join(out)
report_out = os.environ.get("REPORT_OUT") or str(REPO / f"audit_no_highlight_buckets_{SEASON}.md")
with io.open(report_out, "w", encoding="utf-8") as f:
    f.write(report)
print(report)
print(f"\n[report written to {report_out}]")
print(f"[read-only check] YOUTUBE_API_KEY set: {bool(yt_key)}  quota_used: {quota_used}")

# GitHub Actions: also surface the report in the run summary.
summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with io.open(summary, "a", encoding="utf-8") as f:
        f.write(report + "\n")
