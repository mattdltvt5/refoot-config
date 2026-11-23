"""Alternatives pass: give matches whose clips don't play in some launch
country a second clip that does, from the sources already configured.

Why: the matcher stops at the first source with a clip, and a covered match is
never searched again. For Serie A, Ligue 1 and LaLiga the first clip is often a
US broadcaster's (CBS Sports Golazo, beIN Sports US, ESPN US) that plays only
in the US, while the competition and club channels - tried earlier or never -
publish clips that play almost everywhere. On 2026-11-22, 878 matches had no
clip playable in GB/IE (877 in ES, 624 in CA, 13 in the US).

What it does, for current-season domestic league, Champions League and
summer-tournament matches (ALT_COMPS). Country sources beyond the competition and
club channels: UCL - TNT Sports Football's uploads (GB/IE, a broadcasterChannels
source) and DAZN Football's season playlist (CA); World Cup - FIFA's Match
Highlights playlist (everywhere); Euro 2024 - ITV (GB), RTE (IE), TSN (CA);
Copa America 2024 - TSN (CA). See rights/{season}.json.

  * A match "needs alternatives" when some LAUNCH_COUNTRIES country can play
    none of its clips (a clip without ``region`` counts as playable).
  * It re-runs the matcher accepting only clips that play in a still-uncovered
    country (and aren't already listed). Every source is searched, broadcaster
    playlists included: a US broadcaster's clip is refused for a match missing
    GB/IE/ES (it adds nothing) but taken for one missing the US - e.g. a LaLiga
    channel clip that blocks the US, Canada and Spain, where ESPN US's covers
    the US. Found clips are APPENDED - the existing first clip stays,
    and the app opens whichever plays where the viewer is.
  * Recent matches (fixture within ALT_RECENT_DAYS) are re-checked with
    backoff - 2, 4, 8, 16, 32, 64 hours after the previous check - because
    club and competition channels often upload after the US broadcaster.
  * Older matches get ONE catch-up check, newest first, within
    ALT_CATCHUP_UNITS_PER_RUN and only while the day's quota is below
    ALT_CATCHUP_CEILING, so match days keep their budget. The catch-up also
    covers the previous ALT_PAST_SEASONS season(s), after the current one
    (domestic leagues only: UCL and the tournaments are current edition only).
  * Adding a source re-opens the check: each check records a fingerprint of
    the competition's sources (``sources``), and a match checked against other
    sources is due once more. Records from before fingerprints count as checked
    against today's sources, except for the competitions in
    SOURCES_CHANGED_UNSTAMPED (their sources changed when fingerprints began).
  * A match missing from the fixture data (e.g. the UCL 2025 archive holds
    only knockouts) is searched with a fixture built from its own record:
    team names, date and the matchday from the file name.
  * Each check searches up to ALT_MAX_ROUNDS times while a country is still
    uncovered (the matcher stops at the first source with a clip).
  * A clip that stops playing (deleted, re-restricted) re-opens the check: a
    country covered at the last check (``covered``) that no longer is.
  * Bookkeeping per match: ``alternatives: {"checks": n, "last": ISO,
    "sources": fingerprint, "covered": [countries]}``.

Never touches a match with full coverage, a match without clips, or anything
outside the current season's domestic league files.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from highlights_common import (
    COMPETITION_SLUG_MAP,
    DOMESTIC_LEAGUE_COMPS,
    FIXTURES_DIR,
    HIGHLIGHTS_DIR,
    REPO_ROOT,
    LAUNCH_COUNTRIES,
    QuotaCapReached,
    SUMMER_TOURNAMENT_COMPS,
    is_highlight_title,
    load_json_file,
    region_is_playable_in,
    resolve_videos_for_fixture,
    season_for_competition,
    utc_now_iso,
    write_json_atomic,
)

log = logging.getLogger(__name__)

ALT_COMPS = set(DOMESTIC_LEAGUE_COMPS) | {"Champions League"} | set(SUMMER_TOURNAMENT_COMPS)
TOURNAMENT_GROUPS_DIR = REPO_ROOT / "tournament-groups"
ALT_PAST_SEASONS = 1                 # catch-up also covers last season
# Not worth a past-season check: UCL's only non-US source is TNT Sports Football's
# uploads, which the 10-page uploads search can't reach a season back. Measured
# 2026-11-23: 32 UCL 2025 matches checked, none gained a TNT clip.
ALT_CURRENT_SEASON_ONLY = {"Champions League"} | set(SUMMER_TOURNAMENT_COMPS)
# Unstamped records (checked before source fingerprints existed) of these
# competitions are re-checked once: their sources changed on 2026-11-23 (UCL:
# DAZN Football for Canada; "Feyenoord Rotterdam" titles now match).
SOURCES_CHANGED_UNSTAMPED = {"Champions League"}
ALT_RECENT_DAYS = 7
ALT_MAX_RECENT_CHECKS = 6            # 2+4+8+16+32+64 h ~= 5 days of re-checks
ALT_CATCHUP_UNITS_PER_RUN = 20
ALT_MAX_ROUNDS = 3                   # searches per check while a country is uncovered
ALT_CATCHUP_CEILING = 5_000          # catch-up only while units_used is below this


def plays(region: "dict | None", country: str) -> bool:
    """A cached clip's ``region`` (absent/{} = everywhere) plays in [country]."""
    return region_is_playable_in(region or None, country)


