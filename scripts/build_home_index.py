"""Assemble the cross-competition, date-indexed Home artifact (backlog #8).

DERIVE-ONLY: this reads data the pipeline has ALREADY cached and writes a new
derived artifact. It makes ZERO external API calls (no football-data, no
API-Sports, no YouTube). It only READS:
  - fixtures/{slug}/{season}.json      (5 domestic leagues; no video_id — joined below)
  - highlights/{slug}/{season}/*.json  (domestic match_id -> video_id join)
  - tournament-groups/{slug}.json      (UCL/UEL/Euro/WC/Copa; video_id embedded)

and WRITES per-month index files:
  - home-index/{YYYY-MM}.json          (that month's dates -> competitions -> matches)
  - home-index/index.json              (manifest: available months + canonical comps)
  - fixtures/seasons.json              (season years, newest first: "seasons" for every
                                        league's fixtures/{slug}/{year}.json files and
                                        "tournament_archives" for tournament-groups/
                                        {slug}/{year}.json, "events" for events/{slug}/
                                        {year}.json - the app reads it instead of
                                        requesting files that may not exist)

Design notes
------------
* UTC bucketing. Each match is placed on its UTC calendar date (match utcDate[:10]).
  The pipeline cannot know a client's timezone, so it carries the full utcDate on
  every match and lets the client label Today/Tomorrow in local time. A match near
  the local/UTC midnight boundary can therefore appear under an adjacent local date
  from a given user's perspective — a documented phase-1 limitation, NOT a bug.
* Raw status. The raw status token (TIMED/IN_PLAY/PAUSED/FINISHED/...) is carried
  verbatim. NO server-side "live" computation — the client owns the strict
  {IN_PLAY, PAUSED} gate.
* Season boundary. We load ALL present season files per league, so any date near
  the UTC August boundary is covered from whichever season file contains it. The
  canonical August rule lives in season_utils.current_season() (reused, not
  reimplemented) — used only to tag the manifest's "current_season".
* Faithful to data. A match is indexed only if it has BOTH a match id and a
  utcDate. Matches missing a date (all tournament group matches; Copa knockout)
  are skipped and counted — never given a fabricated date.
* Scalable layout. Per-month files mean a client loads only the month(s) it needs;
  there is no monolithic all-dates file. Dates with no games are absent; a
  competition with no games on a date is absent.
* Deterministic & idempotent. Stable ordering (canonical competitions, matches by
  kickoff then match_id) and atomic writes mean re-runs produce byte-identical
  files (no noisy diffs). Month files no longer backed by data are removed.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from highlights_common import (
    COMPETITION_SLUG_MAP,
    DOMESTIC_LEAGUE_COMPS,
    FIXTURES_DIR,
    HIGHLIGHTS_DIR,
    SOURCES_JSON,
    load_json_file,
    utc_now_iso,
    video_options,
    write_json_atomic,
)
from season_utils import current_season

log = logging.getLogger("build_home_index")

HOME_INDEX_DIR = FIXTURES_DIR.parent / "home-index"
TOURNAMENT_GROUPS_DIR = FIXTURES_DIR.parent / "tournament-groups"
EVENTS_DIR = FIXTURES_DIR.parent / "events"


# ── Canonical competition order ─────────────────────────────────────────────────


def canonical_competitions() -> list[tuple[str, str]]:
    """Return [(comp_name, slug), ...] in the app's canonical order.

    Order is taken from sources.json's "competitions" key order (the same order the
    app's home/competition list uses); slug via COMPETITION_SLUG_MAP. Competitions
    without a slug mapping are skipped.
    """
    with open(SOURCES_JSON, encoding="utf-8") as f:
        raw = json.load(f)
    out: list[tuple[str, str]] = []
    for name in raw.get("competitions", {}):
        slug = COMPETITION_SLUG_MAP.get(name)
        if slug:
            out.append((name, slug))
    return out


# ── Match normalization (home-card fields only) ─────────────────────────────────


def _team(raw: dict | None) -> dict:
    raw = raw or {}
    return {
        # football-data.org numeric team id — the stable cross-competition join key
        # (same id for a club in its domestic league and in UCL/UEL). Additive: the
        # client still renders from name/tla/crest; the id enables favorite-team
        # filtering on the home screen. null when the raw fixture lacks it (null-safe;
        # a missing/partial side never crashes the build).
        "id": raw.get("id"),
        "name": raw.get("name", ""),
        "shortName": raw.get("shortName", raw.get("name", "")),
        "tla": raw.get("tla", ""),
        "crest": raw.get("crest", ""),
    }


def _bucket_date(utc_date: str | None) -> str | None:
    """Return the UTC calendar date 'YYYY-MM-DD' for an ISO-8601 utcDate, else None."""
    if not utc_date or not isinstance(utc_date, str):
        return None
    try:
        dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).date().isoformat()


def normalize_match(raw: dict, video_id: str | None,
                    options: "list[dict] | None" = None) -> dict | None:
    """Map a raw fixture (domestic or tournament) to the Home match schema.

    Returns None when the match lacks a stable id or a utcDate (cannot be placed on
    a date without fabricating one). Carries the raw status token unchanged.
    """
    match_id = raw.get("match_id", raw.get("id"))
    utc_date = raw.get("utcDate")
    if match_id is None or _bucket_date(utc_date) is None:
        return None

    score = (raw.get("score") or {}).get("fullTime") or {}
    out: dict = {
        "match_id": match_id,
        "homeTeam": _team(raw.get("homeTeam")),
        "awayTeam": _team(raw.get("awayTeam")),
        "homeScore": score.get("home"),
        "awayScore": score.get("away"),
        "status": raw.get("status", ""),  # RAW token — client owns the live gate
        "utcDate": utc_date,
    }
    vid = video_id if video_id is not None else raw.get("video_id")
    if vid:
        out["videoId"] = vid
        # Alternatives with region availability, only when one is geo-locked
        # (see highlights_common.video_options); the app picks one that plays
        # in the viewer's country.
        opts = options if video_id is not None else raw.get("video_options")
        if opts:
            out["videoOptions"] = [
                {"videoId": o["video_id"], "region": o.get("region") or {}} for o in opts
            ]
    return out


# ── Loaders (read-only; no external calls) ──────────────────────────────────────


def _domestic_video_map(slug: str, season: int) -> dict[int, tuple]:
    """Build {match_id -> (video_id, video_options)} from a league's highlights.

    Reads highlights/{slug}/{season}/*.json (each has matches[] with match_id +
    videos[]). First video wins; video_options lists every candidate's region
    when one is geo-locked (else None). Missing files/dirs yield an empty map.
    """
    out: dict[int, tuple] = {}
    season_dir = HIGHLIGHTS_DIR / slug / str(season)
    for path in sorted(season_dir.glob("*.json")):
        data = load_json_file(path)
        if not isinstance(data, dict):
            continue
        for m in data.get("matches", []):
            mid = m.get("match_id")
            vids = m.get("videos") or []
            if mid is not None and vids:
                vid = vids[0].get("video_id")
                if vid and mid not in out:
                    out[mid] = (vid, video_options(vids))
    return out


def load_domestic_matches(slug: str) -> list[dict]:
    """Load + normalize every match for a domestic league across ALL present seasons.

    Loading all season files inherently covers the UTC August boundary from both
    sides. video_id is joined from the season's highlights artifacts.
    """
    matches: list[dict] = []
    league_dir = FIXTURES_DIR / slug
    if not league_dir.is_dir():
        return matches
    for path in sorted(league_dir.glob("*.json")):
        data = load_json_file(path)
        if not isinstance(data, dict):
            continue
        try:
            season = int(path.stem)
        except ValueError:
            continue
        vid_map = _domestic_video_map(slug, season)
        for raw in data.get("fixtures", []):
            vid, opts = vid_map.get(raw.get("match_id"), (None, None))
            m = normalize_match(raw, vid, opts)
            if m:
                matches.append(m)
    return matches


def load_tournament_matches(slug: str) -> list[dict]:
    """Load + normalize dated matches from tournament-groups/{slug}.json.

    Covers both matches[] (knockout) and groupMatches[]. video_id is embedded per
    match. Matches without a utcDate/id (all group matches; Copa knockout today)
    are skipped by normalize_match.
    """
    matches: list[dict] = []
    data = load_json_file(TOURNAMENT_GROUPS_DIR / f"{slug}.json")
    if not isinstance(data, dict):
        return matches
    for raw in list(data.get("matches", [])) + list(data.get("groupMatches", [])):
        m = normalize_match(raw, None)
        if m:
            matches.append(m)
    return matches


# ── Assembly ────────────────────────────────────────────────────────────────────


def build_index() -> tuple[dict[str, dict], dict]:
    """Assemble the per-month index and a manifest.

    Returns (months, manifest) where:
      months   = { 'YYYY-MM': { 'YYYY-MM-DD': [ {competition, name, matches:[...]} ] } }
      manifest = { generated_at, current_season, months:[...], competitions:[{slug,name}] }
    Competitions are in canonical order; matches sorted by (utcDate, match_id).
    """
    comps = canonical_competitions()
    rank = {slug: i for i, (_, slug) in enumerate(comps)}

    # date -> slug -> list[match]
    by_date: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    total_dated = 0

    for name, slug in comps:
        if name in DOMESTIC_LEAGUE_COMPS:
            comp_matches = load_domestic_matches(slug)
        else:
            comp_matches = load_tournament_matches(slug)
        for m in comp_matches:
            by_date[_bucket_date(m["utcDate"])][slug].append(m)
            total_dated += 1

    # Group by month, canonical comp order, matches sorted deterministically.
    months: dict[str, dict] = defaultdict(dict)
    name_by_slug = {slug: name for name, slug in comps}
    for date in sorted(by_date):
        month = date[:7]
        groups = []
        for slug in sorted(by_date[date], key=lambda s: rank.get(s, 1_000)):
            ms = sorted(by_date[date][slug], key=lambda x: (x["utcDate"], x["match_id"]))
            groups.append({"competition": slug, "name": name_by_slug[slug], "matches": ms})
        months[month][date] = groups

    manifest = {
        "generated_at": utc_now_iso(),
        "current_season": current_season(),
        "months": sorted(months),
        "competitions": [{"slug": slug, "name": name} for name, slug in comps],
    }
    log.info(
        "home-index assembled: %d dated matches across %d month(s), %d date(s)",
        total_dated, len(months), len(by_date),
    )
    return dict(months), manifest


def _write_if_changed(path: Path, payload: dict) -> bool:
    """Write payload only if its content (ignoring generated_at) differs from disk.

    Keeps re-runs diff-free: an unchanged month/manifest is left untouched (its old
    generated_at preserved), so `git status` shows nothing when no fixtures changed.
    Returns True if the file was (re)written.
    """
    existing = load_json_file(path)
    if isinstance(existing, dict):
        strip = lambda d: {k: v for k, v in d.items() if k != "generated_at"}
        if strip(existing) == strip(payload):
            return False
    write_json_atomic(path, payload)
    return True


def write_home_index(months: dict[str, dict], manifest: dict) -> None:
    """Write per-month files + manifest atomically; remove stale month files.

    Content-driven & idempotent: only files whose data changed are rewritten, so
    re-runs produce no noisy diffs. Month files no longer backed by data are removed.
    """
    HOME_INDEX_DIR.mkdir(parents=True, exist_ok=True)
    wanted = set()
    written = 0
    for month, dates in months.items():
        path = HOME_INDEX_DIR / f"{month}.json"
        if _write_if_changed(path, {
            "generated_at": manifest["generated_at"],
            "month": month,
            "dates": dates,
        }):
            written += 1
        wanted.add(path.name)
    _write_if_changed(HOME_INDEX_DIR / "index.json", manifest)

    # Remove month files no longer backed by data (keep the manifest).
    removed = 0
    for existing in HOME_INDEX_DIR.glob("*.json"):
        if existing.name != "index.json" and existing.name not in wanted:
            existing.unlink()
            removed += 1
            log.info("Removed stale home-index file: home-index/%s", existing.name)
    log.info("home-index write: %d month file(s) changed, %d removed", written, removed)


SEASONS_MANIFEST_NAME = "seasons.json"


def _years_by_slug(root: Path) -> dict[str, list[int]]:
    """{subdir: [years newest first]} for every root/{subdir}/{year}.json; sorted by slug."""
    out: dict[str, list[int]] = {}
    if root.is_dir():
        for sub in sorted(p for p in root.iterdir() if p.is_dir()):
            years = sorted((int(p.stem) for p in sub.glob("*.json") if p.stem.isdigit()),
                           reverse=True)
            if years:
                out[sub.name] = years
    return out


def build_season_manifest() -> dict:
    """{generated_at, seasons, tournament_archives, events} - {slug: [years newest first]} each.

    seasons: a league season is listed iff fixtures/{slug}/{year}.json exists.
    tournament_archives: a season-based tournament (UCL) iff its archive
    tournament-groups/{slug}/{year}.json exists. These are the tests the app's
    season selectors used to make with a HEAD request per year (a 404 per load
    for every year before the oldest one).
    events: goal/card timelines, events/{slug}/{year}.json (Copa America, written
    by backfill_copa_events.py; none while API-Sports is suspended). The app
    asks for an events file only when it is listed, so the dormant feature costs
    no 404 - and switches on by itself on the run after the backfill writes one.
    """
    return {
        "generated_at": utc_now_iso(),
        "seasons": _years_by_slug(FIXTURES_DIR),
        "tournament_archives": _years_by_slug(TOURNAMENT_GROUPS_DIR),
        "events": _years_by_slug(EVENTS_DIR),
    }


def write_season_manifest() -> bool:
    """Write fixtures/seasons.json if its seasons changed (generated_at ignored)."""
    return _write_if_changed(FIXTURES_DIR / SEASONS_MANIFEST_NAME, build_season_manifest())


def regenerate() -> None:
    """Entry point wired into the existing fixtures write path (fetch_highlights)."""
    months, manifest = build_index()
    write_home_index(months, manifest)
    log.info("Wrote home-index/ (%d month file(s) + index.json)", len(months))
    if write_season_manifest():
        log.info("Wrote fixtures/%s", SEASONS_MANIFEST_NAME)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    regenerate()


if __name__ == "__main__":
    main()
