#!/usr/bin/env python3
"""
source_health_scan.py — weekly, READ-ONLY health check of every configured
highlight source. It writes a report for a person (and later the Web Admin)
to act on, and it PROPOSES only: it never edits sources.json, the highlights
cache, the fixtures or the rights registry.

For the current season of each competition in sources.json:

  a. Sources — every configured source (competition channel uploads, broadcaster
     playlists, team playlists, club channel uploads), first page only
     (playlistItems.list, ≤50 items, 1 unit). Flags EMPTY, STALE (suppressed when
     the competition had no finished fixtures in the window — breaks and the
     off-season are not failures), UNREACHABLE (HTTP error / playlist gone).
  b. Title drift — the matcher's current title gate for that source kind over
     recent uploads. TITLE_DRIFT when there are recent uploads but none passes.
  c. Coverage — finished fixtures in the window vs. cached highlights.
     LOW_COVERAGE below the configured ratio.
  d. Region — up to N recent passing items per source through videos.list
     (fetch_video_details): per launch country playable count, embeddable count,
     items over MAX_VIDEO_DURATION_SECONDS. Same region rules as the matcher.
  e. Registry — rights/{season}.json: MISSING_SOURCE, OUTDATED_SOURCE,
     UNVERIFIED, NO_HOLDER per launch country.
  f. Proposals — recent passing uploads from approved registry channels that are
     not in sources.json.

YouTube API: playlistItems.list + videos.list only. Never search.list,
playlists.list or channels.list (a channel's uploads playlist is UC→UU), and
never API-Sports. Quota: its own budget (SCAN_BUDGET_UNITS, default 500) on top
of the shared daily tracker (highlights/quota-tracker.json); when either runs
low the scan stops, marks the report ``partial: true`` and lists what it skipped.

Output: reports/source-health/{season}/latest.json + {YYYY-MM-DD}.json (last 8
dated copies kept). Schema documented in README.md ("Source health scan").

Usage: python scripts/source_health_scan.py [--budget N] [--stale-days N]
       [--coverage-min-ratio R] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from highlights_common import (  # noqa: E402
    COMP_CHANNEL_REQUIRED_TERMS,
    COMP_CHANNEL_TITLE_TERMS,
    COMP_EXCLUSION_KEYWORDS,
    COMPETITION_SLUG_MAP,
    DOMESTIC_LEAGUE_COMPS,
    FIXTURES_DIR,
    HIGHLIGHTS_DIR,
    INCREMENTAL_CAP,
    LAUNCH_COUNTRIES,
    REPO_ROOT,
    SOURCES_JSON,
    SUMMER_TOURNAMENT_CYCLE,
    YT_PLAYLIST,
    QuotaCapReached,
    QuotaTracker,
    _normalize,
    _within_highlight_grace,
    channel_to_uploads,
    fetch_video_details,
    has_required_term,
    is_highlight_title,
    is_laliga_highlight_title,
    is_youth_reserve_title,
    load_json_file,
    load_sources,
    max_video_duration,
    region_is_playable_in,
    title_block_reason,
    utc_now_iso,
    write_json_atomic,
)
from playlist_discovery import apply_discovered_overrides  # noqa: E402
import rights_registry as rr  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ── Configuration (overridable by CLI flags) ──────────────────────────────────

SCAN_BUDGET_UNITS        = 500   # this scan's own YouTube budget per run (--budget)
STALE_DAYS               = 14    # STALE: no upload in this many days
RECENT_DAYS              = 14    # "recent" uploads + coverage window
COVERAGE_MIN_RATIO       = 0.8   # LOW_COVERAGE below this share of finished fixtures
REGION_SAMPLE_PER_SOURCE = 10    # recent passing items per source sent to videos.list
TITLE_DRIFT_MIN_RECENT   = 3     # need at least this many recent uploads to judge drift
DRIFT_SAMPLE_TITLES      = 5
PROPOSAL_SAMPLE_TITLES   = 5
KEEP_DATED_REPORTS       = 8
# Never push the shared day past the incremental fetch's cap: the scan stops
# before tracker.units_used would reach it.
DAILY_CEILING            = INCREMENTAL_CAP
REPORTS_DIR              = REPO_ROOT / "reports" / "source-health"

FLAGS = (
    "EMPTY", "STALE", "UNREACHABLE", "TITLE_DRIFT", "LOW_COVERAGE",
    "MISSING_SOURCE", "OUTDATED_SOURCE", "UNVERIFIED", "NO_HOLDER",
)

# Extra tier-2 gates the matcher applies to one competition channel
# (mirrors resolve_videos_for_fixture's LaLiga 'HIGHLIGHTS LALIGA' gate).
TIER2_EXTRA_GATES = {"LaLiga": is_laliga_highlight_title}

_DATED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


# ── Quota ─────────────────────────────────────────────────────────────────────


class ScanQuota:
    """Wraps the shared QuotaTracker with this scan's own budget.

    ``can_spend`` is checked BEFORE every API call; ``increment`` (the interface
    fetch_video_details already uses) records a spent unit in both the scan
    counter and the shared tracker (highlights/quota-tracker.json)."""

    def __init__(self, tracker, budget: int, daily_ceiling: int = DAILY_CEILING) -> None:
        self.tracker = tracker
        self.budget = budget
        self.daily_ceiling = daily_ceiling
        self.used = 0

    def can_spend(self, units: int = 1) -> bool:
        return (self.used + units <= self.budget
                and self.tracker.units_used + units < self.daily_ceiling)

    def why_not(self) -> str:
        if self.used >= self.budget:
            return "scan_budget"
        return "daily_quota"

    def increment(self, cap: int) -> None:
        self.used += 1
        self.tracker.increment(cap)


# ── YouTube client (playlistItems.list + videos.list only) ────────────────────


class YouTubeClient:
    def __init__(self, api_key: str, session: "requests.Session | None" = None) -> None:
        self.api_key = api_key
        self.session = session or requests.Session()

    def playlist_page(self, playlist_id: str) -> dict:
        """First page (≤50 items) of a playlist. Returns
        {"status": "ok"|"not_found"|"error", "http": int|None, "items": [...],
        "has_more": bool}. Items: {video_id, title, published (YYYY-MM-DD|None)}.
        Raises QuotaCapReached on HTTP 403 (YouTube's own quota)."""
        try:
            resp = self.session.get(YT_PLAYLIST, params={
                "part": "snippet,contentDetails", "playlistId": playlist_id,
                "maxResults": 50, "key": self.api_key,
            }, timeout=15)
        except requests.RequestException as exc:
            log.warning(f"network error on playlist {playlist_id}: {exc}")
            return {"status": "error", "http": None, "items": [], "has_more": False}
        if resp.status_code == 403:
            raise QuotaCapReached(f"YouTube 403 on playlist {playlist_id}")
        if resp.status_code == 404:
            return {"status": "not_found", "http": 404, "items": [], "has_more": False}
        if not resp.ok:
            return {"status": "error", "http": resp.status_code, "items": [], "has_more": False}
        data = resp.json()
        items = []
        for it in data.get("items", []):
            sn, cd = it.get("snippet", {}), it.get("contentDetails", {})
            vid = cd.get("videoId") or sn.get("resourceId", {}).get("videoId")
            # videoPublishedAt is the real upload date; it is absent for private /
            # deleted placeholders, which therefore never count as recent.
            pub = cd.get("videoPublishedAt")
            items.append({"video_id": vid, "title": sn.get("title", ""),
                          "published": pub[:10] if pub else None})
        return {"status": "ok", "http": 200, "items": items,
                "has_more": bool(data.get("nextPageToken"))}

    def video_details(self, video_ids: list[str], quota, cap: int) -> dict:
        return fetch_video_details(video_ids, self.api_key, quota, cap)


# ── Sources ───────────────────────────────────────────────────────────────────


def configured_sources(comp: str, config: dict, raw: dict) -> list[dict]:
    """Every source the matcher can read for [comp], as fetchable playlist IDs.

    Tier 2a per-matchday playlists are discovered by the matcher with
    playlists.list, which this scan must not call; the channel's uploads
    playlist (tier 2b) stands in for the channel."""
    out: list[dict] = []
    ch = config["competition_channels"].get(comp)
    if ch:
        out.append({"kind": "competition_channel", "tier": 2, "label": comp,
                    "channel_id": ch, "playlist_id": channel_to_uploads(ch)})
    for broadcaster, ids in config["competition_playlists"].get(comp, {}).items():
        for pid in ids:
            out.append({"kind": "broadcaster_playlist", "tier": 4, "label": broadcaster,
                        "playlist_id": pid})
    for team, pid in config["team_playlists"].get(comp, {}).items():
        out.append({"kind": "team_playlist", "tier": 1, "label": team, "playlist_id": pid})
    team_names = [t.get("name") for t in raw.get("teamLists", {}).get(comp, []) or []
                  if isinstance(t, dict)]
    for team in team_names:
        cid = config["team_channels"].get(team)
        if cid:
            out.append({"kind": "team_channel", "tier": 1, "label": team,
                        "channel_id": cid, "playlist_id": channel_to_uploads(cid)})
    return out


def title_passes(source_kind: str, comp: str, title: str) -> bool:
    """The matcher's current title gate for this source kind (source-scoped)."""
    if not title or is_youth_reserve_title(title):
        return False
    if source_kind == "competition_channel":
        if not is_highlight_title(title, COMP_CHANNEL_TITLE_TERMS.get(comp, ()),
                                  required_terms=COMP_CHANNEL_REQUIRED_TERMS.get(comp, ())):
            return False
        extra = TIER2_EXTRA_GATES.get(comp)
        return extra(title) if extra else True
    if source_kind in ("broadcaster_playlist", "team_playlist"):
        # Curated highlights playlists: the matcher skips the allowlist.
        return is_highlight_title(title, require_allowlist=False)
    # Club channel uploads (and unconfigured proposal sources): full gate plus the
    # competition-exclusion keywords the matcher applies to club channels.
    if not is_highlight_title(title):
        return False
    norm = _normalize(title)
    return not any(kw in norm for kw in COMP_EXCLUSION_KEYWORDS.get(comp, ()))


def _fail_reason(source_kind: str, comp: str, title: str) -> str:
    if is_youth_reserve_title(title):
        return "youth-reserve"
    blocked = title_block_reason(title)
    if blocked:
        return f"blocked:{blocked}"
    if source_kind == "competition_channel" and not has_required_term(
            title, COMP_CHANNEL_REQUIRED_TERMS.get(comp, ())):
        return "missing-required-term"
    return "no-allowlist-match"


# ── Fixtures + coverage ───────────────────────────────────────────────────────


def load_fixture_records(comp: str, slug: str, season: int) -> "list[dict] | None":
    """Cached fixture records for the competition season, or None when there is
    no fixture data at all (then breaks can't be told apart from failures)."""
    if comp in DOMESTIC_LEAGUE_COMPS:
        data = load_json_file(FIXTURES_DIR / slug / f"{season}.json")
        return None if data is None else list(data.get("fixtures") or [])
    data = load_json_file(REPO_ROOT / "tournament-groups" / f"{slug}.json")
    if data is None:
        return None
    if data.get("season") not in (None, season):
        return []
    return list(data.get("groupMatches") or []) + list(data.get("matches") or [])


def _fixture_date(f: dict) -> "date | None":
    ko = f.get("utcDate")
    try:
        return date.fromisoformat(str(ko)[:10]) if ko else None
    except ValueError:
        return None


def finished_in_window(fixtures: "list[dict] | None", today: date, days: int) -> "list[dict]":
    if not fixtures:
        return []
    start = today - timedelta(days=days)
    return [f for f in fixtures if f.get("status") == "FINISHED"
            and (d := _fixture_date(f)) is not None and start <= d <= today]


def highlight_match_ids(slug: str, season: int) -> set:
    have: set = set()
    for p in glob.glob(str(HIGHLIGHTS_DIR / slug / str(season) / "*.json")):
        for m in (load_json_file(Path(p)) or {}).get("matches", []) or []:
            if m.get("videos"):
                have.add(m.get("match_id"))
    return have


def coverage_for(comp: str, slug: str, season: int, fixtures, now: datetime,
                 days: int, min_ratio: float) -> dict:
    if fixtures is None:
        return {"window_days": days, "fixtures_known": False, "finished": 0,
                "awaiting": 0, "eligible": 0, "with_highlight": 0, "ratio": None,
                "flags": []}
    have = highlight_match_ids(slug, season)
    finished = finished_in_window(fixtures, now.date(), days)
    awaiting = [f for f in finished
                if f.get("match_id") not in have and not f.get("video_id")
                and _within_highlight_grace(f.get("utcDate"), now)]
    eligible = [f for f in finished if f not in awaiting]
    covered = [f for f in eligible if f.get("match_id") in have or f.get("video_id")]
    ratio = round(len(covered) / len(eligible), 3) if eligible else None
    flags = ["LOW_COVERAGE"] if ratio is not None and ratio < min_ratio else []
    return {"window_days": days, "fixtures_known": True, "finished": len(finished),
            "awaiting": len(awaiting), "eligible": len(eligible),
            "with_highlight": len(covered), "ratio": ratio, "min_ratio": min_ratio,
            "flags": flags,
            "missing_match_ids": sorted(f.get("match_id") for f in eligible if f not in covered)}


# ── Registry checks ───────────────────────────────────────────────────────────


def _previous_season(comp: str, season: int) -> int:
    cycle = SUMMER_TOURNAMENT_CYCLE.get(comp)
    return season - (cycle[1] if cycle else 1)


def registry_findings(comp: str, slug: str, season: int, today: date,
                      configured: dict, rights_dir=None) -> list[dict]:
    """[configured] maps playlist_id → source label for this competition."""
    season_entries = [e for e in rr.load_registry(season, rights_dir) if e["competition"] == slug]
    if not season_entries:
        return []  # competition not tracked in this season's registry
    findings: list[dict] = []
    active_all: list[dict] = []
    for cc in LAUNCH_COUNTRIES:
        act = rr.active_entries(slug, cc, today, rights_dir)
        active_all.extend(act)
        named = rr.holders(act)
        if not named:
            notes = [e.get("notes") for e in act if e.get("notes")]
            findings.append({"flag": "NO_HOLDER", "country": cc,
                             "note": notes[0] if notes else "no active registry entry"})
            continue
        unverified = [e for e in named if e["status"] == "unverified"]
        if unverified:
            findings.append({"flag": "UNVERIFIED", "country": cc,
                             "holders": [{"holder": e["holder"], "role": e["role"]}
                                         for e in unverified]})
        for e in named:
            if e["status"] != "approved":
                continue
            for sid in sorted(rr.entry_source_ids(e) - set(configured)):
                findings.append({"flag": "MISSING_SOURCE", "country": cc,
                                 "holder": e["holder"], "role": e["role"], "source_id": sid})
    # OUTDATED_SOURCE: a configured source linked (by ID) to a holder in this or
    # the previous season's registry, but no currently active entry lists it.
    linked: dict[str, set] = {}
    for s in (season, _previous_season(comp, season)):
        for e in rr.load_registry(s, rights_dir):
            if e["competition"] != slug:
                continue
            for sid in rr.entry_source_ids(e):
                linked.setdefault(sid, set()).add(e.get("holder") or "?")
    active_ids = set().union(*(rr.entry_source_ids(e) for e in active_all)) if active_all else set()
    for sid, label in sorted(configured.items()):
        if sid in linked and sid not in active_ids:
            findings.append({"flag": "OUTDATED_SOURCE", "source_id": sid, "label": label,
                             "linked_holders": sorted(linked[sid])})
    return findings


# ── Scan ──────────────────────────────────────────────────────────────────────


class _Stop(Exception):
    pass


def run_scan(
    *,
    client: "YouTubeClient | None",
    quota: ScanQuota,
    now: "datetime | None" = None,
    sources_raw: "dict | None" = None,
    config: "dict | None" = None,
    rights_dir=None,
    stale_days: int = STALE_DAYS,
    recent_days: int = RECENT_DAYS,
    coverage_min_ratio: float = COVERAGE_MIN_RATIO,
) -> dict:
    """Build the report dict. Pure apart from the injected client/quota and
    reading repo files; never writes config, cache or registry."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    if sources_raw is None:
        sources_raw = json.loads(Path(SOURCES_JSON).read_text(encoding="utf-8"))
    if config is None:
        config = apply_discovered_overrides(load_sources())

    skipped: list[dict] = []
    stop_reason: "str | None" = None
    page_cache: dict[str, dict] = {}
    details_cache: dict[str, dict] = {}

    def fetch_page(pid: str, comp: str, label: str) -> "dict | None":
        nonlocal stop_reason
        if pid in page_cache:
            return page_cache[pid]
        if client is None:
            skipped.append({"competition": comp, "source": label, "playlist_id": pid,
                            "reason": "no_api_key"})
            return None
        if stop_reason or not quota.can_spend(1):
            stop_reason = stop_reason or quota.why_not()
            skipped.append({"competition": comp, "source": label, "playlist_id": pid,
                            "reason": stop_reason})
            return None
        try:
            page = client.playlist_page(pid)
        except QuotaCapReached:
            stop_reason = "youtube_403"
            skipped.append({"competition": comp, "source": label, "playlist_id": pid,
                            "reason": stop_reason})
            return None
        if page["http"] is not None:  # the API answered (even 404) → 1 unit spent
            quota.increment(quota.daily_ceiling)
        page_cache[pid] = page
        return page

    def fetch_details(ids: list[str], comp: str) -> None:
        nonlocal stop_reason
        todo = [i for i in dict.fromkeys(ids) if i and i not in details_cache]
        for i in range(0, len(todo), 50):
            batch = todo[i:i + 50]
            if client is None or stop_reason or not quota.can_spend(1):
                if client is not None:
                    stop_reason = stop_reason or quota.why_not()
                skipped.append({"competition": comp, "videos_list_batch": len(batch),
                                "reason": "no_api_key" if client is None else stop_reason})
                continue
            try:
                got = client.video_details(batch, quota, quota.daily_ceiling)
            except QuotaCapReached:
                stop_reason = "youtube_403"
                skipped.append({"competition": comp, "videos_list_batch": len(batch),
                                "reason": stop_reason})
                continue
            for b in batch:
                details_cache[b] = got.get(b) or {"missing": True}

    competitions: dict[str, dict] = {}
    for comp in sources_raw.get("competitions", {}):
        slug = COMPETITION_SLUG_MAP.get(comp)
        if not slug:
            continue
        season = rr.resolve_season(comp, today)
        fixtures = load_fixture_records(comp, slug, season)
        recent_fixtures = finished_in_window(fixtures, today, stale_days)
        recent_start = today - timedelta(days=recent_days)
        stale_start = today - timedelta(days=stale_days)

        sources_out = []
        region_ids: dict[str, list[str]] = {}
        srcs = configured_sources(comp, config, sources_raw)
        for src in srcs:
            pid = src["playlist_id"]
            entry = {k: v for k, v in src.items()}
            page = fetch_page(pid, comp, src["label"])
            if page is None:
                entry.update({"checked": False, "flags": []})
                sources_out.append(entry)
                continue
            flags: list[str] = []
            items = page["items"]
            dated = [it for it in items if it["published"]]
            newest = max((it["published"] for it in dated), default=None)
            recent = [it for it in dated if it["published"] >= recent_start.isoformat()]
            passing = [it for it in recent if title_passes(src["kind"], comp, it["title"])]
            entry.update({"checked": True, "http_status": page["http"],
                          "item_count": len(items), "has_more": page["has_more"],
                          "newest_upload": newest, "recent_uploads": len(recent),
                          "recent_passing": len(passing)})
            if page["status"] != "ok":
                flags.append("UNREACHABLE")
            elif not items:
                flags.append("EMPTY")
            else:
                is_stale = newest is None or newest < stale_start.isoformat()
                ordered = pid.startswith("UU") or not page["has_more"]
                if is_stale:
                    if fixtures is None:
                        entry["stale_suppressed"] = "no_fixture_data"
                    elif not recent_fixtures:
                        entry["stale_suppressed"] = "no_finished_fixtures_in_window"
                    elif not ordered:
                        # A long non-uploads playlist is not newest-first, so its
                        # first page can't prove staleness.
                        entry["stale_suppressed"] = "first_page_not_newest_first"
                    else:
                        flags.append("STALE")
                if len(recent) >= TITLE_DRIFT_MIN_RECENT and not passing:
                    flags.append("TITLE_DRIFT")
                    entry["drift_samples"] = [
                        {"title": it["title"], "reason": _fail_reason(src["kind"], comp, it["title"])}
                        for it in recent[:DRIFT_SAMPLE_TITLES]]
            entry["flags"] = flags
            region_ids[pid] = [it["video_id"] for it in passing[:REGION_SAMPLE_PER_SOURCE]]
            sources_out.append(entry)

        # d. Region availability for the sampled passing items.
        fetch_details([v for ids in region_ids.values() for v in ids], comp)
        max_dur = max_video_duration(comp)
        comp_region = {cc: {"checked": 0, "playable": 0, "playable_and_embeddable": 0}
                       for cc in LAUNCH_COUNTRIES}
        for entry in sources_out:
            ids = [i for i in region_ids.get(entry["playlist_id"], [])
                   if i in details_cache and not details_cache[i].get("missing")]
            if not region_ids.get(entry["playlist_id"]):
                continue
            dets = [details_cache[i] for i in ids]
            reg = {"checked": len(dets),
                   "not_checked": len(region_ids[entry["playlist_id"]]) - len(dets),
                   "embeddable": sum(1 for d in dets if d.get("is_embeddable", True)),
                   "over_max_duration": sum(1 for d in dets if d.get("duration_seconds", 0) > max_dur),
                   "playable": {}}
            for cc in LAUNCH_COUNTRIES:
                ok = [d for d in dets if region_is_playable_in(d.get("region_restriction"), cc)]
                reg["playable"][cc] = len(ok)
                comp_region[cc]["checked"] += len(dets)
                comp_region[cc]["playable"] += len(ok)
                comp_region[cc]["playable_and_embeddable"] += sum(
                    1 for d in ok if d.get("is_embeddable", True))
            entry["region"] = reg

        configured_ids = {s["playlist_id"]: s["label"] for s in srcs}
        findings = registry_findings(comp, slug, season, today, configured_ids, rights_dir)

        # f. Proposals from approved registry channels not in sources.json.
        proposals = []
        for cc in LAUNCH_COUNTRIES:
            for e in rr.holders(rr.active_entries(slug, cc, today, rights_dir)):
                if e["status"] != "approved":
                    continue
                for sid in sorted(rr.entry_source_ids(e) - set(configured_ids)):
                    page = fetch_page(sid, comp, f"proposal:{e['holder']}")
                    if page is None or page["status"] != "ok":
                        continue
                    hits = [it for it in page["items"]
                            if it["published"] and it["published"] >= recent_start.isoformat()
                            and title_passes("proposal", comp, it["title"])]
                    if hits:
                        proposals.append({
                            "source_id": sid, "holder": e["holder"], "role": e["role"],
                            "country": cc, "recent_passing": len(hits),
                            "samples": [{"video_id": it["video_id"], "title": it["title"],
                                         "published": it["published"]}
                                        for it in hits[:PROPOSAL_SAMPLE_TITLES]]})

        competitions[comp] = {
            "slug": slug,
            "season": season,
            "fixtures_finished_in_stale_window": len(recent_fixtures),
            "sources": sources_out,
            "coverage": coverage_for(comp, slug, season, fixtures, now, recent_days,
                                     coverage_min_ratio),
            "region": comp_region,
            "registry_findings": findings,
            "proposals": proposals,
        }

    summary = {f: 0 for f in FLAGS}
    for c in competitions.values():
        for s in c["sources"]:
            for f in s["flags"]:
                summary[f] += 1
        for f in c["coverage"]["flags"]:
            summary[f] += 1
        for f in c["registry_findings"]:
            summary[f["flag"]] += 1
    summary["proposals"] = sum(len(c["proposals"]) for c in competitions.values())

    return {
        "schema_version": 1,
        "generated_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "report_season": rr.resolve_season("Premier League", today),
        "launch_countries": list(LAUNCH_COUNTRIES),
        "partial": bool(skipped),
        "stop_reason": stop_reason,
        "quota_used": quota.used,
        "quota_budget": quota.budget,
        "settings": {"stale_days": stale_days, "recent_days": recent_days,
                     "coverage_min_ratio": coverage_min_ratio,
                     "region_sample_per_source": REGION_SAMPLE_PER_SOURCE,
                     "title_drift_min_recent": TITLE_DRIFT_MIN_RECENT},
        "summary": summary,
        "skipped": skipped,
        "competitions": competitions,
    }


# ── Output ────────────────────────────────────────────────────────────────────


def write_report(report: dict, out_dir: "Path | None" = None,
                 keep: int = KEEP_DATED_REPORTS) -> list[Path]:
    """Write latest.json + a dated copy under {out_dir}/{report_season}/ and prune
    dated copies beyond the newest [keep]. Returns the written paths."""
    base = Path(out_dir or REPORTS_DIR) / str(report["report_season"])
    base.mkdir(parents=True, exist_ok=True)
    day = report["generated_at"][:10]
    paths = [base / "latest.json", base / f"{day}.json"]
    for p in paths:
        write_json_atomic(p, report)
    dated = sorted(p for p in base.iterdir() if _DATED_RE.match(p.name))
    for old in dated[:-keep] if keep > 0 else dated:
        old.unlink()
        log.info(f"pruned old report {old.name}")
    return paths


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--budget", type=int, default=SCAN_BUDGET_UNITS)
    ap.add_argument("--stale-days", type=int, default=STALE_DAYS)
    ap.add_argument("--recent-days", type=int, default=RECENT_DAYS)
    ap.add_argument("--coverage-min-ratio", type=float, default=COVERAGE_MIN_RATIO)
    ap.add_argument("--out-dir", type=Path, default=REPORTS_DIR)
    args = ap.parse_args(argv)

    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    client = YouTubeClient(key) if key else None
    if client is None:
        log.warning("YOUTUBE_API_KEY not set — source checks skipped; report will be partial")
    quota = ScanQuota(QuotaTracker(), args.budget)
    report = run_scan(client=client, quota=quota, stale_days=args.stale_days,
                      recent_days=args.recent_days, coverage_min_ratio=args.coverage_min_ratio)
    paths = write_report(report, args.out_dir)
    log.info(f"source health: partial={report['partial']} quota_used={report['quota_used']}"
             f"/{report['quota_budget']} summary={report['summary']}")
    for p in paths:
        log.info(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