def uncovered(videos: list[dict]) -> list[str]:
    """Launch countries where none of [videos] plays."""
    return [cc for cc in LAUNCH_COUNTRIES
            if not any(plays(v.get("region"), cc) for v in videos)]


def needs_alternatives(match: dict) -> bool:
    vids = match.get("videos") or []
    return bool(vids) and bool(uncovered(vids))


def _parse(iso: "str | None") -> "datetime | None":
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_recent(match: dict, now: datetime) -> bool:
    try:
        day = datetime.strptime(match.get("date", ""), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return now - day <= timedelta(days=ALT_RECENT_DAYS)


def sources_fingerprint(config: dict, comp: str) -> str:
    """Short, stable fingerprint of a competition's sources: its channel, its
    broadcaster playlists and broadcaster channel feeds (club sources are left
    out - they change often and rarely add a country)."""
    body = {
        "channel": (config.get("competition_channels") or {}).get(comp, ""),
        "playlists": {k: sorted(v) for k, v in
                      sorted(((config.get("competition_playlists") or {}).get(comp) or {}).items())},
        "broadcasters": sorted((e.get("channel_id", ""), tuple(e.get("required_terms") or ()))
                               for e in (config.get("broadcaster_channels") or {}).get(comp, [])),
    }
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:10]


def sources_changed(state: dict, comp: "str | None", fingerprint: "str | None") -> bool:
    """Whether the last check used other sources than [fingerprint]."""
    if fingerprint is None:
        return False
    if "sources" in state:
        return state["sources"] != fingerprint
    return comp in SOURCES_CHANGED_UNSTAMPED


def covered(videos: list[dict]) -> list[str]:
    """Launch countries where at least one of [videos] plays."""
    gaps = set(uncovered(videos))
    return [cc for cc in LAUNCH_COUNTRIES if cc not in gaps]


def lost_coverage(match: dict) -> bool:
    """A country covered at the last check isn't any more (a clip was deleted or
    re-restricted). Records from before ``covered`` was kept: only when no clip
    plays anywhere."""
    state = match.get("alternatives") or {}
    now_covered = set(covered(match.get("videos") or []))
    if "covered" in state:
        return bool(set(state["covered"]) - now_covered)
    return not now_covered


def due(match: dict, now: datetime, comp: "str | None" = None,
        fingerprint: "str | None" = None) -> bool:
    """Whether to check this match for alternatives now (see module doc)."""
    if not needs_alternatives(match):
        return False
    state = match.get("alternatives") or {}
    checks = int(state.get("checks", 0))
    if checks and sources_changed(state, comp, fingerprint):
        return True                              # new sources: one more check
    if checks and lost_coverage(match):
        return True                              # a clip stopped playing: look again
    if not is_recent(match, now):
        return checks == 0                       # catch-up: once
    if checks >= ALT_MAX_RECENT_CHECKS:
        return False
    last = _parse(state.get("last"))
    return last is None or now - last >= timedelta(hours=2 ** checks)


