#!/usr/bin/env python3
"""
highlights_common.py

Shared utilities for fetch_highlights.py and backfill_highlights.py.
"""

import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT              = Path(__file__).resolve().parent.parent
SOURCES_JSON           = REPO_ROOT / "sources.json"
HIGHLIGHTS_DIR         = REPO_ROOT / "highlights"
QUOTA_TRACKER_PATH     = HIGHLIGHTS_DIR / "quota-tracker.json"
BACKFILL_PROGRESS_PATH = HIGHLIGHTS_DIR / "backfill-progress.json"
BACKFILL_LOCK_PATH     = HIGHLIGHTS_DIR / "backfill.lock"

# ── API endpoints ─────────────────────────────────────────────────────────────

FD_BASE     = "https://api.football-data.org/v4"
YT_PLAYLIST = "https://www.googleapis.com/youtube/v3/playlistItems"

# ── Tuning constants ──────────────────────────────────────────────────────────

VIDEO_WINDOW_DAYS = 3    # accept videos published up to N days after fixture date
                         # (was 5 — reduced to avoid catching the next fixture's
                         # highlights when two matches are played within 5 days)
MAX_YT_PAGES      = 10  # cap per playlist (50 items/page → max 500 items, 10 units)
FD_SLEEP_SECONDS  = 6   # pause between football-data.org requests (10 req/min free tier)
INCREMENTAL_CAP   = 8_000  # max YouTube units/day for incremental runs
BACKFILL_CAP      = 9_500  # max YouTube units/day for backfill runs

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
    "EC":  "Euro Cup",
    "WC":  "World Cup",
}

# sources.json competition name → output directory slug
COMPETITION_SLUG_MAP: dict[str, str] = {
    "Premier League":   "premier-league",
    "LaLiga":           "laliga",
    "Serie A":          "serie-a",
    "Bundesliga":       "bundesliga",
    "Ligue 1":          "ligue-1",
    "Champions League": "ucl",
    "Europa League":    "uel",
    "Euro Cup":         "euro-cup",
    "World Cup":        "world-cup",
}

# UCL/UEL use "matchday-N.json"; domestic leagues use "gameweek-N.json"
UCL_UEL: set[str] = {"Champions League", "Europa League"}

# Competitions with two-legged knockout rounds
TWO_LEGGED_COMPS: set[str] = {"Champions League", "Europa League"}

# Competitions that use stage-aware file naming (knockout rounds)
STAGE_AWARE_COMPS: set[str] = {"Champions League", "Europa League", "Euro Cup", "World Cup"}

# Keywords confirming a video belongs to the competition (used for club-channel tiers 1a/1b)
COMPETITION_KEYWORDS: dict[str, list[str]] = {
    "Premier League":   ["premier league", "epl"],
    "LaLiga":           ["laliga", "la liga"],
    "Serie A":          ["serie a"],
    "Bundesliga":       ["bundesliga"],
    "Ligue 1":          ["ligue 1"],
    "Champions League": ["champions league", "ucl"],
    "Europa League":    ["europa league", "uel"],
    "Euro Cup":         ["euro", "euros", "euro cup", "european championship"],
    "World Cup":        ["world cup", "fifa world cup", "mundial"],
}

# Ordered list of all expected file stems per competition
COMPETITION_FILE_STEMS: dict[str, list[str]] = {
    "Premier League":   [f"gameweek-{i}" for i in range(1, 39)],
    "LaLiga":           [f"gameweek-{i}" for i in range(1, 39)],
    "Serie A":          [f"gameweek-{i}" for i in range(1, 39)],
    "Bundesliga":       [f"gameweek-{i}" for i in range(1, 35)],
    "Ligue 1":          [f"gameweek-{i}" for i in range(1, 35)],
    "Champions League": (
        [f"matchday-{i}" for i in range(1, 9)]
        + ["playoff-leg-1", "playoff-leg-2",
           "round-of-16-leg-1", "round-of-16-leg-2",
           "quarter-final-leg-1", "quarter-final-leg-2",
           "semi-final-leg-1", "semi-final-leg-2",
           "final"]
    ),
    "Europa League":    (
        [f"matchday-{i}" for i in range(1, 9)]
        + ["playoff-leg-1", "playoff-leg-2",
           "round-of-16-leg-1", "round-of-16-leg-2",
           "quarter-final-leg-1", "quarter-final-leg-2",
           "semi-final-leg-1", "semi-final-leg-2",
           "final"]
    ),
    "Euro Cup":         (
        [f"matchday-{i}" for i in range(1, 4)]
        + ["round-of-16", "quarter-final", "semi-final", "final"]
    ),
    "World Cup":        (
        [f"matchday-{i}" for i in range(1, 4)]
        + ["round-of-16", "quarter-final", "semi-final", "third-place", "final"]
    ),
}

