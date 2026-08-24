"""Automatic per-season playlist discovery — matcher + helpers.

Given an ALREADY-KNOWN source channel (human-seeded in sources.json), find the
current-season highlights playlist WITHIN it by matching real playlist names, so
rotating season playlists no longer need manual sources.json edits. This does NOT
find unknown channels (that would need the forbidden search.list) — channels stay
the stable human input; only the playlist rotates and is re-discovered.

Design is grounded in a keyed read-only discovery run (real observed titles), not
assumptions:
  • Season strings appear as YYYY/YY, YY/YY, and YYYY-YY; tournaments use an
    edition year ("UEFA Euro 2024", "2026 FIFA World Cup").
  • Highlight terms are multilingual (reuses TITLE_ALLOWLIST) — but are NOT a hard
    gate: real correct playlists like "Ligue 1 2025/26" and "UEFA Europa League
    2025/26" contain no highlight word. Competition-gate + season-match is primary.
  • Multi-comp broadcaster channels (CBS Golazo, TUDN, Fox) carry many
    competitions + decoys, so the COMPETITION-GATE is mandatory there.
  • DECOYS must be excluded (rolling "EVERY…", "Multiple Leagues"/"Scoreline",
    classic/best-of, full-match, interviews/reactions, off-competition).

The pure matcher (select_current_season_playlist) takes a synthetic playlist list
and is fully unit-tested without any network key. The network-backed pieces
(list_channel_playlists) live here too but are exercised only by the orchestrator.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import requests

from highlights_common import (
    COMPETITION_KEYWORDS,
    COMPETITION_SLUG_MAP,
    DOMESTIC_LEAGUE_COMPS,
    HIGHLIGHTS_DIR,
    REPO_ROOT,
    SUMMER_TOURNAMENT_COMPS,
    TITLE_ALLOWLIST,
    YT_PLAYLISTS,
    load_json_file,
    season_for_competition,
    write_json_atomic,
)
from season_utils import current_season

# Where the resolved current-season playlists + flags are persisted.
DISCOVERED_PATH = HIGHLIGHTS_DIR / "discovered-playlists.json"

# Bumped whenever the TEAM (Tier-1c/1d) matcher changes materially. The consumer
# applies team overrides ONLY from files stamped with the current version, so a
# file written by an OLDER/greedier team matcher (e.g. the first run that picked
# bench-cam/pre-season/women's playlists) is IGNORED for team overrides — teams
# fall back to sources.json last-known-good until discovery is re-run with the
# fixed matcher. Competition (Tier-4) overrides are unaffected by this gate.
TEAM_MATCHER_VERSION = 3

# ── Season tokens ──────────────────────────────────────────────────────────────


def current_season_tokens(comp_name: str, now=None) -> "tuple[str, list[str]]":
    """Return (mode, tokens) for the CURRENT season of comp_name.

    Reuses the canonical season logic (no boundary reimplementation):
      • leagues / UCL / UEL  → mode 'season', tokens for YYYY/YY, YY/YY, YYYY-YY
      • summer tournaments    → mode 'edition', a single edition-year token
    """
    if comp_name in SUMMER_TOURNAMENT_COMPS:
        return "edition", [str(season_for_competition(comp_name, now))]
    y = current_season(now)                 # e.g. 2026 → the 2026/27 season
    nn = (y + 1) % 100
    return "season", [f"{y}/{nn:02d}", f"{y % 100:02d}/{nn:02d}", f"{y}-{nn:02d}"]


# ── Text helpers ───────────────────────────────────────────────────────────────

def _fold(s: str) -> str:
    """Lowercase + strip accents so 'RESÚMENES' matches the 'resumen' vocab."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", (s or "").lower())
        if not unicodedata.combining(c)
    )

# Any season string in a title (used to reject past seasons and to identify
# genuinely rolling/no-season playlists).
_SEASON_RE = re.compile(r"20\d{2}\s*[/-]\s*\d{2}|(?<!\d)\d{2}\s*/\s*\d{2}(?!\d)")

# Real decoys observed on the mapped channels. Kept deliberately specific:
# competition cross-picks are handled by the competition-gate; these catch
# same-competition noise (rolling all-time, mixed, non-highlight formats).
_DECOY_TERMS = (
    "every ", "multiple leagues", "scoreline", "classic",
    "best of", "the best", "lo mejor", "goalito",
    "partidos completos", "partido completo",           # full-match replays (ES)
    "and more", "interview", "reaction", "analysis",    # non-highlight formats
    "watch along", "matchday live", "vlog", "mini resumen",
    "u.s. open cup",
    # Throwback / archive (old highlights, not current-season match highlights).
    # Applied on BOTH branches — no legit current playlist carries these.
    "throwback", "archive", "retro", "vintage", "on this day", "flashback",
)

