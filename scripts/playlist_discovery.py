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
)
from season_utils import current_season

# Where the resolved current-season playlists + flags are persisted.
DISCOVERED_PATH = HIGHLIGHTS_DIR / "discovered-playlists.json"

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
)

_FOLDED_ALLOWLIST = tuple(_fold(t) for t in TITLE_ALLOWLIST)


def _has_highlight_term(title: str) -> bool:
    folded = _fold(title)
    return any(term in folded for term in _FOLDED_ALLOWLIST)


def _is_decoy(title: str) -> bool:
    low = (title or "").lower()
    return any(term in low for term in _DECOY_TERMS)


def _recency_key(p: dict):
    return (p.get("publishedAt") or "", int(p.get("itemCount") or 0))


# ── The matcher ────────────────────────────────────────────────────────────────


def select_current_season_playlist(
    comp_name: str,
    playlists: "list[dict]",
    *,
    now=None,
    require_competition_gate: bool = True,
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

    survivors = []
    for p in playlists:
        title = p.get("title") or ""
        if _is_decoy(title):
            continue
        if require_competition_gate and kws and not any(k in title.lower() for k in kws):
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


# ── Override loader (used by the fetch path) ────────────────────────────────────


def apply_discovered_overrides(config: dict, *, path: Path = DISCOVERED_PATH) -> dict:
    """Override sources.json rotating playlists with auto-discovered current ones.

    Reads discovered-playlists.json (written by discover_season_playlists.py):
      {generated_at, resolved:{comp:{broadcaster:playlist_id}},
       team:{comp:{team:playlist_id}}, flags:[...]}
    Where a confident current-season playlist was resolved, it replaces the
    hardcoded Tier-4 / Tier-1c-d ID; where absent (no confident match), the
    sources.json last-known-good ID is left untouched. No-op if the file is
    missing, so the pipeline is unchanged until discovery has run.
    """
    data = load_json_file(path)
    if not isinstance(data, dict):
        return config

    comp_pl = config.get("competition_playlists", {})
    for comp, bmap in (data.get("resolved") or {}).items():
        if not isinstance(bmap, dict):
            continue
        dest = comp_pl.setdefault(comp, {})
        for broadcaster, pid in bmap.items():
            if pid:
                dest[broadcaster] = [pid]

    team_pl = config.get("team_playlists", {})
    for comp, tmap in (data.get("team") or {}).items():
        if not isinstance(tmap, dict):
            continue
        dest = team_pl.setdefault(comp, {})
        for team, pid in tmap.items():
            if pid:
                dest[team] = pid

    return config