# Display label for each file stem
FILE_STEM_LABEL: dict[str, str] = {
    **{f"gameweek-{i}": f"GW{i}" for i in range(1, 39)},
    **{f"matchday-{i}": f"MD{i}" for i in range(1, 9)},
    "playoff-leg-1":      "PO L1",
    "playoff-leg-2":      "PO L2",
    "round-of-16-leg-1":  "R16 L1",
    "round-of-16-leg-2":  "R16 L2",
    "round-of-16":        "R16",
    "quarter-final-leg-1": "QF L1",
    "quarter-final-leg-2": "QF L2",
    "quarter-final":      "QF",
    "semi-final-leg-1":   "SF L1",
    "semi-final-leg-2":   "SF L2",
    "semi-final":         "SF",
    "third-place":        "3rd Place",
    "final":              "Final",
}

# ── Title filter ─────────────────────────────────────────────────────────────
#
# TITLE_BLOCKLIST is checked FIRST — any match immediately rejects the video.
# TITLE_ALLOWLIST is checked second — at least one term must match to accept.
# Both checks are case-insensitive substring matches.

TITLE_BLOCKLIST: list[str] = [

    # ── English ──
    "press conference", "presser", "pre-match press", "post-match press",
    "interview", "speaks to", "talks to", "media day",
    "reaction", "reacts", "fan reaction", "player reaction",
    "training", "training session", "open training",
    "preview", "prediction", "preview show", "match preview",
    "analysis", "tactical", "breakdown",
    "watch along", "live stream", "matchday live",
    "top 10", "best goals", "best of the season", "season review",
    "all goals of the week",
    "#shorts",               # YouTube Shorts — social clips, not highlight packages

    # ── Single-goal clips (all languages) ──────────────────────────────────────
    # Format: "The goal by [player name]" — a moment clip, not a highlights package.
    "el gol de",             # Spanish
    "le but de",             # French
    "il gol di",             # Italian
    "o gol de",              # Portuguese (Brazilian)
    "o golo de",             # Portuguese (European)
    "das tor von",           # German
    "tor von",               # German (short form)
    "the goal by",           # English

    # ── Historical season clips (all languages) ─────────────────────────────
    # Format: "de la temporada 2012/13" — throwback clip from a past season,
    # published by club channels with current competition hashtags.
    "de la temporada",       # Spanish:    "de la temporada 2012/13"
    "de la saison",          # French:     "de la saison 2012/13"
    "della stagione",        # Italian:    "della stagione 2012/13"
    "da temporada",          # Portuguese: "da temporada 2012/13"
    "der saison",            # German:     "der saison 2012/13"

    # ── Spanish ──
    "rueda de prensa",       # press conference
    "entrevista",            # interview
    "previo",                # preview
    "análisis",              # analysis
    "análisis táctico",      # tactical analysis
    "previa",                # preview
    "reacción",              # reaction

    # ── French ──
    "conférence de presse",  # press conference
    "avant-match",           # pre-match
    "après-match",           # post-match
    "analyse",               # analysis
    "réaction",              # reaction
    "entraînement",          # training

    # ── German ──
    "pressekonferenz",       # press conference
    "vorschau",              # preview
    "training einheit",      # training session
    "reaktion",              # reaction

    # ── Italian ──
    "conferenza stampa",     # press conference
    "intervista",            # interview
    "anteprima",             # preview
    "analisi",               # analysis
    "reazione",              # reaction
    "allenamento",           # training

    # ── Portuguese ──
    "coletiva de imprensa",  # press conference
    "pré-jogo",              # pre-match
    "pós-jogo",              # post-match
    "análise",               # analysis
    "treino",                # training

    # ── Arabic ──
    "مؤتمر صحفي",            # press conference
    "مقابلة",                # interview
    "تحليل",                 # analysis
    "تدريب",                 # training
    "معاينة",                # preview

    # ── Dutch ──
    "persconferentie",       # press conference
    "vooruitblik",           # preview

    # ── Turkish ──
    "basın toplantısı",      # press conference
    "röportaj",              # interview
    "önizleme",              # preview
    "analiz",                # analysis
    "antrenman",             # training
]

