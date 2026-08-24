#!/usr/bin/env python3
"""
find_channel_candidates.py — off-season, MANUAL channel candidate finder.

For teams the missing-channel detector flagged as lacking an OWN highlight channel
(playlist_discovery.compute_missing_channels), search YouTube for the club's
official channel, rank + ownership-check the candidates, and publish
highlights/channel-candidates.json for a HUMAN to approve later in the admin.

═══════════════════════════════════════════════════════════════════════════════
 THE search.list EXCEPTION — READ BEFORE EDITING
═══════════════════════════════════════════════════════════════════════════════
This is the ONE and ONLY sanctioned caller of YouTube ``search.list`` in the whole
pipeline. search.list is FORBIDDEN everywhere else: it costs 100 units/call (vs 1
for playlistItems.list / playlists.list) and returns a RANKED GUESS stuffed with
fan / re-uploader / pirate channels. Finding an UNKNOWN channel is the single thing
playlistItems.list / playlists.list cannot do — they need a known channel/playlist
id — so search.list is the only mechanism, and its output must therefore be RANKED
CANDIDATES FOR HUMAN CONFIRMATION, never an auto-pick.

Hard guardrails (all enforced below):
  • STANDALONE + MANUAL: run only via the find-channel-candidates workflow
    (workflow_dispatch). This module MUST NEVER be imported by fetch_highlights
    (the 4-hour incremental pipeline) or discover_season_playlists, and MUST NEVER
    be put on an in-season schedule. See _assert_not_incremental().
  • FLAGGED TEAMS ONLY: searches only teams compute_missing_channels flagged for
    AVAILABLE competitions — never all teams, never a sweep, never EL/Copa.
  • HARD SEARCH CAP: at most MAX_SEARCHES search.list calls per run (also clamped
    to a safe fraction of the daily quota). Pre-run estimate printed; on cap/quota
    exhaustion the run aborts CLEANLY and writes whatever was gathered so far.
  • PROPOSES ONLY: writes highlights/channel-candidates.json. It does NOT write
    sources.json, does NOT modify channel mappings, does NOT auto-adopt. Human
    approval happens later in the admin (a separate build).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlights_common import (
    HIGHLIGHTS_DIR,
    SOURCES_JSON,
    load_json_file,
    utc_now_iso,
    write_json_atomic,
)
from playlist_discovery import (
    DISCOVERED_PATH,
    available_competitions,
    compute_missing_channels,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("find_channel_candidates")

CANDIDATES_PATH = HIGHLIGHTS_DIR / "channel-candidates.json"

# YouTube Data API v3 endpoints. YT_SEARCH is intentionally defined ONLY here —
# it lives nowhere in the shared modules so no other script can accidentally reach
# for search.list.
YT_SEARCH   = "https://www.googleapis.com/youtube/v3/search"
YT_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"

# Quota profile.
SEARCH_UNIT_COST   = 100     # search.list — the expensive call (this whole file's reason to be careful)
CHANNELS_UNIT_COST = 1       # channels.list — cheap; batched up to 50 ids/call
DAILY_QUOTA        = 10_000  # default YouTube Data API v3 daily quota
QUOTA_SAFE_FRACTION = 0.5    # never spend more than half the day's budget on this off-season job
MAX_SEARCHES       = 50      # hard ceiling on search.list calls per run (default; --max-searches overrides)

TOP_N            = 3         # candidates emitted per team
MIN_PLAUSIBLE    = 0.30      # below this best-score → team goes to flags[] instead of candidates
SEARCH_RESULTS   = 5         # results requested per search.list call (still 1 call = 100 units)

# Club-name qualifier tokens dropped before similarity scoring (legal-form suffixes,
# connectors) — accent-folded, lowercase.
_CLUB_STOPWORDS = {
    "fc", "cf", "afc", "sc", "ac", "cd", "ss", "ssc", "us", "sv", "fk", "aj",
    "es", "rc", "ca", "as", "sd", "ud", "rcd", "calcio", "club", "de", "la",
    "el", "the", "und", "e", "v", "07",
}

# Clear fan / re-uploader / pirate signals in a channel title → strong down-rank.
# NOTE: deliberately excludes "tv"/"hd" as HARD fakes — many LEGIT club channels are
# "<Club> TV" (Real Madrid TV, MUTV, LFCTV). Those are handled as MILD signals only
# when the name barely matches.
_FAKE_HARD = ("fan", "fanpage", "fans", "unofficial", "fancam", "edits",
              "reupload", "re-upload", "not official", "parody", "tribute")
_FAKE_MILD = ("hd", "highlights", "clips", "world", "news", "updates", "zone")


def _assert_not_incremental() -> None:
    """Fail loudly if this module was pulled into the incremental/discovery pipeline.
    search.list belongs ONLY to this standalone, manually-dispatched job."""
    forbidden = {"fetch_highlights", "discover_season_playlists"}
    loaded = forbidden & set(sys.modules)
    if loaded:
        raise RuntimeError(
            f"find_channel_candidates (the sanctioned search.list job) must never be "
            f"imported by the incremental pipeline, but found: {sorted(loaded)}. "
            f"search.list is 100 units/call and is off-season/manual only.")


# ── Pure ranking logic (no network — fully unit-testable) ───────────────────────

def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def _tokens(name: str) -> "set[str]":
    """Significant, accent-folded tokens of a club/channel name (stopwords dropped)."""
    words = re.findall(r"[a-z0-9]+", _fold(name))
    sig = {w for w in words if w not in _CLUB_STOPWORDS and len(w) > 1}
    return sig or set(words)  # fall back to raw words if everything was a stopword


def score_candidate(team_name: str, cand: dict) -> "tuple[float, str]":
    """Rank one candidate channel for one club. Returns (score, evidence).

    Signals: club-name token coverage in the channel title (primary), verification
    (bonus when known True), subscriber magnitude (small bonus), and fan/re-uploader
    down-ranks. The evidence string SURFACES the reasoning — including why a likely
    fake is down-ranked — so the human approver can judge the ambiguity, never hides it.
    """
    club = _tokens(team_name)
    title = cand.get("channelTitle") or ""
    ctoks = _tokens(title)
    overlap = len(club & ctoks)
    name_sim = overlap / len(club) if club else 0.0

    score = name_sim
    parts = [f"name match {name_sim:.2f} ({overlap}/{len(club)} club tokens in {title!r})"]

    if name_sim >= 1.0:
        score += 0.10
        parts.append("all club tokens present")

    verified = cand.get("verified")
    if verified is True:
        score += 0.25
        parts.append("verified ✓")
    elif verified is False:
        parts.append("unverified")

    subs = cand.get("subscriberCount")
    if isinstance(subs, int) and subs > 0:
        import math
        score += min(0.15, math.log10(subs + 1) / 8.0)
        parts.append(f"{subs:,} subs")
    else:
        parts.append("subs hidden/unknown")

    folded_title = _fold(title)
    hard = [t for t in _FAKE_HARD if t in folded_title]
    mild = [t for t in _FAKE_MILD if re.search(rf"\b{re.escape(t)}\b", folded_title)]
    if hard:
        score -= 0.40
        parts.append(f"contains {hard} → likely fan/re-uploader (down-ranked)")
    # Mild signals only bite when the name match is weak (a strong-name-match "TV"
    # channel is probably the real club channel, so don't punish it).
    if mild and name_sim < 1.0:
        score -= 0.15
        parts.append(f"generic term {mild} with partial name → possibly not official")

    return round(score, 4), "; ".join(parts)


def rank_candidates(team_name: str, raw_cands: "list[dict]",
                    top_n: int = TOP_N) -> "list[dict]":
    """Score + sort candidates for a team, returning the top_n in the locked schema
    (highest score first; ties broken by subscriber count). Empty in → empty out."""
    scored = []
    for c in raw_cands:
        s, evidence = score_candidate(team_name, c)
        cid = c.get("channelId") or ""
        scored.append({
            "channelId":       cid,
            "channelTitle":    c.get("channelTitle") or "",
            "url":             f"https://youtube.com/channel/{cid}" if cid else "",
            "thumbnail":       c.get("thumbnail"),
            "subscriberCount": c.get("subscriberCount") if isinstance(c.get("subscriberCount"), int) else None,
            "verified":        c.get("verified") if isinstance(c.get("verified"), bool) else None,
            "score":           s,
            "evidence":        evidence,
        })
    scored.sort(key=lambda e: (e["score"], e["subscriberCount"] or 0), reverse=True)
    return scored[:top_n]


def _effective_search_cap(max_searches: int) -> int:
    """Clamp the requested search cap to a safe fraction of the daily quota."""
    quota_cap = int(DAILY_QUOTA * QUOTA_SAFE_FRACTION) // SEARCH_UNIT_COST
    return max(0, min(max_searches, quota_cap))


def _select_missing_teams(sources_raw: dict, available: "set[str]",
                          *, actionable_only: bool) -> "list[dict]":
    """Flagged missing-own teams for available competitions. Prefers the detector's
    committed missing_channels[]; falls back to recomputing from config (read-only).
    Returns [{competition, team, covered_via_other_tier}], sorted actionable-first."""
    report = load_json_file(DISCOVERED_PATH)
    missing = None
    if isinstance(report, dict) and isinstance(report.get("missing_channels"), list) \
            and report["missing_channels"]:
        missing = [m for m in report["missing_channels"]
                   if m.get("competition") in available]
        log.info("Using missing_channels[] from %s (%d entries in-scope)",
                 DISCOVERED_PATH.name, len(missing))
    if not missing:
        missing = compute_missing_channels(sources_raw, available)
        log.info("Re-derived missing-own set from config (compute_missing_channels): "
                 "%d entries", len(missing))
    if actionable_only:
        missing = [m for m in missing if not m.get("covered_via_other_tier")]
    # actionable (covered=False) first, then covered; deterministic within each.
    missing.sort(key=lambda m: (bool(m.get("covered_via_other_tier")),
                                m.get("competition", ""), m.get("team", "")))
    return missing


def find_candidates(sources_raw: dict, available: "set[str]", *, searcher, enricher,
                    max_searches: int = MAX_SEARCHES, top_n: int = TOP_N,
                    actionable_only: bool = False,
                    min_plausible: float = MIN_PLAUSIBLE) -> dict:
    """Core orchestrator (network injected as ``searcher`` / ``enricher`` so it is
    fully testable offline).

    ``searcher(team_name) -> list[{channelId, channelTitle, thumbnail}]`` — ONE
    search.list call (100 units). ``enricher(channel_ids) -> {id: {subscriberCount,
    verified, channelTitle, thumbnail}}`` — cheap channels.list enrichment.

    Enforces the hard search cap: once ``max_searches`` (clamped to a safe quota
    fraction) is reached, remaining teams are recorded in flags[] as unsearched and
    the report is returned with partial results — never blowing the budget.
    """
    teams = _select_missing_teams(sources_raw, available,
                                  actionable_only=actionable_only)
    cap = _effective_search_cap(max_searches)
    estimate = len(teams) * SEARCH_UNIT_COST
    planned = min(len(teams), cap) * SEARCH_UNIT_COST
    log.info("Missing-own teams to search: %d | hard cap: %d searches | "
             "estimated cost if all searched: %d units (~%d planned within cap)",
             len(teams), cap, estimate, planned)

    candidates: "dict[str, dict]" = {}
    flags: "list[dict]" = []
    searches_done = 0

    for m in teams:
        comp, team = m["competition"], m["team"]
        if searches_done >= cap:
            flags.append({"competition": comp, "team": team,
                          "reason": "search_cap_reached_not_searched",
                          "covered_via_other_tier": bool(m.get("covered_via_other_tier"))})
            continue

        raw = searcher(team) or []
        searches_done += 1

        # Cheap enrichment of the returned channels (subs / verification / title).
        ids = [r.get("channelId") for r in raw if r.get("channelId")]
        meta = enricher(ids) if ids else {}
        for r in raw:
            info = meta.get(r.get("channelId")) or {}
            # Prefer enriched title/thumbnail; keep search snippet as fallback.
            r["channelTitle"]    = info.get("channelTitle") or r.get("channelTitle")
            r["thumbnail"]       = info.get("thumbnail") or r.get("thumbnail")
            r["subscriberCount"] = info.get("subscriberCount", r.get("subscriberCount"))
            r["verified"]        = info.get("verified", r.get("verified"))

        ranked = rank_candidates(team, raw, top_n=top_n)
        plausible = [c for c in ranked if c["score"] >= min_plausible]
        if plausible:
            candidates.setdefault(comp, {})[team] = plausible
        else:
            flags.append({"competition": comp, "team": team,
                          "reason": "no_plausible_candidate",
                          "best_score": ranked[0]["score"] if ranked else None})

    actual_search_units = searches_done * SEARCH_UNIT_COST
    log.info("search.list calls made: %d → %d units (cap %d). Teams flagged: %d.",
             searches_done, actual_search_units, cap, len(flags))

    return {
        "generated_at":          utc_now_iso(),
        "estimated_search_units": actual_search_units,
        "candidates":            candidates,
        "flags":                 flags,
    }


# ── Real network calls (the ONLY search.list in the codebase) ───────────────────

def _make_searcher(api_key: str, session: requests.Session):
    """Build a real search.list-backed searcher. THIS is the sanctioned exception:
    ``type=channel`` search for the club's official channel. 100 units per call."""
    def searcher(team_name: str) -> "list[dict]":
        params = {
            "part": "snippet", "type": "channel", "maxResults": SEARCH_RESULTS,
            "q": f"{team_name} official", "key": api_key,
        }
        resp = session.get(YT_SEARCH, params=params, timeout=30)
        resp.raise_for_status()
        out = []
        for it in resp.json().get("items", []):
            sn = it.get("snippet") or {}
            cid = (it.get("id") or {}).get("channelId") or sn.get("channelId")
            if not cid:
                continue
            thumbs = sn.get("thumbnails") or {}
            out.append({
                "channelId":    cid,
                "channelTitle": sn.get("channelTitle") or sn.get("title") or "",
                "thumbnail":    (thumbs.get("high") or thumbs.get("default") or {}).get("url"),
            })
        return out
    return searcher