def make_accept(videos: list[dict]):
    """accept(video) for the matcher: a new clip that plays in a still-uncovered
    launch country (a candidate without region info is refused - it can't be
    shown to help - and so is one whose title doesn't read as highlights)."""
    need = uncovered(videos)
    ids = {v.get("video_id") for v in videos}
    # Same-title guard against re-listing a clip, but only for clips that still
    # play somewhere: a deleted clip (region {"allowed": []}) re-uploaded under
    # its old title is exactly the replacement we want.
    titles = {(v.get("title") or "").strip().lower() for v in videos
              if any(plays(v.get("region"), cc) for cc in LAUNCH_COUNTRIES)}

    def accept(video: dict) -> bool:
        if video.get("video_id") in ids:
            return False
        if (video.get("title") or "").strip().lower() in titles:
            return False
        if "region" not in video:
            return False
        # Broadcaster playlists skip the matcher's highlight-title allowlist, and
        # some mix in other clips (TSN's Euro 2024 playlist: "FULL EURO2024 TROPHY
        # PRESENTATION | SPAIN VS. ENGLAND"). An added clip must read as highlights.
        if not is_highlight_title(video.get("title") or ""):
            return False
        return any(plays(video["region"], cc) for cc in need)
    return accept


def _raw_fixtures(slug: str, season: int, fixtures_dir: Path, groups_dir: Path) -> list[dict]:
    """Domestic: fixtures/{slug}/{season}.json. UCL: tournament-groups/{slug}.json
    (league phase groupMatches + knockout matches), when it is that season's.
    Summer tournaments: tournament-groups/{slug}.json has no ``season``; its
    matches dated in [season] (the edition year) are that edition's."""
    data = load_json_file(fixtures_dir / slug / f"{season}.json")
    if isinstance(data, dict) and data.get("fixtures"):
        return data["fixtures"]
    for tg in (load_json_file(groups_dir / f"{slug}.json"),
               load_json_file(groups_dir / slug / f"{season}.json")):   # season archive
        if not isinstance(tg, dict):
            continue
        rows = list(tg.get("groupMatches") or []) + list(tg.get("matches") or [])
        if tg.get("season") is None and "season" not in tg:
            rows = [f for f in rows if str(f.get("utcDate") or "")[:4] == str(season)]
            if not rows:
                continue
        elif tg.get("season") != season:
            continue
        return [{**f, "match_id": f.get("match_id", f.get("id"))} for f in rows]
    return []


def fixture_from_record(match: dict, path: Path) -> "dict | None":
    """A matcher fixture built from a cached match record, for matches the
    fixture data doesn't list. No TLA, short name = full name (team_tokens adds
    the aliases), no score (so no scoreline check)."""
    home, away, date = match.get("home_team"), match.get("away_team"), match.get("date")
    if not (home and away and date and match.get("match_id") is not None):
        return None
    stem = path.stem
    md = int(stem.rsplit("-", 1)[1]) if stem.rsplit("-", 1)[-1].isdigit() else None
    return {
        "match_id": match["match_id"], "home_team": home, "home_short": home, "home_tla": "",
        "away_team": away, "away_short": away, "away_tla": "", "date": date,
        "matchday": md, "stage": "LEAGUE_STAGE" if stem.startswith("matchday-") else "REGULAR_SEASON",
        "status": "FINISHED", "score": {},
    }


def fixture_index(slug: str, season: int, fixtures_dir: Path = FIXTURES_DIR,
                  groups_dir: "Path | None" = None) -> dict[int, dict]:
    """match_id -> the matcher's fixture dict (see _raw_fixtures for sources)."""
    groups_dir = TOURNAMENT_GROUPS_DIR if groups_dir is None else groups_dir
    out: dict[int, dict] = {}
    for f in _raw_fixtures(slug, season, fixtures_dir, groups_dir):
        h, a = f.get("homeTeam") or {}, f.get("awayTeam") or {}
        mid = f.get("match_id")
        if mid is None:
            continue
        out[mid] = {
            "match_id":   mid,
            "home_team":  h.get("name", ""),
            "home_short": h.get("shortName") or h.get("name", ""),
            "home_tla":   h.get("tla", ""),
            "away_team":  a.get("name", ""),
            "away_short": a.get("shortName") or a.get("name", ""),
            "away_tla":   a.get("tla", ""),
            "date":       (f.get("utcDate") or "")[:10],
            "matchday":   f.get("matchday"),
            "stage":      f.get("stage") or ("LEAGUE_STAGE" if f.get("group") else "REGULAR_SEASON"),
            "status":     f.get("status"),
            "score":      f.get("score") or {},
        }
    return out