# "entrevista" covers Spanish + Portuguese; "analyse" covers French/German/Dutch;
# "interview"/"training"/"analyse" appear in multiple languages — one entry handles all.
# These shared terms are listed once in TITLE_BLOCKLIST above.

TITLE_ALLOWLIST: list[str] = [

    # ── English ──
    "highlight", "highlights",
    "extended highlights",
    "match highlights",
    # "full match" intentionally excluded: bare "FULL MATCH | ..." titles are
    # full 90-min replays (e.g. FC Barcelona's channel), not highlight packages.
    # "Full Match Highlights" still passes because "highlights" is in the list.
    "goals",

    # ── French ──
    "résumé",                # summary/highlights
    "buts",                  # goals

    # ── Spanish ──
    "resumen",               # summary/highlights
    "goles",                 # goals
    "mejores momentos",      # best moments

    # ── German ──
    "zusammenfassung",       # summary/highlights
    "tore",                  # goals
    "spielzusammenfassung",  # match summary

    # ── Italian ──
    "sintesi",               # summary/highlights
    "gol",                   # goals (also Spanish/Portuguese; substring matches goles/gols/goal/goals)

    # ── Portuguese ──
    "melhores momentos",     # best moments / highlights
    "gols",                  # goals
    "resumo",                # summary/highlights

    # ── Arabic ──
    "ملخص",                  # summary/highlights
    "أهداف",                 # goals

    # ── Dutch ──
    "samenvatting",          # summary/highlights
    "doelpunten",            # goals

    # ── Turkish ──
    "özet",                  # summary/highlights
    "goller",                # goals
    "maç özeti",             # match summary

    # ── Japanese ──
    "ハイライト",              # highlights
    "ゴール",                 # goals

    # ── Korean ──
    "하이라이트",              # highlights
    "골",                    # goals
]


def is_highlight_title(title: str) -> bool:
    """
    Return True only when the video title passes both filters:

    1. Blocklist (checked first — always wins): if any blocklist term is found
       in the raw lowercase title the video is rejected immediately, regardless
       of allowlist matches.
    2. Allowlist: at least one allowlist term must appear in the title **with
       hashtags stripped out**.  Stripping prevents hashtag-only passes such as
       ``#LaLigaHighlights`` matching the allowlist term ``"highlights"`` — a
       common pattern on social/Shorts clips that are not highlight packages.

    Both checks are case-insensitive and cover 11 languages: English, Spanish,
    French, German, Italian, Portuguese, Arabic, Dutch, Turkish, Japanese, Korean.
    """
    lower = title.lower()

    # Step 1 — blocklist on the raw title (hashtags included so "#shorts" fires)
    for term in TITLE_BLOCKLIST:
        if term in lower:
            log.debug(f"Title blocked ({term!r}): {title!r}")
            return False

    # Step 2 — allowlist on the hashtag-stripped title.
    # Removes every #word token so that "#LaLigaHighlights" does NOT satisfy
    # the "highlights" allowlist entry.  A genuine highlights title always has
    # the keyword in the non-hashtag body (e.g. "HIGHLIGHTS | LALIGA EA SPORTS").
    lower_no_tags = re.sub(r"#\S+", "", lower).strip()
    for term in TITLE_ALLOWLIST:
        if term in lower_no_tags:
            return True

    log.debug(f"Title failed allowlist: {title!r}")
    return False


# ── Exception ─────────────────────────────────────────────────────────────────


