#!/usr/bin/env python3
"""
fetch_highlights.py

Fetches recently completed fixtures from football-data.org, searches
configured YouTube playlists from sources.json in tier-priority order,
and writes pre-built video metadata into per-gameweek JSON files under
highlights/{competition-slug}/.

Environment variables required:
    FOOTBALL_DATA_API_KEY   — football-data.org personal access token
    YOUTUBE_API_KEY         — YouTube Data API v3 key

Quota cost: only playlistItems.list (1 unit/page) is used — never search.list.
"""

import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT      = Path(__file__).resolve().parent.parent
SOURCES_JSON   = REPO_ROOT / "sources.json"
HIGHLIGHTS_DIR = REPO_ROOT / "highlights"

# ── API endpoints ─────────────────────────────────────────────────────────────

FD_BASE        = "https://api.football-data.org/v4"
YT_PLAYLIST    = "https://www.googleapis.com/youtube/v3/playlistItems"

# ── Tuning constants ──────────────────────────────────────────────────────────

LOOKBACK_DAYS     = 5   # how many days back to look for completed fixtures
VIDEO_WINDOW_DAYS = 5   # accept videos published up to N days after fixture date
MAX_YT_PAGES      = 10  # cap per playlist (50 items/page → max 500 items, 10 quota units)
MAX_GW_IN_SUMMARY = 2   # most-recent gameweeks per competition included in summary.json

# ── Competition maps ──────────────────────────────────────────────────────────

# football-data.org competition code → key used in sources.json
COMPETITION_CODE_MAP: dict[str, str] = {
    "PL":  "Premier League",
    "PD":  "LaLiga",
    "SA":  "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL":  "Champions League",
    "EL":  "Europa League",
}

# sources.json competition name → output directory slug
COMPETITION_SLUG_MAP: dict[str, str] = {
    "Premier League": "premier-league",
    "LaLiga":         "laliga",
    "Serie A":        "serie-a",
    "Bundesliga":     "bundesliga",
    "Ligue 1":        "ligue-1",
    "Champions League": "ucl",
    "Europa League":  "uel",
}

# UCL/UEL use "matchday-N.json"; domestic leagues use "gameweek-N.json"
UCL_UEL: set[str] = {"Champions League", "Europa League"}

# Keywords used to confirm a video title belongs to the competition
# (applied only when requires_competition_filter=True — i.e., club channel uploads)
COMPETITION_KEYWORDS: dict[str, list[str]] = {
    "Premier League":   ["premier league", "epl"],
    "LaLiga":           ["laliga", "la liga"],
    "Serie A":          ["serie a"],
    "Bundesliga":       ["bundesliga"],
    "Ligue 1":          ["ligue 1"],
    "Champions League": ["champions league", "ucl"],
    "Europa League":    ["europa league", "uel"],
}

# ── ID helpers ────────────────────────────────────────────────────────────────

def extract_channel_id(value: str) -> str | None:
    """
    Return a clean UC... channel ID, or None if the value is a full YouTube
    URL, an empty string, or otherwise not a bare channel ID.

    Some entries in sources.json contain full URLs like
    "https://youtube.com/@laliga?si=..." — these are skipped silently.
    """
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    # A raw channel ID starts with "UC", is ~24 chars, and contains no spaces or slashes
    if re.fullmatch(r"UC[A-Za-z0-9_\-]{20,}", v):
        return v
    return None