_FOLDED_ALLOWLIST = tuple(_fold(t) for t in TITLE_ALLOWLIST)


def _has_highlight_term(title: str) -> bool:
    folded = _fold(title)
    return any(term in folded for term in _FOLDED_ALLOWLIST)


def _is_decoy(title: str) -> bool:
    low = (title or "").lower()
    return any(term in low for term in _DECOY_TERMS)


# Team-channel NOISE: season-stringed playlists on a club channel that are NOT
# first-team match highlights. Built from the real bad picks in the discovery run
# (bench cam, pre-season, academy, women's, second division, signings,
# individual-player/tribute) plus obvious multilingual siblings. Matched on the
# accent-folded title so "Féminines"/"femenino"/"femminile" all hit. Applied ONLY
# in team_mode — the competition matcher is unaffected.
_TEAM_NOISE_FOLDED = (
    "bench cam",
    # pre-season / friendlies
    "pre-season", "preseason", "pre season", "pretemporada", "vorbereitung",
    "friendly", "friendlies", "amistoso", "amichevoli", "amichevole",
    # academy / youth
    "academy", "youth", "cantera", "primavera", "juvenil", "jugend", "nachwuchs",
    " u16", " u17", " u18", " u19", " u21", " u23", "under-18", "under-19",
    "under-21", "under-23",
    # women
    "women", "femenin", "feminin", "femmin", "frauen", "damen",
    # reserves / B teams / youth training centres
    "castilla", " ii ", " ii|", "b team", "b-team", "reserve",
    "fortuna",                                    # Celta Fortuna (reserve/B team)
    "centre de formation", "centro de formacion", "youth academy",
    # signings / transfers / unveilings
    "signing", "fichaje", "transfer", "unveil", "presentacion",
    # individual-player tribute / record compilations — key on the RECORD/TRIBUTE
    # signal (names are unbounded), accent-folded + multilingual. This is what the
    # Griezmann "all-time top scorer | máximo goleador de la historia" leak needed.
    "record", "tribute", "best goals", "top 10", "top 5", "iconic",
    "every goal", "greatest", "top scorer", "all-time", "all time",
    "maximo goleador", "goleador de la historia", "homenaje", "leyenda",
    "legend", "hall of fame", "most goals", "record breaker",
    # talk / press / behind-the-scenes / tours
    "press conference", "post-match", "post match", "post partita",
    "postpartita", "mic'd", "micd", " tour", "journey", "behind the scenes",
)

# Second-division tokens (collision-prone → regex so "Bundesliga 2026" is NOT
# caught, but "2. Bundesliga" / "Bundesliga 2 |" / "2 Bundesliga" is). Applied on
# BOTH branches for DOMESTIC leagues (the top-flight source must never resolve its
# own second division). Deliberately NOT applied to tournaments so "European
# Championship" (Euro Cup) is never mistaken for the English Championship.
_SECOND_DIV_RE = re.compile(
    r"\b2\.\s*bundesliga\b|\bbundesliga\s+2\b(?!\d)|\b2\s+bundesliga\b|"
    r"\bserie\s+b\b|\bsegunda\b|\bligue\s+2\b|\bchampionship\b")


def _is_team_noise(title: str) -> bool:
    return any(term in _fold(title) for term in _TEAM_NOISE_FOLDED)


def _recency_key(p: dict):
    return (p.get("publishedAt") or "", int(p.get("itemCount") or 0))


# ── The matcher ────────────────────────────────────────────────────────────────