class QuotaCapReached(Exception):
    """Daily YouTube unit cap hit — save state and exit cleanly (exit 0)."""


# ── Season / time helpers ─────────────────────────────────────────────────────

# Competitions that run in the calendar year (June-July) rather than the
# August-July domestic football season.  For these, the season year equals
# the calendar year of the tournament (e.g. WC 2026 → season=2026), even
# when current_season() would otherwise return year-1 (e.g. in May/June).
SUMMER_TOURNAMENT_COMPS: set[str] = {"World Cup", "Euro Cup"}


def current_season() -> int:
    """Return the current domestic football season start year (e.g. 2025 for 2025-26)."""
    now = datetime.now(timezone.utc)
    return now.year if now.month >= 7 else now.year - 1


def season_for_competition(comp_name: str) -> int:
    """
    Return the football-data.org season year to query for a given competition.

    Domestic leagues and UCL/UEL use the August–July convention handled by
    ``current_season()``.  Summer tournaments (World Cup, Euro Cup) run
    June–July and are indexed by their calendar year, so they always use
    ``datetime.now().year`` regardless of the August cutoff.
    """
    if comp_name in SUMMER_TOURNAMENT_COMPS:
        return datetime.now(timezone.utc).year
    return current_season()


def utc_now_iso() -> str:
    """Return current UTC time as an ISO-8601 string with 'Z' suffix."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


# ── File I/O helpers ──────────────────────────────────────────────────────────


def write_json_atomic(path: Path, data: dict) -> None:
    """Atomic write: write to a sibling temp file, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp.json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Backward-compat aliases kept for any callers using the old names
write_gw_file = write_json_atomic


def load_json_file(path: Path) -> dict | None:
    """Load a JSON file; return None on missing file or parse error."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"Could not read {path}: {exc}")
        return None


load_gw_file = load_json_file  # backward-compat alias


# ── Quota tracker ─────────────────────────────────────────────────────────────


class QuotaTracker:
    """
    Persists daily YouTube API unit consumption to highlights/quota-tracker.json.
    Automatically resets when the UTC date changes.
    Both scripts share the same file so the budget is enforced across both runs.
    """

    def __init__(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data  = load_json_file(QUOTA_TRACKER_PATH) or {}
        if data.get("date") != today:
            self.date       = today
            self.units_used = 0
            log.info("Quota day reset — units_used = 0")
        else:
            self.date       = data["date"]
            self.units_used = int(data.get("units_used", 0))
        self._save()

    def _save(self) -> None:
        write_json_atomic(QUOTA_TRACKER_PATH, {
            "date":         self.date,
            "units_used":   self.units_used,
            "last_updated": utc_now_iso(),
        })

    def increment(self, cap: int) -> None:
        """Increment units_used by 1, persist, and raise QuotaCapReached if cap hit."""
        self.units_used += 1
        self._save()
        if self.units_used >= cap:
            raise QuotaCapReached(
                f"{self.units_used}/{cap} units — daily cap reached"
            )

    @property
    def over_incremental_cap(self) -> bool:
        return self.units_used >= INCREMENTAL_CAP


# ── football-data.org helper ──────────────────────────────────────────────────


def fd_get(url: str, fd_key: str, params: dict | None = None) -> requests.Response:
    """
    GET from football-data.org. Backs off 60 s and retries once on 429.
    Exits non-zero if rate-limited a second time in a row.
    """
    headers = {"X-Auth-Token": fd_key}
    resp = requests.get(url, headers=headers, params=params or {}, timeout=15)
    if resp.status_code == 429:
        log.warning("football-data.org rate-limited — backing off 60 s")
        time.sleep(60)
        resp = requests.get(url, headers=headers, params=params or {}, timeout=15)
        if resp.status_code == 429:
            log.error("football-data.org rate-limited again — aborting")
            sys.exit(1)
    return resp


# ── ID helpers ────────────────────────────────────────────────────────────────


def extract_channel_id(value: str) -> str | None:
    """
    Return a clean UC... channel ID, or None if the value is a full YouTube
    URL, an empty string, or otherwise not a bare channel ID.
    """
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    return v if re.fullmatch(r"UC[A-Za-z0-9_\-]{20,}", v) else None


def extract_playlist_id(value: str) -> str | None:
    """Return a clean PL... playlist ID, or None if invalid."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    return v if re.fullmatch(r"PL[A-Za-z0-9_\-]{20,}", v) else None


