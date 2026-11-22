"""Alternatives pass: give matches whose clips don't play in some launch
country a second clip that does, from the sources already configured.

Why: the matcher stops at the first source with a clip, and a covered match is
never searched again. For Serie A, Ligue 1 and LaLiga the first clip is often a
US broadcaster's (CBS Sports Golazo, beIN Sports US, ESPN US) that plays only
in the US, while the competition and club channels - tried earlier or never -
publish clips that play almost everywhere. On 2026-11-22, 878 matches had no
clip playable in GB/IE (877 in ES, 624 in CA, 13 in the US).

What it does, for current-season domestic league matches only (UCL and the
tournaments have no worldwide source configured - phase 2):

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
    ALT_CATCHUP_CEILING, so match days keep their budget.
  * Bookkeeping per match: ``alternatives: {"checks": n, "last": ISO}``.

Never touches a match with full coverage, a match without clips, or anything
outside the current season's domestic league files.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from highlights_common import (
    COMPETITION_SLUG_MAP,
    DOMESTIC_LEAGUE_COMPS,
    FIXTURES_DIR,
    HIGHLIGHTS_DIR,
    LAUNCH_COUNTRIES,
    QuotaCapReached,
    load_json_file,
    region_is_playable_in,
    resolve_videos_for_fixture,
    season_for_competition,
    utc_now_iso,
    write_json_atomic,
)

log = logging.getLogger(__name__)

ALT_RECENT_DAYS = 7
ALT_MAX_RECENT_CHECKS = 6            # 2+4+8+16+32+64 h ~= 5 days of re-checks
ALT_CATCHUP_UNITS_PER_RUN = 20
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


def due(match: dict, now: datetime) -> bool:
    """Whether to check this match for alternatives now (see module doc)."""
    if not needs_alternatives(match):
        return False
    state = match.get("alternatives") or {}
    checks = int(state.get("checks", 0))
    if not is_recent(match, now):
        return checks == 0                       # catch-up: once
    if checks >= ALT_MAX_RECENT_CHECKS:
        return False
    last = _parse(state.get("last"))
    return last is None or now - last >= timedelta(hours=2 ** checks)


def make_accept(videos: list[dict]):
    """accept(video) for the matcher: a new clip that plays in a still-uncovered
    launch country (a candidate without region info is refused - it can't be
    shown to help)."""
    need = uncovered(videos)
    ids = {v.get("video_id") for v in videos}
    titles = {(v.get("title") or "").strip().lower() for v in videos}

    def accept(video: dict) -> bool:
        if video.get("video_id") in ids:
            return False
        if (video.get("title") or "").strip().lower() in titles:
            return False
        if "region" not in video:
            return False
        return any(plays(video["region"], cc) for cc in need)
    return accept


def fixture_index(slug: str, season: int, fixtures_dir: Path = FIXTURES_DIR) -> dict[int, dict]:
    """match_id -> the matcher's fixture dict, from fixtures/{slug}/{season}.json."""
    data = load_json_file(fixtures_dir / slug / f"{season}.json") or {}
    out: dict[int, dict] = {}
    for f in data.get("fixtures", []):
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
            "stage":      "REGULAR_SEASON",
            "status":     f.get("status"),
            "score":      f.get("score") or {},
        }
    return out


def _candidates(root: Path, fixtures_dir: Path, now: datetime):
    """(comp, path, data, match, fixture) for every due match, recent first,
    then catch-up newest first."""
    recent, catchup = [], []
    for comp in sorted(DOMESTIC_LEAGUE_COMPS):
        slug = COMPETITION_SLUG_MAP.get(comp)
        if not slug:
            continue
        season = season_for_competition(comp, now)
        season_dir = root / slug / str(season)
        if not season_dir.is_dir():
            continue
        fx = fixture_index(slug, season, fixtures_dir)
        for path in sorted(season_dir.glob("*.json")):
            data = load_json_file(path)
            if not isinstance(data, dict):
                continue
            for match in data.get("matches", []):
                fixture = fx.get(match.get("match_id"))
                if fixture is None or not due(match, now):
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
    catchup_units: int = ALT_CATCHUP_UNITS_PER_RUN,
    catchup_ceiling: int = ALT_CATCHUP_CEILING,
) -> list[Path]:
    """Run the pass; returns the files written. QuotaCapReached propagates after
    what was found so far is written."""
    now = now or datetime.now(timezone.utc)
    gw_playlist_cache = {} if gw_playlist_cache is None else gw_playlist_cache
    page_cache = {} if page_cache is None else page_cache
    recent, catchup = _candidates(root, fixtures_dir, now)
    touched: dict[Path, dict] = {}
    found = 0
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def check(comp, path, data, match, fixture):
        nonlocal found
        vids = match.get("videos") or []
        extra = resolve_videos_for_fixture(
            fixture, comp, config, yt_key, quota, cap,
            gw_playlist_cache=gw_playlist_cache, page_cache=page_cache,
            accept=make_accept(vids),
        )
        state = match.get("alternatives") or {}
        match["alternatives"] = {"checks": int(state.get("checks", 0)) + 1, "last": stamp}
        if extra:
            match["videos"] = vids + extra
            found += len(extra)
            data["generated_at"] = utc_now_iso()
            log.info(f"  Alternatives: +{len(extra)} for {fixture['home_team']} vs "
                     f"{fixture['away_team']} now plays in "
                     f"{[cc for cc in LAUNCH_COUNTRIES if cc not in uncovered(match['videos'])]}")
        touched[path] = data

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