def extract_playlist_id(value: str) -> str | None:
    """Return a clean PL... playlist ID, or None if invalid."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if re.fullmatch(r"PL[A-Za-z0-9_\-]{20,}", v):
        return v
    return None


def channel_to_uploads(channel_id: str) -> str:
    """
    Convert a UC... channel ID to its hidden uploads playlist ID.
    Every YouTube channel has one: replace "UC" prefix with "UU".
    e.g. "UCpryVRk_VDudG8SHXgWcG0w" → "UUpryVRk_VDudG8SHXgWcG0w"
    """
    if channel_id.startswith("UC"):
        return "UU" + channel_id[2:]
    return channel_id  # safety fallback


# ── Config loading ────────────────────────────────────────────────────────────

def load_sources() -> dict:
    """
    Parse sources.json into four lookup dicts. Silently skips any entry
    that contains a full URL instead of a bare channel/playlist ID.
    """
    with open(SOURCES_JSON, encoding="utf-8") as f:
        raw = json.load(f)

    # Tier 2: official competition channel IDs
    competition_channels: dict[str, str] = {}
    for comp, val in raw.get("competitions", {}).items():
        cid = extract_channel_id(val)
        if cid:
            competition_channels[comp] = cid

    # Tier 1a/1b: official club channel IDs
    team_channels: dict[str, str] = {}
    for team, val in raw.get("teams", {}).items():
        cid = extract_channel_id(val)
        if cid:
            team_channels[team] = cid

    # Tier 4: broadcaster playlists  —  {competition: {broadcaster: [PLxxx, ...]}}
    competition_playlists: dict[str, dict[str, list[str]]] = {}
    for comp, broadcasters in raw.get("playlists", {}).items():
        if not isinstance(broadcasters, dict):
            continue
        bmap: dict[str, list[str]] = {}
        for bcast, ids in broadcasters.items():
            if isinstance(ids, list):
                clean = [p for p in (extract_playlist_id(i) for i in ids) if p]
            elif isinstance(ids, str):
                p = extract_playlist_id(ids)
                clean = [p] if p else []
            else:
                clean = []
            if clean:
                bmap[bcast] = clean
        if bmap:
            competition_playlists[comp] = bmap

    # Tier 1c/1d: competition-scoped team playlists  —  {competition: {team: PLxxx}}
    team_playlists: dict[str, dict[str, str]] = {}
    for comp, teams in raw.get("teamPlaylists", {}).items():
        if not isinstance(teams, dict):
            continue
        tmap: dict[str, str] = {}
        for team, pid in teams.items():
            p = extract_playlist_id(pid)
            if p:
                tmap[team] = p
        if tmap:
            team_playlists[comp] = tmap

    log.info(
        f"Config loaded: {len(competition_channels)} competition channels, "
        f"{len(team_channels)} team channels, "
        f"{len(competition_playlists)} competitions with broadcaster playlists, "
        f"{len(team_playlists)} competitions with team playlists"
    )
    return {
        "competition_channels":  competition_channels,
        "team_channels":         team_channels,
        "competition_playlists": competition_playlists,
        "team_playlists":        team_playlists,
    }


# ── Football-data.org ─────────────────────────────────────────────────────────

def fetch_recent_fixtures(fd_key: str) -> dict[str, dict[int, list[dict]]]:
    """
    Fetch FINISHED matches for every configured competition that fall within
    the last LOOKBACK_DAYS days (UTC).

    Returns:
        {competition_name: {matchday: [fixture_dict, ...]}}

    Each fixture_dict contains:
        match_id, home_team, home_short, away_team, away_short, date, matchday
    """
    now_utc = datetime.now(timezone.utc)
    cutoff  = now_utc - timedelta(days=LOOKBACK_DAYS)

    result: dict[str, dict[int, list[dict]]] = {}

    for code, comp_name in COMPETITION_CODE_MAP.items():
        url     = f"{FD_BASE}/competitions/{code}/matches"
        headers = {"X-Auth-Token": fd_key}
        params  = {"status": "FINISHED"}

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        except requests.RequestException as exc:
            log.warning(f"Network error fetching {code}: {exc}")
            continue

        if resp.status_code == 429:
            log.warning(f"football-data.org rate-limited on {code} — skipping")
            continue
        if resp.status_code == 404:
            log.warning(f"Competition {code} not found (404) — skipping")
            continue
        if not resp.ok:
            log.warning(f"football-data.org returned HTTP {resp.status_code} for {code} — skipping")
            continue

        matches = resp.json().get("matches", [])
        by_matchday: dict[int, list[dict]] = {}

        for m in matches:
            utc_str = m.get("utcDate", "")
            if not utc_str:
                continue
            try:
                match_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            if match_dt < cutoff:
                continue

            matchday = m.get("matchday")
            if matchday is None:
                continue

            home = m.get("homeTeam", {})
            away = m.get("awayTeam", {})

            by_matchday.setdefault(matchday, []).append({
                "match_id":   m["id"],
                "home_team":  home.get("name", ""),
                "home_short": home.get("shortName") or home.get("name", ""),
                "away_team":  away.get("name", ""),
                "away_short": away.get("shortName") or away.get("name", ""),
                "date":       utc_str[:10],
                "matchday":   matchday,
            })

        if by_matchday:
            result[comp_name] = by_matchday
            total = sum(len(v) for v in by_matchday.values())
            log.info(
                f"{comp_name}: {total} recent finished fixture(s) "
                f"across {len(by_matchday)} matchday(s)"
            )

    return result


# ── Gameweek file helpers ─────────────────────────────────────────────────────

def gw_filename(comp_name: str, matchday: int) -> str:
    prefix = "matchday" if comp_name in UCL_UEL else "gameweek"
    return f"{prefix}-{matchday}.json"


def gw_path(comp_name: str, matchday: int) -> Path:
    slug = COMPETITION_SLUG_MAP[comp_name]
    return HIGHLIGHTS_DIR / slug / gw_filename(comp_name, matchday)


def load_gw_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Could not read {path}: {exc} — will overwrite")
        return None


def write_gw_file(path: Path, data: dict) -> None:
    """Atomic write: write to a sibling temp file, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp.json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def is_gameweek_complete(existing: dict | None, fixtures: list[dict]) -> bool:
    """
    Return True if every fixture in the current run already has at least one
    video recorded in the existing file. If the file doesn't exist, or any
    fixture is missing / has no videos, return False.
    """
    if existing is None:
        return False
    by_id: dict[int, dict] = {m["match_id"]: m for m in existing.get("matches", [])}
    for fix in fixtures:
        match = by_id.get(fix["match_id"])
        if match is None or not match.get("videos"):
            return False
    return True