def channel_to_uploads(channel_id: str) -> str:
    """Convert UC... channel ID to its uploads playlist (replace UC prefix with UU)."""
    return "UU" + channel_id[2:] if channel_id.startswith("UC") else channel_id


# ── Config loading ────────────────────────────────────────────────────────────


def load_sources() -> dict:
    """
    Parse sources.json into four lookup dicts.
    Silently skips entries containing full URLs instead of bare channel/playlist IDs.
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


# ── Stage → file stem mapping ─────────────────────────────────────────────────


def stage_to_file_stem(stage: str, matchday: int | None, comp_name: str) -> str | None:
    """
    Convert a football-data.org stage string to a file stem.

    For non-STAGE_AWARE_COMPS (domestic leagues): returns "gameweek-{matchday}".
    For STAGE_AWARE_COMPS (UCL/UEL/Euro Cup/World Cup): maps the stage string.

    Returns None if the stage is unrecognised (caller should skip the fixture).
    """
    if comp_name not in STAGE_AWARE_COMPS:
        if matchday is None:
            return None
        return f"gameweek-{matchday}"

    two_legged = comp_name in TWO_LEGGED_COMPS

    if stage in ("LEAGUE_STAGE", "GROUP_STAGE"):
        if matchday is None:
            return None
        return f"matchday-{matchday}"
    elif stage == "PLAYOFFS":
        if matchday is None:
            return None
        return f"playoff-leg-{matchday}"
    elif stage == "LAST_16":
        if two_legged:
            if matchday is None:
                return None
            return f"round-of-16-leg-{matchday}"
        return "round-of-16"
    elif stage == "QUARTER_FINALS":
        if two_legged:
            if matchday is None:
                return None
            return f"quarter-final-leg-{matchday}"
        return "quarter-final"
    elif stage == "SEMI_FINALS":
        if two_legged:
            if matchday is None:
                return None
            return f"semi-final-leg-{matchday}"
        return "semi-final"
    elif stage == "THIRD_PLACE":
        return "third-place"
    elif stage == "FINAL":
        return "final"
    else:
        log.debug(f"Unrecognised stage {stage!r} for {comp_name} — skipping")
        return None


# ── Gameweek file helpers ─────────────────────────────────────────────────────


def gw_filename(comp_name: str, stem: str) -> str:
    return f"{stem}.json"


def gw_path(comp_name: str, stem: str) -> Path:
    return HIGHLIGHTS_DIR / COMPETITION_SLUG_MAP[comp_name] / f"{stem}.json"


def is_gameweek_complete(existing: dict | None, fixtures: list[dict]) -> bool:
    """Return True if every fixture already has at least one video in the existing file."""
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
    quota: QuotaTracker,
    cap: int,
    requires_competition_filter: bool = False,
    requires_both_teams: bool = False,
) -> list[dict]:
    """
    Search a playlist for videos matching the given fixture.

    Acceptance criteria (ALL must be true):
      1. published_at is in [fixture_date, fixture_date + VIDEO_WINDOW_DAYS]
      2. If requires_competition_filter: title contains a competition keyword
      3. If requires_both_teams: title contains home_short AND away_short
         Otherwise: title contains home_short OR away_short

    ``requires_both_teams`` should be True for broad channels (tier 2 competition
    channel, tier 4 broadcaster playlists) which publish every fixture in the league.
    Without it, a "Rennais vs Nantes" video would pass for a "PSG vs Nantes" fixture
    because "nantes" (away_short) appears in both titles.  Team-scoped sources
    (tiers 1a–1d) leave it False because the channel/playlist is already restricted
    to one club, so only the opponent needs to appear in the title.

    Pagination capped at MAX_YT_PAGES; stops after the first page with an accepted video.
    Calls quota.increment(cap) per page fetched — raises QuotaCapReached if cap hit.
    Raises QuotaCapReached on HTTP 403 (YouTube's own quota exhaustion) so callers can
    save state and exit cleanly rather than crashing with a non-zero exit code.
    """
    fixture_date = datetime.strptime(fixture["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    window_end   = fixture_date + timedelta(days=VIDEO_WINDOW_DAYS)
    home_short   = fixture["home_short"].lower()
    away_short   = fixture["away_short"].lower()
    keywords     = COMPETITION_KEYWORDS.get(comp_name, [])

    accepted:      list[dict] = []
    seen_ids:      set[str]   = set()
    page_token:    str        = ""
    pages_fetched: int        = 0

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
            log.warning(
                f"YouTube API quota exhausted (403) on playlist {playlist_id} "
                "— saving checkpoint and exiting cleanly"
            )
            raise QuotaCapReached(
                f"YouTube returned HTTP 403 on playlist {playlist_id} — quota exhausted"
            )
        if resp.status_code == 404:
            log.warning(f"Playlist {playlist_id} not found (404) — skipping")
            return accepted
        if not resp.ok:
            log.warning(
                f"YouTube API HTTP {resp.status_code} on playlist {playlist_id} — skipping"
            )
            return accepted

        data = resp.json()
        quota.increment(cap)  # raises QuotaCapReached if cap hit
        pages_fetched += 1

        for item in data.get("items", []):
            snippet  = item.get("snippet", {})
            title    = snippet.get("title", "")
            video_id = snippet.get("resourceId", {}).get("videoId", "")
            pub_str  = snippet.get("publishedAt", "")

            if not video_id or not pub_str or video_id in seen_ids:
                continue

            pub_str_date = pub_str[:10]
            try:
                pub_date = datetime.strptime(pub_str_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

            if pub_date < fixture_date or pub_date > window_end:
                continue

            lower_title = title.lower()

            if requires_competition_filter and not any(
                kw in lower_title for kw in keywords
            ):
                continue

            if requires_both_teams:
                if home_short not in lower_title or away_short not in lower_title:
                    continue
            else:
                if home_short not in lower_title and away_short not in lower_title:
                    continue

            # Reject press conferences, interviews, previews, training clips, etc.
            # Applies to all tiers — blocklist is checked inside is_highlight_title()
            # before the allowlist, so blocklist always wins.
            if not is_highlight_title(title):
                continue

            seen_ids.add(video_id)
            accepted.append({
                "video_id":     video_id,
                "title":        title,
                "published_at": pub_str_date,
            })

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
    quota: QuotaTracker,
    cap: int,
) -> list[dict]:
    """
    Try each tier in priority order, stopping at the first that yields ≥1 video.
    Raises QuotaCapReached (propagated from search_playlist) if cap is hit.

    Tier order: 1c → 1d → 2 → 4 → 1a → 1b
    """
    team_pl = config["team_playlists"]
    comp_ch = config["competition_channels"]
    comp_pl = config["competition_playlists"]
    team_ch = config["team_channels"]
    home    = fixture["home_team"]
    away    = fixture["away_team"]

    def _try(
        playlist_id: str,
        tier: int,
        comp_filter: bool = False,
        both_teams: bool = False,
    ) -> list[dict] | None:
        if not playlist_id:
            return None
        vids = search_playlist(
            playlist_id, yt_key, fixture, comp_name, quota, cap,
            requires_competition_filter=comp_filter,
            requires_both_teams=both_teams,
        )
        return [{**v, "tier_used": tier} for v in vids]

    # Tier 1c — home team competition-scoped playlist
    # comp_filter=True: even though this playlist is labelled for one competition,
    # clubs sometimes use the same playlist across competitions (e.g. a Betis
    # LaLiga playlist that also contains Europa League clips).  Requiring a
    # competition keyword in the title prevents cross-competition false positives
    # (e.g. "PFC Ludogorets - Real Betis | HIGHLIGHTS" stored for a LaLiga fixture).
    result = _try(team_pl.get(comp_name, {}).get(home, ""), tier=1, comp_filter=True)
    if result:
        return result

    # Tier 1d — away team competition-scoped playlist (same reasoning as 1c)
    result = _try(team_pl.get(comp_name, {}).get(away, ""), tier=1, comp_filter=True)
    if result:
        return result

    # Tier 2 — official competition channel uploads
    # Requires BOTH team names: a competition channel covers every fixture,
    # so without both-team matching a "Rennais vs Nantes" video would be stored
    # for the "PSG vs Nantes" fixture.
    ch = comp_ch.get(comp_name, "")
    if ch:
        result = _try(channel_to_uploads(ch), tier=2, both_teams=True)
        if result:
            return result

    # Tier 4 — broadcaster playlists (same rationale: broad channels, need both names)
    for _broadcaster, pl_ids in comp_pl.get(comp_name, {}).items():
        for pl_id in pl_ids:
            result = _try(pl_id, tier=4, both_teams=True)
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


# ── Merge helper ──────────────────────────────────────────────────────────────


def merge_into_gw(
    existing: dict | None,
    comp_name: str,
    stem: str,
    enriched_fixtures: list[dict],
) -> tuple[dict, bool]:
    """
    Merge enriched_fixtures into existing gameweek data (or create from scratch).

    Rules:
      - New match_id → append full match object
      - Existing match_id → append only video_ids not already present
      - generated_at updated only when something changed

    Returns (merged_data, changed: bool).
    """
    label = FILE_STEM_LABEL.get(stem, stem)
    if existing is None:
        existing = {
            "competition":  comp_name,
            "gameweek":     label,
            "stem":         stem,
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
                "videos":    new_videos[:],
            }
            changed = True
        else:
            existing_match    = by_id[mid]
            existing_vid_ids  = {v["video_id"] for v in existing_match.get("videos", [])}
            # Also deduplicate by normalised title: channels sometimes publish the
            # same highlight twice with different video IDs but identical titles
            # (e.g. LaLiga GW35 Levante vs Osasuna RESUMEN uploaded twice).
            existing_titles   = {v["title"].strip().lower() for v in existing_match.get("videos", [])}
            for vid in new_videos:
                if vid["video_id"] in existing_vid_ids:
                    continue
                if vid["title"].strip().lower() in existing_titles:
                    log.debug(
                        f"Skipping duplicate title for match {mid}: {vid['title']!r}"
                    )
                    continue
                existing_match.setdefault("videos", []).append(vid)
                existing_vid_ids.add(vid["video_id"])
                existing_titles.add(vid["title"].strip().lower())
                changed = True

    existing["matches"] = list(by_id.values())
    if changed:
        existing["generated_at"] = utc_now_iso()

    return existing, changed


# ── Summary generation ────────────────────────────────────────────────────────


def generate_summary() -> None:
    """
    Scan all existing gameweek/matchday files and write highlights/summary.json.
    Includes all gameweeks for every competition found on disk.

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
    competitions: list[dict] = []

    for comp_name, slug in COMPETITION_SLUG_MAP.items():
        comp_dir = HIGHLIGHTS_DIR / slug
        if not comp_dir.exists():
            continue

        stems = COMPETITION_FILE_STEMS.get(comp_name, [])

        gameweeks: list[dict] = []
        for stem in stems:
            f = comp_dir / f"{stem}.json"
            if not f.exists():
                continue
            data = load_json_file(f)
            if not data:
                continue
            matches_data = data.get("matches", [])
            gameweeks.append({
                "gameweek": FILE_STEM_LABEL.get(stem, stem),
                "stem":     stem,
                "total":    len(matches_data),
                "covered":  sum(1 for m in matches_data if m.get("videos")),
                "matches": [
                    {
                        "match_id": m["match_id"],
                        "home":     m["home_team"],
                        "away":     m["away_team"],
                        "date":     m.get("date", ""),
                        "covered":  bool(m.get("videos")),
                    }
                    for m in matches_data
                ],
            })

        if gameweeks:
            competitions.append({
                "competition": comp_name,
                "slug":        slug,
                "gameweeks":   gameweeks,
            })

    write_json_atomic(
        HIGHLIGHTS_DIR / "summary.json",
        {
            "generated_at": utc_now_iso(),
            "competitions": competitions,
        },
    )
    log.info(f"Written summary.json ({len(competitions)} competition(s))")