def select_current_season_playlist(
    comp_name: str,
    playlists: "list[dict]",
    *,
    now=None,
    require_competition_gate: bool = True,
    team_mode: bool = False,
) -> "dict | None":
    """Pick the current-season highlights playlist from a channel's playlist list.

    [playlists] items: {title, id, itemCount, publishedAt}. Returns the chosen
    playlist dict, or None when there is no confident match (caller keeps
    last-known-good and flags).

    Priority (grounded in the real title data):
      1. Exclude decoys (rolling "EVERY…"/mixed/non-highlight/full-match).
      2. Competition-gate (mandatory for multi-comp channels; skipped for
         single-team channels where the title omits the competition name).
      3. Prefer a title carrying the CURRENT season token (YYYY/YY, YY/YY,
         YYYY-YY, or edition year); among those, prefer one that also has a
         highlight term, then most-recent/most-populated.
      4. No-season fallback: if none carry the current season, accept a genuinely
         rolling comp playlist — a survivor with NO season string at all AND a
         highlight term (e.g. "LALIGA Highlights | ESPN FC"); most-recent wins.
         A survivor bearing a NON-current season string is neither current nor
         rolling and is rejected (never adopt a stale/past playlist).
    """
    mode, tokens = current_season_tokens(comp_name, now)
    kws = [k.lower() for k in COMPETITION_KEYWORDS.get(comp_name, [])]

    is_domestic = comp_name in DOMESTIC_LEAGUE_COMPS
    survivors = []
    for p in playlists:
        title = p.get("title") or ""
        if _is_decoy(title):
            continue
        # Second-division leak (COMPETITION and team branches): a top-flight
        # domestic source must never resolve its own second division
        # ("2. Bundesliga", "Serie B", "Segunda", "Ligue 2", "Championship").
        # Targets the division TOKEN, so "Bundesliga | Highlights 2026/27" (top
        # flight, mere year) still passes. Skipped for tournaments (no 2nd tier).
        if is_domestic and _SECOND_DIV_RE.search(_fold(title)):
            continue
        # TEAM channels carry many season-stringed non-first-team playlists (bench
        # cam, pre-season, academy, women's, reserves, second division, signings,
        # player tributes). In team_mode: reject that noise AND require a positive
        # highlights/match term — a season string alone is NOT sufficient (that was
        # the greedy bug). Season string stays a preference (current/rolling below),
        # so terse correct titles ("BUTS", "RÉSUMÉ DE MATCH") still pass.
        if team_mode and _is_team_noise(title):
            continue
        if require_competition_gate and kws and not any(k in title.lower() for k in kws):
            continue
        if team_mode and not _has_highlight_term(title):
            continue
        survivors.append(p)
    if not survivors:
        return None

    current = [p for p in survivors
               if any(tok in (p.get("title") or "").lower() for tok in tokens)]
    if current:
        current.sort(key=lambda p: (_has_highlight_term(p["title"]), _recency_key(p)),
                     reverse=True)
        return current[0]

    if mode == "edition":
        # Tournaments: only a current-edition-year match is acceptable; there is
        # no meaningful "rolling" fallback for a specific edition.
        return None

    # Season mode: rolling fallback = no season string at all + a highlight term.
    rolling = [p for p in survivors
               if not _SEASON_RE.search(p.get("title") or "")
               and _has_highlight_term(p["title"])]
    if rolling:
        rolling.sort(key=_recency_key, reverse=True)
        return rolling[0]
    return None


# ── Availability scoping ───────────────────────────────────────────────────────


def is_competition_available(comp_name: str, *, repo_root: Path = REPO_ROOT) -> bool:
    """Whether the competition currently has fixtures to attach highlights to.

    Self-updating signal (no FD call, no hardcoded skip list):
      • domestic leagues → a fixtures/{slug}/{season}.json with ≥1 fixture
      • tournaments      → tournament-groups/{slug}.json with ≥1 DATED match
                           (utcDate + id) — the same "dated" notion the home-index
                           uses.
    Yields available = {PL, LaLiga, Serie A, Bundesliga, Ligue 1, UCL, Euro, WC};
    UNAVAILABLE = {Europa League (no file), Copa América (0 dated matches)}. If
    those gain fixtures later, they auto-include with no code change.
    """
    slug = COMPETITION_SLUG_MAP.get(comp_name)
    if not slug:
        return False
    if comp_name in DOMESTIC_LEAGUE_COMPS:
        d = repo_root / "fixtures" / slug
        if not d.is_dir():
            return False
        for f in d.glob("*.json"):
            data = load_json_file(f)
            if isinstance(data, dict) and data.get("fixtures"):
                return True
        return False
    tg = load_json_file(repo_root / "tournament-groups" / f"{slug}.json")
    if not isinstance(tg, dict):
        return False
    for m in list(tg.get("matches", [])) + list(tg.get("groupMatches", [])):
        if m.get("utcDate") and (m.get("id") or m.get("match_id")):
            return True
    return False


def available_competitions(*, repo_root: Path = REPO_ROOT) -> "set[str]":
    return {c for c in COMPETITION_SLUG_MAP
            if is_competition_available(c, repo_root=repo_root)}


# ── Channel playlist listing (network; used by the orchestrator only) ───────────