def _candidates(root: Path, fixtures_dir: Path, now: datetime, groups_dir: "Path | None" = None,
                fingerprints: "dict[str, str] | None" = None):
    """(comp, path, data, match, fixture) for every due match, recent first,
    then catch-up newest first. [fingerprints]: comp -> sources_fingerprint."""
    recent, catchup = [], []
    fingerprints = fingerprints or {}
    for comp in sorted(ALT_COMPS):
        slug = COMPETITION_SLUG_MAP.get(comp)
        if not slug:
            continue
        fp = fingerprints.get(comp)
        current = season_for_competition(comp, now)
        past = 0 if comp in ALT_CURRENT_SEASON_ONLY else ALT_PAST_SEASONS
        for season in range(current, current - past - 1, -1):
            season_dir = root / slug / str(season)
            if not season_dir.is_dir():
                continue
            fx = fixture_index(slug, season, fixtures_dir, groups_dir)
            for path in sorted(season_dir.glob("*.json")):
                data = load_json_file(path)
                if not isinstance(data, dict):
                    continue
                for match in data.get("matches", []):
                    if not due(match, now, comp, fp):
                        continue
                    fixture = fx.get(match.get("match_id")) or fixture_from_record(match, path)
                    if fixture is None:
                        continue
                    item = (comp, path, data, match, fixture)
                    (recent if is_recent(match, now) else catchup).append(item)
    catchup.sort(key=lambda it: it[3].get("date", ""), reverse=True)
    return recent, catchup


def find_alternatives(
    yt_key: str,
    quota,
    cap: int,
    config: dict,
    gw_playlist_cache: "dict | None" = None,
    page_cache: "dict | None" = None,
    now: "datetime | None" = None,
    root: Path = HIGHLIGHTS_DIR,
    fixtures_dir: Path = FIXTURES_DIR,
    groups_dir: "Path | None" = None,
    catchup_units: int = ALT_CATCHUP_UNITS_PER_RUN,
    catchup_ceiling: int = ALT_CATCHUP_CEILING,
) -> list[Path]:
    """Run the pass; returns the files written. QuotaCapReached propagates after
    what was found so far is written."""
    now = now or datetime.now(timezone.utc)
    gw_playlist_cache = {} if gw_playlist_cache is None else gw_playlist_cache
    page_cache = {} if page_cache is None else page_cache
    fingerprints = {comp: sources_fingerprint(config, comp) for comp in ALT_COMPS}
    recent, catchup = _candidates(root, fixtures_dir, now, groups_dir, fingerprints)
    touched: dict[Path, dict] = {}
    found = 0
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def check(comp, path, data, match, fixture):
        """The matcher stops at the first source with an accepted clip, so search
        again while a country is still uncovered (e.g. DAZN's CA clip first,
        then TNT's GB/IE one): each round refuses what was already added."""
        nonlocal found
        state = match.get("alternatives") or {}
        touched[path] = data
        added = 0
        try:
            for _ in range(ALT_MAX_ROUNDS):
                vids = match.get("videos") or []
                extra = resolve_videos_for_fixture(
                    fixture, comp, config, yt_key, quota, cap,
                    gw_playlist_cache=gw_playlist_cache, page_cache=page_cache,
                    accept=make_accept(vids),
                )
                if not extra:
                    break
                match["videos"] = vids + extra
                added += len(extra)
                if not uncovered(match["videos"]):
                    break
        finally:
            match["alternatives"] = {"checks": int(state.get("checks", 0)) + 1, "last": stamp,
                                     "sources": fingerprints.get(comp),
                                     "covered": covered(match.get("videos") or [])}
            if added:
                found += added
                data["generated_at"] = utc_now_iso()
                log.info(f"  Alternatives: +{added} for {fixture['home_team']} vs "
                         f"{fixture['away_team']} now plays in {covered(match['videos'])}")

    try:
        for item in recent:
            check(*item)
        start = quota.units_used
        for item in catchup:
            if quota.units_used - start >= catchup_units or quota.units_used >= catchup_ceiling:
                break
            check(*item)
    finally:
        for path, data in touched.items():
            write_json_atomic(path, data)
        if touched:
            log.info(f"Alternatives: checked {len(recent)} recent + catch-up; "
                     f"{found} clip(s) added; {len(touched)} file(s) written")
    return list(touched)