# ── YouTube playlist search ───────────────────────────────────────────────────

def search_playlist(
    playlist_id: str,
    yt_key: str,
    fixture: dict,
    comp_name: str,
    requires_competition_filter: bool = False,
) -> list[dict]:
    """
    Search a playlist for videos that match the given fixture.

    Acceptance criteria (ALL must be true):
      1. published_at is in [fixture_date, fixture_date + VIDEO_WINDOW_DAYS]
      2. If requires_competition_filter: title contains a competition keyword
      3. Title (case-insensitive) contains home_short OR away_short

    Pagination is capped at MAX_YT_PAGES. Pagination stops after the first
    page on which at least one video is accepted.

    Raises SystemExit(1) on HTTP 403 (quota exceeded) — caller must not
    continue or commit partial results.

    Returns a list of dicts: {video_id, title, published_at}
    (tier_used is stamped by the caller)
    """
    fixture_date = datetime.strptime(fixture["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_end   = fixture_date + timedelta(days=VIDEO_WINDOW_DAYS)

    home_short = fixture["home_short"].lower()
    away_short = fixture["away_short"].lower()
    keywords   = COMPETITION_KEYWORDS.get(comp_name, [])

    accepted: list[dict] = []
    seen_ids: set[str]   = set()
    page_token: str      = ""
    pages_fetched: int   = 0

    while pages_fetched < MAX_YT_PAGES:
        params: dict = {
            "part":       "snippet",
            "playlistId": playlist_id,
            "key":        yt_key,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = requests.get(YT_PLAYLIST, params=params, timeout=15)
        except requests.RequestException as exc:
            log.warning(f"Network error searching playlist {playlist_id}: {exc}")
            return accepted

        if resp.status_code == 403:
            log.error(
                f"YouTube API returned 403 (quota exceeded) on playlist {playlist_id}. "
                "Aborting — no partial results will be committed."
            )
            sys.exit(1)

        if resp.status_code == 404:
            log.warning(f"Playlist {playlist_id} not found (404) — skipping")
            return accepted

        if not resp.ok:
            log.warning(f"YouTube API HTTP {resp.status_code} on playlist {playlist_id} — skipping")
            return accepted

        data = resp.json()
        pages_fetched += 1

        for item in data.get("items", []):
            snippet  = item.get("snippet", {})
            title    = snippet.get("title", "")
            video_id = snippet.get("resourceId", {}).get("videoId", "")
            pub_str  = snippet.get("publishedAt", "")

            if not video_id or not pub_str or video_id in seen_ids:
                continue

            # Parse published date
            pub_str_date = pub_str[:10]
            try:
                pub_date = datetime.strptime(pub_str_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

            # 1. Date window
            if pub_date < fixture_date or pub_date > window_end:
                continue

            lower_title = title.lower()

            # 2. Competition keyword (only for club-channel uploads)
            if requires_competition_filter:
                if not any(kw in lower_title for kw in keywords):
                    continue

            # 3. Team name
            if home_short not in lower_title and away_short not in lower_title:
                continue

            seen_ids.add(video_id)
            accepted.append({
                "video_id":     video_id,
                "title":        title,
                "published_at": pub_str_date,
            })

        # Stop pagination once we've found at least one video on this page
        if accepted:
            break

        page_token = data.get("nextPageToken", "")
        if not page_token:
            break

    return accepted


# ── Tier resolution ───────────────────────────────────────────────────────────

def resolve_videos_for_fixture(
    fixture: dict,
    comp_name: str,
    config: dict,
    yt_key: str,
) -> list[dict]:
    """
    Try each tier in priority order. Stop at the first tier that yields at
    least one accepted video. Returns list of video dicts with tier_used set.

    Tier order:
      1c — team_playlists[competition][home_team]  (competition-scoped)
      1d — team_playlists[competition][away_team]  (competition-scoped)
       2 — competition_channels[competition] → uploads playlist
       4 — competition_playlists[competition] (all broadcaster arrays flattened)
      1a — team_channels[home_team] → uploads playlist  (requires_competition_filter)
      1b — team_channels[away_team] → uploads playlist  (requires_competition_filter)
    """
    team_pl     = config["team_playlists"]
    comp_ch     = config["competition_channels"]
    comp_pl     = config["competition_playlists"]
    team_ch     = config["team_channels"]

    home = fixture["home_team"]
    away = fixture["away_team"]

    def _try(playlist_id: str, tier: int, comp_filter: bool = False) -> list[dict] | None:
        """
        Search the playlist. Returns stamped video list if non-empty,
        empty list [] if searched but nothing found, None if no playlist_id.
        """
        if not playlist_id:
            return None
        vids = search_playlist(playlist_id, yt_key, fixture, comp_name, comp_filter)
        return [{**v, "tier_used": tier} for v in vids]

    # Tier 1c — home team competition-scoped playlist
    pl = team_pl.get(comp_name, {}).get(home, "")
    if pl:
        result = _try(pl, tier=1)
        if result:
            return result

    # Tier 1d — away team competition-scoped playlist
    pl = team_pl.get(comp_name, {}).get(away, "")
    if pl:
        result = _try(pl, tier=1)
        if result:
            return result

    # Tier 2 — official competition channel uploads
    ch = comp_ch.get(comp_name, "")
    if ch:
        result = _try(channel_to_uploads(ch), tier=2)
        if result:
            return result

    # Tier 4 — broadcaster playlists (flatten all arrays, try each)
    for _broadcaster, pl_ids in comp_pl.get(comp_name, {}).items():
        for pl_id in pl_ids:
            result = _try(pl_id, tier=4)
            if result:
                return result

    # Tier 1a — home team club channel uploads (requires competition keyword in title)
    ch = team_ch.get(home, "")
    if ch:
        result = _try(channel_to_uploads(ch), tier=1, comp_filter=True)
        if result:
            return result

    # Tier 1b — away team club channel uploads (requires competition keyword in title)
    ch = team_ch.get(away, "")
    if ch:
        result = _try(channel_to_uploads(ch), tier=1, comp_filter=True)
        if result:
            return result

    return []


# ── Merge helpers ─────────────────────────────────────────────────────────────

def merge_into_gw(
    existing: dict | None,
    comp_name: str,
    matchday: int,
    enriched_fixtures: list[dict],
) -> tuple[dict, bool]:
    """
    Merge enriched_fixtures into existing gameweek data (or create from scratch).

    Rules:
      - New match_id → append full match object
      - Existing match_id → append only video_ids not already present
      - generated_at is updated on every write (only if something changed)

    Returns (merged_data, changed: bool).
    """
    if existing is None:
        existing = {
            "competition":  comp_name,
            "gameweek":     matchday,
            "generated_at": "",
            "matches":      [],
        }

    by_id: dict[int, dict] = {m["match_id"]: m for m in existing.get("matches", [])}
    changed = False

    for fix in enriched_fixtures:
        mid        = fix["match_id"]
        new_videos = fix.get("videos", [])

        if mid not in by_id:
            by_id[mid] = {
                "match_id":  mid,
                "home_team": fix["home_team"],
                "away_team": fix["away_team"],
                "date":      fix["date"],
                "videos":    new_videos[:],  # copy
            }
            changed = True
        else:
            existing_match    = by_id[mid]
            existing_vid_ids  = {v["video_id"] for v in existing_match.get("videos", [])}
            for vid in new_videos:
                if vid["video_id"] not in existing_vid_ids:
                    existing_match.setdefault("videos", []).append(vid)
                    existing_vid_ids.add(vid["video_id"])
                    changed = True

    existing["matches"] = list(by_id.values())

    if changed:
        existing["generated_at"] = datetime.utcnow().isoformat() + "Z"

    return existing, changed


# ── Summary generation ────────────────────────────────────────────────────────

def generate_summary() -> None:
    """
    Scan all existing gameweek/matchday files under highlights/ and write
    highlights/summary.json.

    For each competition the MAX_GW_IN_SUMMARY most-recent files are included
    so the admin panel only shows actionable, recent coverage gaps.

    Schema:
        {
          "generated_at": "...",
          "competitions": [
            {
              "competition": "Premier League",
              "slug": "premier-league",
              "gameweeks": [
                {
                  "gameweek": 36,
                  "total": 10,
                  "covered": 8,
                  "matches": [
                    {"match_id": 1, "home": "...", "away": "...",
                     "date": "...", "covered": true}
                  ]
                }
              ]
            }
          ]
        }
    """
    now = datetime.utcnow().isoformat() + "Z"
    competitions: list[dict] = []

    for comp_name, slug in COMPETITION_SLUG_MAP.items():
        comp_dir = HIGHLIGHTS_DIR / slug
        if not comp_dir.exists():
            continue

        pattern = "matchday-*.json" if comp_name in UCL_UEL else "gameweek-*.json"
        # Sort by embedded number so "gameweek-9" < "gameweek-10"
        def _gw_num(p: "Path") -> int:
            try:
                return int(p.stem.split("-")[-1])
            except (ValueError, IndexError):
                return 0

        files = sorted(comp_dir.glob(pattern), key=_gw_num)
        files = files[-MAX_GW_IN_SUMMARY:]  # keep most recent N

        gameweeks: list[dict] = []
        for f in files:
            data = load_gw_file(f)
            if not data:
                continue
            matches_data = data.get("matches", [])
            total = len(matches_data)
            covered = sum(1 for m in matches_data if m.get("videos"))

            try:
                number = int(f.stem.split("-")[-1])
            except (ValueError, IndexError):
                continue

            matches_summary = [
                {
                    "match_id": m["match_id"],
                    "home":     m["home_team"],
                    "away":     m["away_team"],
                    "date":     m.get("date", ""),
                    "covered":  bool(m.get("videos")),
                }
                for m in matches_data
            ]

            gameweeks.append({
                "gameweek": number,
                "total":    total,
                "covered":  covered,
                "matches":  matches_summary,
            })

        if gameweeks:
            competitions.append({
                "competition": comp_name,
                "slug":        slug,
                "gameweeks":   gameweeks,
            })

    summary = {
        "generated_at": now,
        "competitions": competitions,
    }
    summary_path = HIGHLIGHTS_DIR / "summary.json"
    write_gw_file(summary_path, summary)
    log.info(f"Written summary.json ({len(competitions)} competition(s))")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    fd_key = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()
    yt_key = os.environ.get("YOUTUBE_API_KEY", "").strip()

    if not fd_key:
        log.error("FOOTBALL_DATA_API_KEY environment variable is not set")
        sys.exit(1)
    if not yt_key:
        log.warning(
            "YOUTUBE_API_KEY is not set — YouTube playlist searches will be skipped. "
            "summary.json will still be regenerated from existing highlight files."
        )

    # ── 1. Load configuration ─────────────────────────────────────────────────
    config = load_sources()

    total_written = 0

    # ── 2. Fetch fixtures and search YouTube (only when API key is available) ──
    if yt_key:
        all_fixtures = fetch_recent_fixtures(fd_key)

        if not all_fixtures:
            log.info("No recently completed fixtures found within the last "
                     f"{LOOKBACK_DAYS} days — nothing to do.")
        else:
            # ── 3. Process each competition / matchday ────────────────────────
            for comp_name, by_matchday in sorted(all_fixtures.items()):
                for matchday, fixtures in sorted(by_matchday.items()):

                    path     = gw_path(comp_name, matchday)
                    existing = load_gw_file(path)

                    # Smart skip: skip YouTube if every fixture already has videos
                    if is_gameweek_complete(existing, fixtures):
                        log.info(
                            f"INFO: gameweek {matchday} for {comp_name} is complete, skipping"
                        )
                        continue

                    log.info(
                        f"Processing {comp_name} GW{matchday} "
                        f"({len(fixtures)} fixture(s))…"
                    )

                    enriched_fixtures: list[dict] = []

                    for fix in fixtures:
                        videos = resolve_videos_for_fixture(fix, comp_name, config, yt_key)

                        enriched_fixtures.append({**fix, "videos": videos})

                        if not videos:
                            # ── Missing match alert ──────────────────────────
                            log.warning(
                                f"No highlights found — {comp_name} GW{matchday}: "
                                f"{fix['home_team']} vs {fix['away_team']} ({fix['date']})"
                            )
                        else:
                            tiers = sorted({v["tier_used"] for v in videos})
                            log.info(
                                f"  ✓ {fix['home_team']} vs {fix['away_team']}: "
                                f"{len(videos)} video(s) via tier(s) {tiers}"
                            )

                    # ── Merge and write ──────────────────────────────────────
                    gw_data, changed = merge_into_gw(
                        existing, comp_name, matchday, enriched_fixtures
                    )

                    if changed:
                        write_gw_file(path, gw_data)
                        total_written += 1
                        log.info(f"  → Wrote {path.relative_to(REPO_ROOT)}")
                    else:
                        log.info(f"  → No changes to {path.relative_to(REPO_ROOT)}")

    log.info(f"Done. {total_written} file(s) updated.")

    # ── 4. Always regenerate summary.json ────────────────────────────────────
    generate_summary()


if __name__ == "__main__":
    main()