def list_channel_playlists(
    channel_id: str,
    api_key: str,
    *,
    session: "requests.Session | None" = None,
    max_pages: int = 5,
    counter: "list[int] | None" = None,
) -> "list[dict]":
    """List a channel's playlists via playlists.list?part=snippet,contentDetails.

    Read-only, ≤max_pages (1 unit/page). Returns [{title, id, itemCount,
    publishedAt}]. [counter] (a 1-cell list) accumulates unit usage when given.
    """
    http = session if session is not None else requests.Session()
    out: list[dict] = []
    token = ""
    for _ in range(max_pages):
        params = {"part": "snippet,contentDetails", "channelId": channel_id,
                  "maxResults": 50, "key": api_key}
        if token:
            params["pageToken"] = token
        resp = http.get(YT_PLAYLISTS, params=params, timeout=30)
        if counter is not None:
            counter[0] += 1
        if resp.status_code != 200:
            break
        data = resp.json()
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            out.append({
                "title":       sn.get("title", ""),
                "id":          it.get("id", ""),
                "itemCount":   it.get("contentDetails", {}).get("itemCount"),
                "publishedAt": sn.get("publishedAt", ""),
            })
        token = data.get("nextPageToken", "")
        if not token:
            break
    return out


# ── Season-keyed additive store: schema, migration, merge, write ────────────────
#
# discovered-playlists.json (season-nested, additive):
#   {
#     generated_at, current_season, flags, estimated_units,   # run metadata
#     resolved: { "<season>": { comp: { broadcaster: playlist_id } } },
#     team:     { "<season>": { comp: { team:        playlist_id } } },
#   }
# Each run MERGES its resolutions into the existing file (deep-merge that only
# adds/updates leaves — never deletes), so prior seasons AND same-season prior
# resolutions the current run didn't re-resolve are preserved.

_SEASON_KEY_RE = re.compile(r"^\d{4}$")


def _is_season_nested(mapping: dict) -> bool:
    """True if a resolved/team mapping is already season-nested (top keys are years).

    An empty mapping counts as new-shape (nothing to migrate). The OLD flat shape
    has competition names as top keys, which never match ^\\d{4}$.
    """
    if not mapping:
        return True
    return all(_SEASON_KEY_RE.fullmatch(str(k)) for k in mapping)


def migrate_flat_discovered(data: "dict | None") -> dict:
    """Return {resolved, team} in season-nested shape, migrating the OLD flat shape.

    Old flat shape ({resolved:{comp:{...}}}) is nested under the season named by
    the file's own `current_season` stamp — the honest season it represented (no
    guessing). Missing/corrupt/None → empty. Already-nested → returned as-is.
    """
    if not isinstance(data, dict):
        return {"resolved": {}, "team": {}}
    res = data.get("resolved") or {}
    tm  = data.get("team") or {}
    if _is_season_nested(res) and _is_season_nested(tm):
        return {"resolved": dict(res), "team": dict(tm)}
    season = str(data.get("current_season") or "").strip()
    if not season:
        # Flat shape with no season stamp → cannot place honestly; treat as empty.
        return {"resolved": {}, "team": {}}
    out = {"resolved": {}, "team": {}}
    if not _is_season_nested(res):
        out["resolved"] = {season: dict(res)}
    elif res:
        out["resolved"] = dict(res)
    if not _is_season_nested(tm):
        out["team"] = {season: dict(tm)}
    elif tm:
        out["team"] = dict(tm)
    return out


def _deep_merge_seasons(base: dict, incoming: dict) -> dict:
    """Deep-merge season→comp→source→id: add/update leaves, never delete."""
    for season, comps in incoming.items():
        b_comps = base.setdefault(str(season), {})
        for comp, srcs in comps.items():
            b_comps.setdefault(comp, {}).update(srcs)
    return base


def merge_discovered_seasons(existing: dict, run_resolved: dict, run_team: dict) -> dict:
    """Merge this run's season-nested resolutions into the existing store.

    [existing] is the migrated {resolved, team}. Returns a NEW {resolved, team}
    with this run's entries added/updated and all other seasons/comps preserved.
    """
    merged = {
        "resolved": {s: {c: dict(v) for c, v in comps.items()}
                     for s, comps in (existing.get("resolved") or {}).items()},
        "team":     {s: {c: dict(v) for c, v in comps.items()}
                     for s, comps in (existing.get("team") or {}).items()},
    }
    _deep_merge_seasons(merged["resolved"], run_resolved)
    _deep_merge_seasons(merged["team"], run_team)
    return merged