def _make_enricher(api_key: str, session: requests.Session):
    """channels.list enrichment (1 unit/call, batched ≤50 ids): subscriberCount,
    title, thumbnail. The Data API exposes no reliable verification badge, so
    ``verified`` is left None here (the schema permits null); ranking copes."""
    def enricher(channel_ids: "list[str]") -> "dict[str, dict]":
        out: "dict[str, dict]" = {}
        ids = [c for c in channel_ids if c]
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            resp = session.get(YT_CHANNELS, params={
                "part": "snippet,statistics", "id": ",".join(batch),
                "maxResults": 50, "key": api_key,
            }, timeout=30)
            resp.raise_for_status()
            for it in resp.json().get("items", []):
                sn = it.get("snippet") or {}
                st = it.get("statistics") or {}
                thumbs = sn.get("thumbnails") or {}
                subs = None
                if not st.get("hiddenSubscriberCount") and "subscriberCount" in st:
                    try:
                        subs = int(st["subscriberCount"])
                    except (TypeError, ValueError):
                        subs = None
                out[it["id"]] = {
                    "channelTitle":    sn.get("title") or "",
                    "thumbnail":       (thumbs.get("high") or thumbs.get("default") or {}).get("url"),
                    "subscriberCount": subs,
                    "verified":        None,   # no public verification field in Data API
                }
        return out
    return enricher