def write_discovered_if_changed(path: Path, payload: dict) -> bool:
    """Write payload only if the meaningful content changed (ignoring run metadata).

    Keeps same-input re-runs diff-free: generated_at / estimated_units churn every
    run, so they are excluded from the comparison. Returns True if written.
    """
    existing = load_json_file(path)
    if isinstance(existing, dict):
        strip = lambda d: {k: v for k, v in d.items()
                           if k not in ("generated_at", "estimated_units")}
        if strip(existing) == strip(payload):
            return False
    write_json_atomic(path, payload)
    return True


# ── Source-entry leaf shape (holds BOTH formats + reserved format field) ────────
#
# Each (season → comp → source) leaf can represent EITHER a single season playlist
# OR a per-gameweek collection, tagged by a reserved "format" field:
#   {"format": "season" | "gameweek" | "undetermined",
#    "playlist_id": "PL…",             # season-format: one playlist for the season
#    "gameweeks": {"5": "PL…", ...}}   # gameweek-format: a playlist per gameweek
# Format DETECTION (which one a source is) is a separate concern — writers reserve
# it as "undetermined" unless trivially known, so detection drops in later without
# reshaping the store. A bare-string leaf is ALSO accepted on read (the shape that
# shipped before this reshape, and migrated flat entries) and treated as a
# season playlist_id.

def season_leaf(playlist_id: str, fmt: str = "undetermined") -> dict:
    """Build a single-season-playlist leaf (format reserved, default undetermined)."""
    return {"format": fmt, "playlist_id": playlist_id}


def gameweek_leaf(gameweeks: dict, fmt: str = "gameweek") -> dict:
    """Build a per-gameweek collection leaf: {gw_number(str): playlist_id}."""
    return {"format": fmt, "gameweeks": dict(gameweeks)}


def leaf_playlist_id(leaf) -> "str | None":
    """Extract the single override playlist id from a leaf, tolerating BOTH the
    bare-string shape and the object shape. Returns None for a gameweek-only leaf
    (no single season id — per-gameweek consumption is handled by the existing
    Tier-2 find_gameweek_playlist path, not this override), so the caller applies
    no Tier-4 override and never crashes on the gameweek shape.
    """
    if isinstance(leaf, str):
        return leaf or None
    if isinstance(leaf, dict):
        pid = leaf.get("playlist_id")
        return pid if isinstance(pid, str) and pid else None
    return None


# ── Override loader (used by the fetch path) ────────────────────────────────────


def apply_discovered_overrides(config: dict, *, path: Path = DISCOVERED_PATH,
                               season: "int | None" = None, now=None) -> dict:
    """Override sources.json rotating playlists with auto-discovered current ones.

    Season-aware: for each competition it looks up the resolution under the season
    it is actually processing — season_for_competition(comp) by default (the same
    per-competition season the fetch uses to pick fixtures), or an explicit
    [season] override (e.g. a backfill of a specific season). An entry is applied
    ONLY when it exists for that competition's target season; otherwise the
    sources.json last-known-good ID is left untouched — a different season's ID is
    never cross-applied. Tolerates the OLD flat file shape (migrated on read) and a
    missing/corrupt file (no-op).
    """
    data = load_json_file(path)
    if not isinstance(data, dict):
        return config
    nested = migrate_flat_discovered(data)
    res_by_season = nested.get("resolved") or {}
    team_by_season = nested.get("team") or {}

    def _target_season(comp: str) -> str:
        return str(season if season is not None else season_for_competition(comp, now))

    comp_pl = config.get("competition_playlists", {})
    for comp in {c for comps in res_by_season.values() for c in comps}:
        bmap = res_by_season.get(_target_season(comp), {}).get(comp, {})
        if not isinstance(bmap, dict) or not bmap:
            continue  # no entry for this comp's target season → last-known-good
        dest = comp_pl.setdefault(comp, {})
        for broadcaster, leaf in bmap.items():
            pid = leaf_playlist_id(leaf)   # None for gameweek-only leaves → skip
            if pid:
                dest[broadcaster] = [pid]

    # TEAM overrides apply ONLY when the file was written by the current team
    # matcher. A file from the old greedy matcher (no/old version stamp) is
    # ignored for teams → sources.json last-known-good is used instead. Competition
    # overrides above are always applied.
    if data.get("team_matcher_version") == TEAM_MATCHER_VERSION:
        team_pl = config.get("team_playlists", {})
        for comp in {c for comps in team_by_season.values() for c in comps}:
            tmap = team_by_season.get(_target_season(comp), {}).get(comp, {})
            if not isinstance(tmap, dict) or not tmap:
                continue
            dest = team_pl.setdefault(comp, {})
            for team, leaf in tmap.items():
                pid = leaf_playlist_id(leaf)
                if pid:
                    dest[team] = pid

    return config