def main(argv=None) -> None:
    _assert_not_incremental()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-searches", type=int, default=MAX_SEARCHES,
                    help=f"hard cap on search.list calls (default {MAX_SEARCHES}; "
                         f"also clamped to {int(QUOTA_SAFE_FRACTION*100)}%% of daily quota)")
    ap.add_argument("--actionable-only", action="store_true",
                    help="search only actionable (covered_via_other_tier=False) teams "
                         "(default: ALL missing-own teams, per 'own channels everywhere')")
    ap.add_argument("--top-n", type=int, default=TOP_N)
    args = ap.parse_args(argv)

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        log.error("YOUTUBE_API_KEY is not set — the candidate finder needs YouTube access")
        sys.exit(1)

    sources_raw = json.load(open(SOURCES_JSON, encoding="utf-8"))
    available = available_competitions()
    log.info("Available competitions (scope): %s", sorted(available))
    log.info("⚠ This is the ONLY sanctioned search.list job (100 units/call), "
             "off-season + manual, proposes-not-adopts.")

    session = requests.Session()
    searcher = _make_searcher(api_key, session)
    enricher = _make_enricher(api_key, session)

    try:
        report = find_candidates(
            sources_raw, available, searcher=searcher, enricher=enricher,
            max_searches=args.max_searches, top_n=args.top_n,
            actionable_only=args.actionable_only)
    except requests.HTTPError as exc:
        # Clean abort (e.g. HTTP 403 quota exhaustion) — never crash mid-budget.
        log.error("YouTube API error (%s) — aborting; no partial file written this run.", exc)
        sys.exit(1)

    write_json_atomic(CANDIDATES_PATH, report)
    n_teams = sum(len(v) for v in report["candidates"].values())
    log.info("Wrote %s: %d teams with candidates, %d flags, %d search units.",
             CANDIDATES_PATH.name, n_teams, len(report["flags"]),
             report["estimated_search_units"])
    log.info("PROPOSALS ONLY — sources.json untouched. Approve in the admin to adopt.")


if __name__ == "__main__":
    main()
