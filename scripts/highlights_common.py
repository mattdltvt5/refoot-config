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
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from season_utils import current_season  # canonical August-threshold rule

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT              = Path(__file__).resolve().parent.parent
SOURCES_JSON           = REPO_ROOT / "sources.json"
HIGHLIGHTS_DIR         = REPO_ROOT / "highlights"
FIXTURES_DIR           = REPO_ROOT / "fixtures"
RESULTS_DIR            = REPO_ROOT / "results"   # durable results ledger (self-healing)
QUOTA_TRACKER_PATH     = HIGHLIGHTS_DIR / "quota-tracker.json"
PLAYLIST_OWNERS_PATH   = HIGHLIGHTS_DIR / "playlist-owners.json"
BACKFILL_PROGRESS_PATH = HIGHLIGHTS_DIR / "backfill-progress.json"
BACKFILL_LOCK_PATH     = HIGHLIGHTS_DIR / "backfill.lock"

# ── API endpoints ─────────────────────────────────────────────────────────────

FD_BASE     = "https://api.football-data.org/v4"
YT_PLAYLIST  = "https://www.googleapis.com/youtube/v3/playlistItems"
YT_VIDEOS    = "https://www.googleapis.com/youtube/v3/videos"
YT_PLAYLISTS = "https://www.googleapis.com/youtube/v3/playlists"
MIN_VIDEO_DURATION_SECONDS = 55    # reject clips shorter than 55s (Shorts, social clips)

# ── Manual crest overrides (per football-data team id) ──────────────────────────
#
# For teams whose football-data crest is broken IN THE APP, map the FD team id to a
# verified-renderable replacement, applied wherever crests are ingested from FD
# (fixtures, standings, rosters). NOT a global sweep — only known-broken crests.
#
# Le Mans FC (535): FD has no hosted crest, so it hotlinks a Wikipedia SVG whose 14
# paths take their fills ONLY from a <style> block (class-based + gradient url()
# refs), with zero inline fill= attributes. flutter_svg 2.3.0 can't resolve
# style-block/gradient fills, so every path defaults to black → solid black
# silhouette. Replacement is Wikimedia's server-rendered COLOUR PNG of the same
# crest (verified HTTP 200, real colours, not a silhouette) — a raster the app
# loads via Image, bypassing flutter_svg entirely.
CREST_OVERRIDES = {
    535: "https://upload.wikimedia.org/wikipedia/en/thumb/5/57/Le_Mans_FC_logo.svg/330px-Le_Mans_FC_logo.svg.png",
}


def override_crest(team_id, crest):
    """Return the override crest for a football-data team id, else the given crest."""
    return CREST_OVERRIDES.get(team_id, crest)

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
    "Copa America":     "copa-america",
}

# UCL/UEL use "matchday-N.json"; domestic leagues use "gameweek-N.json"
UCL_UEL: set[str] = {"Champions League", "Europa League"}

# The five domestic leagues that get a dedicated fixtures/{slug}.json artifact.
# Excludes UCL, UEL, and summer tournaments (those use tournament-groups/ files).
DOMESTIC_LEAGUE_COMPS: set[str] = {
    "Premier League", "LaLiga", "Serie A", "Bundesliga", "Ligue 1",
}

# Competitions with two-legged knockout rounds
TWO_LEGGED_COMPS: set[str] = {"Champions League", "Europa League"}

# Competitions that use stage-aware file naming (knockout rounds)
STAGE_AWARE_COMPS: set[str] = {"Champions League", "Europa League", "Euro Cup", "World Cup", "Copa America"}

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
    "Copa America":     ["copa america", "conmebol copa america", "copa"],
}

# Terms that indicate a video belongs to a DIFFERENT competition than the target.
# Used by Tier 1a/1b (team channel uploads) as an exclusion filter so creative/viral
# titles without competition names (e.g. "Buendía's 95′ WINNER! | Villa 2-1 Arsenal")
# are accepted while cup and European knockout content is rejected.
# All entries must be pre-normalised: lowercase, accent-free (NFKD-stripped).
# Deliberately omit bare "final"/"finale"/"semi" — too common as adjectives in titles.
# Bare "leg"/"legs" also omitted — standard football vocabulary ("leg tackle" etc.).
COMP_EXCLUSION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Premier League": (
        # Named competitions
        "fa cup", "carabao cup", "league cup", "champions league", "ucl",
        "europa league", "uel", "conference league", "uecl",
        "community shield", "fa community shield",
        # Knockout-round format indicators
        "quarter-final", "quarter final", "semi-final", "semi final",
        "first leg", "second leg", "1st leg", "2nd leg",
        "round of 16", "round of 32", "round of 8",
        # Generic cup word
        "cup",
    ),
    "LaLiga": (
        # Named competitions
        "copa del rey", "supercopa", "champions league", "ucl",
        "europa league", "uel", "conference league", "uecl",
        # Knockout-round format indicators (Spanish + universal)
        "cuartos de final", "semifinal",
        "partido de ida", "partido de vuelta",
        "quarter-final", "quarter final", "semi-final", "semi final",
        "first leg", "second leg", "1st leg", "2nd leg",
        "round of 16", "round of 32",
        # Generic cup word (Spanish)
        "copa",
    ),
    "Bundesliga": (
        # Named competitions
        "dfb-pokal", "dfb pokal", "supercup", "champions league", "ucl",
        "europa league", "uel", "conference league", "uecl",
        # Knockout-round format indicators (German + universal)
        "viertelfinale", "halbfinale", "hinspiel", "ruckspiel",
        "quarter-final", "quarter final", "semi-final", "semi final",
        "first leg", "second leg", "1st leg", "2nd leg",
        "round of 16", "round of 32",
        # Generic cup word (German)
        "pokal",
    ),
    "Serie A": (
        # Named competitions
        "coppa italia", "supercoppa", "champions league", "ucl",
        "europa league", "uel", "conference league", "uecl",
        # Knockout-round format indicators (Italian + universal)
        # bare "finale" omitted — used as adjective: "gol al minuto finale"
        "quarti di finale", "semifinale",
        "gara di andata", "gara di ritorno",
        "quarter-final", "quarter final", "semi-final", "semi final",
        "first leg", "second leg", "1st leg", "2nd leg",
        "round of 16", "round of 32",
        # Generic cup word (Italian)
        "coppa",
    ),
    "Ligue 1": (
        # Named competitions
        "coupe de france", "coupe de la ligue", "trophee des champions",
        "champions league", "ucl", "europa league", "uel",
        "conference league", "uecl",
        # Knockout-round format indicators (French + universal)
        # bare "finale" omitted — used as adjective in French too
        "quart de finale", "quarts de finale", "demi-finale", "demi finale",
        "match aller", "match retour",
        "quarter-final", "quarter final", "semi-final", "semi final",
        "first leg", "second leg", "1st leg", "2nd leg",
        "round of 16", "round of 32",
        # Generic cup word (French)
        "coupe",
    ),
}

# ── Title normalisation ───────────────────────────────────────────────────────

# TLAs shorter than this are excluded from auto-derived candidates to prevent
# two- and three-letter codes ("OL", "OM", "FCB") from substring-matching
# unrelated words in titles.  Explicit TEAM_TITLE_ALIASES entries bypass this.
_MIN_TLA_LEN: int = 4

# Minimum length for any auto-derived token (prevents very short stripped forms).
_MIN_AUTO_TOKEN_LEN: int = 4


def _normalize(s: str) -> str:
    """NFKD decompose, drop combining marks, then casefold.

    "Barça" → "barca", "Atlético" → "atletico", "Bayern München" → "bayern munchen".
    Produces a plain-ASCII-friendly string safe for substring matching without
    false-negative diacritic mismatches.
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if unicodedata.category(c) != "Mn"
    ).casefold()


# ── Smart auto-alias derivation (fallback for teams not in TEAM_TITLE_ALIASES) ─
#
# Newly promoted / relegated teams that have no explicit entry still get a
# reasonable candidate set by progressively stripping common suffixes, year
# numbers, and geographic qualifiers from the FD full name.
#
# Examples (FD name → additional auto-derived candidates):
#   "Bologna FC 1909"           → "Bologna FC", "Bologna"
#   "1. FC Heidenheim 1846"     → "Heidenheim 1846", "Heidenheim"
#   "Parma Calcio 1913"         → "Parma Calcio", "Parma"
#   "Rayo Vallecano de Madrid"  → "Rayo Vallecano"
#   "Real Sociedad de Fútbol"   → "Real Sociedad"
#   "Stade Rennais FC 1901"     → "Stade Rennais 1901", "Stade Rennais"

# Remove "1. FC/FSV/SV …" prefixes common in German football.
_RE_NUM_PREFIX = re.compile(r"^1\.\s+(?:F[CS]?V?\.?\s*)?", re.IGNORECASE)

# Remove trailing organisational suffixes (can appear multiple times, e.g. "AFC").
_RE_ORG_SUFFIX = re.compile(
    r"(?:\s+(?:"
    r"F\.?C\.?|A\.?F\.?C\.?|S\.?C\.?|S\.?V\.?|A\.?C\.?|B\.?C\.?|"
    r"S\.?S\.?|U\.?S\.?|U\.?D\.?|C\.?F\.?|"
    r"Calcio|Balompi[eé]|Football\s+Club"
    r"))+\s*$",
    re.IGNORECASE,
)

# Remove trailing year-like numbers (four-digit years or short suffixes like "29").
_RE_YEAR_SUFFIX = re.compile(r"\s+\b(?:1[89]\d{2}|20\d{2}|\d{1,2})\b\s*$")

# Remove trailing geographic preposition + one or two words ("de Madrid", "de Vigo",
# "de Fútbol", "von Bremen", "van Amsterdam").
_RE_GEO_SUFFIX = re.compile(
    r"\s+\b(?:de\s+la\b|de\b|del\b|von\b|van\b|of\b)\s+\S+(?:\s+\S+)?\s*$",
    re.IGNORECASE,
)


def _auto_tokens(team_name: str, short_name: str, tla: str = "") -> list[str]:
    """Derive matching tokens for a team without a TEAM_TITLE_ALIASES entry.

    Returns the normalised FD name + shortName as the base, then adds
    progressively stripped variants so that promoted teams work without a manual
    alias entry.  Tokens shorter than _MIN_AUTO_TOKEN_LEN are discarded.
    """
    seen: set[str] = set()
    result: list[str] = []

    def _add(raw: str) -> None:
        n = _normalize(raw.strip())
        if n and n not in seen:
            seen.add(n)
            result.append(n)

    _add(team_name)
    _add(short_name)

    # Progressive stripping: num-prefix → year → org-suffix → year again → geo
    s = _RE_NUM_PREFIX.sub("", team_name).strip()
    _add(s)
    prev = None
    while s != prev:
        prev = s
        s = _RE_YEAR_SUFFIX.sub("", s).strip()
        _add(s)
        s = _RE_ORG_SUFFIX.sub("", s).strip()
        _add(s)
    s = _RE_GEO_SUFFIX.sub("", s).strip()
    _add(s)

    if tla and len(tla) >= _MIN_TLA_LEN:
        _add(tla)

    filtered = [t for t in result if len(t) >= _MIN_AUTO_TOKEN_LEN]
    return filtered or [_normalize(short_name) or _normalize(team_name)]


# ── Team title alias map ──────────────────────────────────────────────────────
#
# Explicit token sets for every known team across all tracked competitions.
# An entry REPLACES the _auto_tokens() set entirely for that team.
#
# Rules:
#   - Key  = exact FD team.name (same string as home_team/away_team in JSON)
#   - Value = all title forms a broadcaster might use; any one is sufficient
#   - For newly promoted/relegated clubs, _auto_tokens() is an adequate fallback
#   - For national teams, ALWAYS add an explicit entry here.  _auto_tokens()
#     produces only one token (TLAs are 3 chars, filtered by _MIN_TLA_LEN) and
#     cannot derive alternate-language name forms (e.g. "Turkey" vs "Türkiye").
#   - Paris FC MUST NOT include bare "Paris" — it would absorb PSG videos
#   - FC Barcelona includes bare "Barcelona" intentionally — broadcast short-form
#     titles like "Barcelona 3-2 Atletico" require it; tier-4 false-positive risk
#     with Espanyol is low because broadcasters use "Espanyol", not the full city name
#   - Real Madrid MUST NOT include bare "Madrid" — shared with Atlético/Rayo

TEAM_TITLE_ALIASES: dict[str, list[str]] = {
    # ── Premier League ───────────────────────────────────────────────────────
    "AFC Bournemouth":             ["AFC Bournemouth", "Bournemouth"],
    "Arsenal FC":                  ["Arsenal FC", "Arsenal"],
    "Aston Villa FC":              ["Aston Villa FC", "Aston Villa", "AVFC", "Villa"],
    "Brentford FC":                ["Brentford FC", "Brentford"],
    "Brighton & Hove Albion FC":   ["Brighton & Hove Albion FC", "Brighton & Hove Albion", "Brighton"],
    "Burnley FC":                  ["Burnley FC", "Burnley"],
    "Chelsea FC":                  ["Chelsea FC", "Chelsea"],
    "Crystal Palace FC":           ["Crystal Palace FC", "Crystal Palace"],
    "Everton FC":                  ["Everton FC", "Everton"],
    "Fulham FC":                   ["Fulham FC", "Fulham"],
    "Leeds United FC":             ["Leeds United FC", "Leeds United", "Leeds"],
    "Liverpool FC":                ["Liverpool FC", "Liverpool"],
    "Manchester City FC":          ["Manchester City FC", "Manchester City", "Man City"],
    "Manchester United FC":        ["Manchester United FC", "Manchester United", "Man United", "Man Utd"],
    "Newcastle United FC":         ["Newcastle United FC", "Newcastle United", "Newcastle"],
    "Nottingham Forest FC":        ["Nottingham Forest FC", "Nottingham Forest", "Nottm Forest", "Nott'm Forest"],
    "Sunderland AFC":              ["Sunderland AFC", "Sunderland"],
    "Tottenham Hotspur FC":        ["Tottenham Hotspur FC", "Tottenham Hotspur", "Tottenham", "Spurs"],
    "West Ham United FC":          ["West Ham United FC", "West Ham United", "West Ham"],
    "Wolverhampton Wanderers FC":  ["Wolverhampton Wanderers FC", "Wolverhampton Wanderers", "Wolverhampton", "Wolves"],

    # ── LaLiga ───────────────────────────────────────────────────────────────
    "Athletic Club":               ["Athletic Club", "Athletic Bilbao", "Athletic"],
    "CA Osasuna":                  ["CA Osasuna", "Osasuna"],
    "Club Atlético de Madrid":     ["Club Atlético de Madrid", "Club Atletico de Madrid",
                                    "Atlético de Madrid", "Atletico de Madrid",
                                    "Atlético Madrid", "Atletico Madrid",
                                    "Atlético", "Atletico", "Atleti"],
    "Deportivo Alavés":            ["Deportivo Alavés", "Deportivo Alaves", "Alavés", "Alaves"],
    "Elche CF":                    ["Elche CF", "Elche"],
    "FC Barcelona":                ["FC Barcelona", "Barcelona", "Barça", "Barca"],
    "Getafe CF":                   ["Getafe CF", "Getafe"],
    "Girona FC":                   ["Girona FC", "Girona"],
    "Levante UD":                  ["Levante UD", "Levante"],
    "RC Celta de Vigo":            ["RC Celta de Vigo", "Celta de Vigo", "RC Celta", "Celta Vigo", "Celta"],
    "RCD Espanyol de Barcelona":   ["RCD Espanyol de Barcelona", "Espanyol de Barcelona", "RCD Espanyol", "Espanyol"],
    "RCD Mallorca":                ["RCD Mallorca", "Mallorca"],
    "Rayo Vallecano de Madrid":    ["Rayo Vallecano de Madrid", "Rayo Vallecano", "Rayo"],
    "Real Betis Balompié":         ["Real Betis Balompié", "Real Betis Balompie", "Real Betis", "Betis"],
    "Real Madrid CF":              ["Real Madrid CF", "Real Madrid"],    # NOT bare "Madrid"
    "Real Oviedo":                 ["Real Oviedo", "Oviedo"],
    "Real Sociedad de Fútbol":     ["Real Sociedad de Fútbol", "Real Sociedad de Futbol", "Real Sociedad"],
    "Sevilla FC":                  ["Sevilla FC", "Sevilla"],
    "Valencia CF":                 ["Valencia CF", "Valencia"],
    "Villarreal CF":               ["Villarreal CF", "Villarreal"],

    # ── Serie A ──────────────────────────────────────────────────────────────
    "AC Milan":                    ["AC Milan", "Milan"],
    "AC Pisa 1909":                ["AC Pisa 1909", "AC Pisa", "Pisa"],
    "ACF Fiorentina":              ["ACF Fiorentina", "Fiorentina"],
    "AS Roma":                     ["AS Roma", "Roma"],
    "Atalanta BC":                 ["Atalanta BC", "Atalanta"],
    "Bologna FC 1909":             ["Bologna FC 1909", "Bologna FC", "Bologna"],
    "Cagliari Calcio":             ["Cagliari Calcio", "Cagliari"],
    "Como 1907":                   ["Como 1907", "Como"],
    "FC Internazionale Milano":    ["FC Internazionale Milano", "Internazionale Milano",
                                    "Inter Milan", "Internazionale", "Inter"],
    "Genoa CFC":                   ["Genoa CFC", "Genoa"],
    "Hellas Verona FC":            ["Hellas Verona FC", "Hellas Verona", "Verona"],
    "Juventus FC":                 ["Juventus FC", "Juventus", "Juve"],
    "Parma Calcio 1913":           ["Parma Calcio 1913", "Parma Calcio", "Parma"],
    "SS Lazio":                    ["SS Lazio", "Lazio"],
    "SSC Napoli":                  ["SSC Napoli", "Napoli"],
    "Torino FC":                   ["Torino FC", "Torino"],
    "US Cremonese":                ["US Cremonese", "Cremonese"],
    "US Lecce":                    ["US Lecce", "Lecce"],
    "US Sassuolo Calcio":          ["US Sassuolo Calcio", "Sassuolo Calcio", "Sassuolo"],
    "Udinese Calcio":              ["Udinese Calcio", "Udinese"],

    # ── Bundesliga ───────────────────────────────────────────────────────────
    "1. FC Heidenheim 1846":       ["1. FC Heidenheim 1846", "FC Heidenheim", "Heidenheim"],
    "1. FC Köln":                  ["1. FC Köln", "1. FC Koln", "FC Köln", "FC Koln", "Köln", "Koln", "Cologne"],
    "1. FC Union Berlin":          ["1. FC Union Berlin", "FC Union Berlin", "Union Berlin"],
    "1. FSV Mainz 05":             ["1. FSV Mainz 05", "FSV Mainz 05", "Mainz 05", "Mainz"],
    "Bayer 04 Leverkusen":         ["Bayer 04 Leverkusen", "Bayer Leverkusen", "Leverkusen"],
    "Borussia Dortmund":           ["Borussia Dortmund", "BVB", "Dortmund"],
    "Borussia Mönchengladbach":    ["Borussia Mönchengladbach", "Borussia Monchengladbach",
                                    "Mönchengladbach", "Monchengladbach", "Gladbach"],
    "Eintracht Frankfurt":         ["Eintracht Frankfurt", "Frankfurt"],
    "FC Augsburg":                 ["FC Augsburg", "Augsburg"],
    "FC Bayern München":           ["FC Bayern München", "FC Bayern Munich",
                                    "Bayern München", "Bayern Munich", "Bayern"],
    "FC St. Pauli 1910":           ["FC St. Pauli 1910", "FC St. Pauli", "St. Pauli"],
    "Hamburger SV":                ["Hamburger SV", "HSV", "Hamburg"],
    "RB Leipzig":                  ["RB Leipzig", "Leipzig"],
    "SC Freiburg":                 ["SC Freiburg", "Freiburg"],
    "SV Werder Bremen":            ["SV Werder Bremen", "Werder Bremen", "Werder"],
    "TSG 1899 Hoffenheim":         ["TSG 1899 Hoffenheim", "TSG Hoffenheim", "1899 Hoffenheim", "Hoffenheim"],
    "VfB Stuttgart":               ["VfB Stuttgart", "Stuttgart"],
    "VfL Wolfsburg":               ["VfL Wolfsburg", "Wolfsburg"],

    # ── Ligue 1 ──────────────────────────────────────────────────────────────
    "AJ Auxerre":                  ["AJ Auxerre", "Auxerre", "AJA"],
    "AS Monaco FC":                ["AS Monaco", "Monaco", "ASM"],
    "Angers SCO":                  ["Angers SCO", "Angers", "SCO Angers"],
    "FC Lorient":                  ["FC Lorient", "Lorient"],
    "FC Metz":                     ["FC Metz", "Metz"],
    "FC Nantes":                   ["FC Nantes", "Nantes"],
    "Le Havre AC":                 ["Le Havre AC", "Le Havre", "Havre AC", "HAC"],
    "Lille OSC":                   ["LOSC", "Lille LOSC", "Lille OSC", "Lille"],
    "OGC Nice":                    ["OGC Nice", "Nice"],
    "Olympique Lyonnais":          ["Olympique Lyonnais", "Lyon", "OL"],
    "Olympique de Marseille":      ["Olympique de Marseille", "Olympique Marseille", "Marseille", "OM"],
    "Paris FC":                    ["Paris FC", "PFC"],   # must NOT include bare "Paris" — see above
    "Paris Saint-Germain FC":      ["Paris Saint-Germain", "Paris Saint Germain", "PSG", "Paris SG"],
    "RC Strasbourg Alsace":        ["RC Strasbourg Alsace", "RC Strasbourg", "Strasbourg", "RCSA"],
    "Racing Club de Lens":         ["Racing Club de Lens", "RC Lens", "Lens", "RCL"],
    "Stade Brestois 29":           ["Stade Brestois", "Stade Brest", "Brestois", "Brest", "SB29"],
    "Stade Rennais FC 1901":       ["Stade Rennais", "Rennais", "Rennes", "Stade Rennes", "SRFC"],
    "Stade de Reims":              ["Stade de Reims", "Reims"],
    "Toulouse FC":                 ["Toulouse FC", "Toulouse", "TFC"],
    "AS Saint-Étienne":            ["AS Saint-Étienne", "AS Saint-Etienne", "Saint-Étienne",
                                    "Saint-Etienne", "ASSE"],
    "Montpellier HSC":             ["Montpellier HSC", "Montpellier", "MHSC"],

    # ── Champions League / Europa League clubs ───────────────────────────────
    "AC Sparta Praha":             ["AC Sparta Praha", "Sparta Praha", "Sparta Prague", "Sparta"],
    "AFC Ajax":                    ["AFC Ajax", "Ajax"],
    "BSC Young Boys":              ["BSC Young Boys", "Young Boys", "YB"],
    "Beşiktaş JK":                 ["Beşiktaş JK", "Beşiktaş", "Besiktas"],
    "Celtic FC":                   ["Celtic FC", "Celtic", "Glasgow Celtic"],
    "Club Brugge KV":              ["Club Brugge KV", "Club Brugge", "Brugge"],
    "FC København":                ["FC København", "FC Kobenhavn", "Copenhagen", "FC Copenhagen"],
    "FC Porto":                    ["FC Porto", "Porto"],
    "FC Red Bull Salzburg":        ["FC Red Bull Salzburg", "Red Bull Salzburg", "RB Salzburg", "Salzburg"],
    "FCSB":                        ["FCSB", "Steaua Bucharest", "Steaua"],
    "FK Bodø/Glimt":               ["FK Bodø/Glimt", "FK Bodo/Glimt", "Bodø/Glimt",
                                    "Bodo/Glimt", "Bodo Glimt"],
    "FK Kairat":                   ["FK Kairat", "Kairat", "Kairat Almaty"],
    "Fenerbahçe SK":               ["Fenerbahçe SK", "Fenerbahçe", "Fenerbahce"],
    "Ferencváros TC":              ["Ferencváros TC", "Ferencváros", "Ferencvaros", "Fradi"],
    "Feyenoord":                   ["Feyenoord"],
    "GNK Dinamo Zagreb":           ["GNK Dinamo Zagreb", "Dinamo Zagreb"],
    "Galatasaray SK":              ["Galatasaray SK", "Galatasaray"],
    "Malmö FF":                    ["Malmö FF", "Malmo FF", "Malmö", "Malmo"],
    "PAE Olympiakos SFP":          ["PAE Olympiakos SFP", "Olympiakos", "Olympiacos"],
    "PSV":                         ["PSV", "PSV Eindhoven"],
    "Paphos FC":                   ["Paphos FC", "Pafos FC", "Pafos"],
    "Qarabağ Ağdam FK":            ["Qarabağ Ağdam FK", "Qarabağ FK", "Qarabağ", "Qarabag"],
    "Rangers FC":                  ["Rangers FC", "Rangers", "Glasgow Rangers"],
    "Royale Union Saint-Gilloise": ["Royale Union Saint-Gilloise", "Union Saint-Gilloise",
                                    "Union SG", "Union St. Gilloise", "Union Saint Gilloise"],
    "SC Braga":                    ["SC Braga", "Sporting de Braga", "Braga"],
    "SK Slavia Praha":             ["SK Slavia Praha", "Slavia Praha", "Slavia Prague"],
    "SK Sturm Graz":               ["SK Sturm Graz", "Sturm Graz", "Sturm"],
    "Shakhtar Donetsk":            ["Shakhtar Donetsk", "Shakhtar"],
    "Sport Lisboa e Benfica":      ["Sport Lisboa e Benfica", "SL Benfica", "Benfica"],
    "Sporting Clube de Portugal":  ["Sporting Clube de Portugal", "Sporting CP", "Sporting"],

    # ── LaLiga additional ────────────────────────────────────────────────────
    "CD Leganés":                  ["CD Leganés", "CD Leganes", "Leganés", "Leganes"],
    "Real Valladolid CF":          ["Real Valladolid CF", "Real Valladolid", "Valladolid"],
    "UD Las Palmas":               ["UD Las Palmas", "Las Palmas"],

    # ── Euro Cup national teams ──────────────────────────────────────────────
    "Albania":                     ["Albania"],
    "Austria":                     ["Austria", "Österreich"],
    "Belgium":                     ["Belgium"],
    "Croatia":                     ["Croatia", "Hrvatska"],
    "Czechia":                     ["Czechia", "Czech Republic"],
    "Denmark":                     ["Denmark"],
    "England":                     ["England"],
    "France":                      ["France"],
    "Georgia":                     ["Georgia"],
    "Germany":                     ["Germany"],
    "Hungary":                     ["Hungary"],
    "Italy":                       ["Italy"],
    "Netherlands":                 ["Netherlands", "Holland"],
    "Poland":                      ["Poland"],
    "Portugal":                    ["Portugal"],
    "Romania":                     ["Romania"],
    "Scotland":                    ["Scotland"],
    "Serbia":                      ["Serbia"],
    "Slovakia":                    ["Slovakia"],
    "Slovenia":                    ["Slovenia"],
    "Spain":                       ["Spain"],
    "Switzerland":                 ["Switzerland"],
    "Türkiye":                     ["Turkey", "Türkiye"],
    "Turkey":                      ["Turkey", "Türkiye"],   # FD returns English form; covers both title spellings
    "Ukraine":                     ["Ukraine"],

    # ── World Cup national teams (additional) ────────────────────────────────
    "Algeria":                     ["Algeria"],
    "Argentina":                   ["Argentina"],
    "Australia":                   ["Australia"],
    "Bosnia-Herzegovina":          ["Bosnia-Herzegovina", "Bosnia Herzegovina",
                                    "Bosnia & Herzegovina", "Bosnia"],
    "Brazil":                      ["Brazil"],
    "Canada":                      ["Canada"],
    "Cape Verde Islands":          ["Cape Verde Islands", "Cape Verde"],
    "Colombia":                    ["Colombia"],
    "Congo DR":                    ["Congo DR", "DR Congo"],
    "Curaçao":                     ["Curaçao", "Curacao"],
    "Ecuador":                     ["Ecuador"],
    "Egypt":                       ["Egypt"],
    "Ghana":                       ["Ghana"],
    "Haiti":                       ["Haiti"],
    "Iran":                        ["Iran"],
    "Iraq":                        ["Iraq"],
    "Ivory Coast":                 ["Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire"],
    "Japan":                       ["Japan"],
    "Jordan":                      ["Jordan"],
    "Mexico":                      ["Mexico", "México"],
    "Morocco":                     ["Morocco"],
    "New Zealand":                 ["New Zealand"],
    "Norway":                      ["Norway"],
    "Panama":                      ["Panama"],
    "Paraguay":                    ["Paraguay"],
    "Qatar":                       ["Qatar"],
    "Saudi Arabia":                ["Saudi Arabia"],
    "Senegal":                     ["Senegal"],
    "South Africa":                ["South Africa"],
    "South Korea":                 ["South Korea", "Korea Republic"],
    "Sweden":                      ["Sweden"],
    "Tunisia":                     ["Tunisia"],
    "United States":               ["United States", "USA"],
    "Uruguay":                     ["Uruguay"],
    "Uzbekistan":                  ["Uzbekistan"],

    # ── Copa America national teams (additional) ─────────────────────────────
    # Teams already present above (via Euro Cup / World Cup sections):
    #   Argentina, Brazil, Canada, Colombia, Ecuador, Mexico, Panama, Paraguay,
    #   United States, Uruguay.
    # The six below are Copa America-only participants not covered above.
    "Bolivia":                     ["Bolivia"],
    "Chile":                       ["Chile"],
    "Costa Rica":                  ["Costa Rica"],
    "Jamaica":                     ["Jamaica"],
    "Peru":                        ["Peru", "Perú"],
    "USA":                         ["United States", "USA"],   # API-Sports returns "USA"; covers both title forms
    "Venezuela":                   ["Venezuela"],
}


def team_tokens(team_name: str, short_name: str, tla: str = "") -> list[str]:
    """Return normalised candidate strings to search for in a YouTube title.

    Override path — if the team has an entry in TEAM_TITLE_ALIASES (keyed by the
    exact FD team.name), return those strings normalised.  They replace the
    auto-derived set entirely.

    Fallback path — call _auto_tokens() which derives candidates from the FD
    {name, shortName, tla} triplet plus progressively stripped variants.  This
    handles newly promoted/relegated teams with no explicit entry.

    Any one candidate appearing as a substring of the normalised title is a hit.
    """
    override = TEAM_TITLE_ALIASES.get(team_name)
    if override:
        return [_normalize(a) for a in override]
    return _auto_tokens(team_name, short_name, tla)


# Regex patterns used to auto-discover per-gameweek playlists from competition channels.
# {n} is replaced with the actual matchday number before compiling (case-insensitive).
# Patterns are tried in order; first match wins.
COMP_GW_PLAYLIST_PATTERNS: dict[str, list[str]] = {
    # Season-wide highlights playlists (no {n} — team-name matching within the playlist)
    "Premier League":   [r"\bclub\s+highlights\b"],
    # Per-GW playlists (use {n} substituted with matchday number)
    # LaLiga channel per-GW playlists: "RESÚMENES J38 | LALIGA EA SPORTS 2025/26"
    "LaLiga":           [r"\bj\s*{n}\b", r"\bjornada\s*{n}\b"],
    "Serie A":          [r"\bround\s*{n}\b"],
    "Bundesliga":       [r"\bmatchday\s*{n}\b"],
    "Ligue 1":          [r"\b{n}(?:ère|ème)\s+journ[eé]e\b"],
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
        + ["round-of-32", "round-of-16", "quarter-final", "semi-final", "third-place", "final"]
    ),
    "Copa America":     (
        [f"matchday-{i}" for i in range(1, 4)]
        + ["quarter-final", "semi-final", "third-place", "final"]
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
    "round-of-32":        "R32",
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
    "goals |",               # goals-compilation pipe label: "Real Madrid 2-0 Oviedo | GOALS | LaLiga"
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

    # ── Single-goal "golazo" clips ──────────────────────────────────────────────
    # "Golazo by [player]" / "Golazo de [player]" = one spectacular goal clip.
    # Note: "CBS Sports Golazo" is a channel name and does NOT follow this pattern.
    "golazo by ",            # English:  "Golazo by Noslin"
    "golazo de ",            # Spanish:  "Golazo de Kebbal"

    # ── Milestone / anniversary clips ───────────────────────────────────────────
    # "[N] goals for [player]" or "first goal for [club]" = career milestone, not a match recap.
    "100 goals for",         # English milestone e.g. "100 Goals for Bruno"
    "every angle",           # English: "Goals from EVERY Angle" = single-goal multi-cam replay
    "primer gol",            # Spanish: "Primer gol de Raúl García" = first goal milestone
    "primers gol",           # Catalan: same pattern
    "primo gol di",          # Italian: "Primo gol di Vardy" = first goal milestone
    "primeiro gol",          # Portuguese: same pattern

    # ── Club vlog / behind-the-scenes series ────────────────────────────────────
    "un dia de partit",      # Catalan: FC Barcelona's matchday vlog series ("A Match Day")
    "un día de partido",     # Spanish equivalent

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
    "ci sta mancando",       # post-match manager quote ("we're missing the goal") — interview snippet
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
    # "goals" intentionally excluded: covered by "gol" substring below, and
    # "goals |" is now in the blocklist to reject goals-compilation pipe labels.

    # ── French ──
    "résumé",                # summary/highlights
    "buts",                  # goals

    # ── Spanish / Catalan ──
    "resumen",               # summary/highlights
    "resum",                 # Catalan summary (RCD Espanyol: "⚽ RESUM J{N} | …")
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

# Per-competition allowlist extensions applied ONLY to official competition channel
# searches (tier 2).  These terms are competition-branded and too broad for club-channel
# tier 1 searches, where a non-highlight video could legitimately mention the competition
# name.  Entries here are sponsor-agnostic and survive rebranding between seasons.
COMP_CHANNEL_TITLE_TERMS: dict[str, list[str]] = {
    # "| HL | MD 4 – Bundesliga", "| Highlights | Matchday 21 – Bundesliga 2025/26",
    # and bare "| Matchday 19" titles that lack "Bundesliga" or "Highlights"
    "Bundesliga": ["bundesliga", "matchday"],
    # "| Week N - Ligue 1 McDonald's 25/26" — survives future sponsor changes
    "Ligue 1":    ["ligue 1"],
}


def is_highlight_title(
    title: str,
    extra_allowlist: "list[str] | tuple[str, ...]" = (),
    require_allowlist: bool = True,
) -> bool:
    """
    Return True only when the video title passes both filters:

    1. Blocklist (checked first — always wins): if any blocklist term is found
       in the raw lowercase title the video is rejected immediately, regardless
       of allowlist matches.
    2. Allowlist: at least one allowlist term must appear in the title **with
       hashtags stripped out**.  Stripping prevents hashtag-only passes such as
       ``#LaLigaHighlights`` matching the allowlist term ``"highlights"`` — a
       common pattern on social/Shorts clips that are not highlight packages.
       Skipped when ``require_allowlist=False`` (caller knows the playlist title
       already signals this is a highlights playlist).

    ``extra_allowlist`` is an optional per-source extension (e.g. competition-name
    terms for official competition channel searches) that is checked after the main
    allowlist without affecting other search tiers.

    Both checks are case-insensitive and cover 11 languages: English, Spanish,
    French, German, Italian, Portuguese, Arabic, Dutch, Turkish, Japanese, Korean.
    """
    lower = title.lower()

    # Step 1 — blocklist on the raw title (hashtags included so "#shorts" fires)
    for term in TITLE_BLOCKLIST:
        if term in lower:
            log.debug(f"Title blocked ({term!r}): {title!r}")
            return False

    if not require_allowlist:
        return True

    # Step 2 — allowlist on the hashtag-stripped title.
    # Removes every #word token so that "#LaLigaHighlights" does NOT satisfy
    # the "highlights" allowlist entry.  A genuine highlights title always has
    # the keyword in the non-hashtag body (e.g. "HIGHLIGHTS | LALIGA EA SPORTS").
    lower_no_tags = re.sub(r"#\S+", "", lower).strip()
    for term in (*TITLE_ALLOWLIST, *extra_allowlist):
        if term in lower_no_tags:
            return True

    log.debug(f"Title failed allowlist: {title!r}")
    return False


def is_laliga_highlight_title(title: str) -> bool:
    """
    Gate for videos discovered via the LaLiga competition channel (tier 2).

    Returns True when the title contains either:
    - 'highlights laliga'  — e.g. "HIGHLIGHTS LALIGA EA SPORTS"
    - 'resumen laliga'     — e.g. "TEAM A 2 - 1 TEAM B | RESUMEN LALIGA EA SPORTS"

    Both are the official LaLiga match-highlights title formats.  Sponsor suffixes
    (e.g. 'EA SPORTS') are ignored — the check is durable across season rebranding.

    Applied exclusively to tier-2 LaLiga results in resolve_videos_for_fixture() and
    in clean_highlights.py.  Team-channel (tier 1) and broadcaster-playlist (tier 4)
    LaLiga videos are never evaluated against this function.
    """
    normalised = " ".join(title.lower().split())
    return "highlights laliga" in normalised or "resumen laliga" in normalised


# ── Video quality helpers ─────────────────────────────────────────────────────


def _parse_iso8601_duration(duration: str) -> int:
    """Parse an ISO 8601 duration string (e.g. 'PT4M13S') to total seconds."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration or "")
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def fetch_video_details(
    video_ids: list[str],
    yt_key: str,
    quota: "QuotaTracker",
    cap: int,
) -> dict[str, dict]:
    """
    Fetch duration and thumbnail orientation for up to 50 video IDs in one API call.

    Returns {video_id: {"duration_seconds": int, "is_portrait": bool}}.
    Videos missing from the response are omitted — callers should treat absent
    entries as "unknown, do not filter".
    Costs 1 quota unit regardless of the number of IDs (up to the 50-ID batch limit).
    Raises QuotaCapReached on HTTP 403.
    """
    if not video_ids:
        return {}
    try:
        resp = requests.get(
            YT_VIDEOS,
            params={
                "part":   "contentDetails,snippet",
                "id":     ",".join(video_ids[:50]),
                "key":    yt_key,
                "fields": "items(id,contentDetails/duration,snippet/thumbnails)",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        log.warning(f"Network error fetching video details: {exc}")
        return {}

    if resp.status_code == 403:
        raise QuotaCapReached("YouTube 403 on videos.list — quota exhausted")
    if not resp.ok:
        log.warning(f"YouTube videos.list HTTP {resp.status_code} — skipping quality filter")
        return {}

    quota.increment(cap)

    out: dict[str, dict] = {}
    for item in resp.json().get("items", []):
        vid_id   = item.get("id", "")
        duration = _parse_iso8601_duration(
            item.get("contentDetails", {}).get("duration", "")
        )
        thumbs   = item.get("snippet", {}).get("thumbnails", {})
        portrait = False
        for size in ("maxres", "standard", "high", "medium", "default"):
            t = thumbs.get(size, {})
            w, h = t.get("width", 0), t.get("height", 0)
            if w > 0 and h > 0:
                portrait = h > w
                break
        if vid_id:
            out[vid_id] = {"duration_seconds": duration, "is_portrait": portrait}
    return out


def find_gameweek_playlist(
    channel_id: str,
    matchday: int | None,
    stage: str,
    comp_name: str,
    *,
    yt_key: str,
    quota: "QuotaTracker",
    cap: int,
    cache: dict,
) -> "tuple[str, str] | None":
    """
    Discover the per-gameweek playlist published by a competition's official channel.

    Calls ``playlists.list`` (1 unit) and matches playlist titles against
    ``COMP_GW_PLAYLIST_PATTERNS``.  Results (including None) are stored in
    ``cache`` so multiple fixtures sharing the same playlist avoid redundant
    API calls:

    - Per-GW patterns (containing ``{n}``): keyed by ``(channel_id, matchday)``
      so all fixtures in the same gameweek share one call.
    - Season-wide patterns (no ``{n}``, e.g. LaLiga Highlights, PL Club
      Highlights): keyed by ``(channel_id, None)`` so every fixture in the
      competition shares a single call — the playlist is the same regardless
      of matchday.

    Returns ``(playlist_id, playlist_title)`` on first title match, or None when:
      - ``matchday`` is None (knockout fixtures; matchday = leg number, not GW)
      - The competition has no defined patterns
      - The stage is a knockout stage for a STAGE_AWARE_COMP (to avoid
        conflating "leg 1" with "Matchday 1")
      - No playlist title matches the pattern
    """
    # For knockout-stage competitions, matchday is the leg number (1/2),
    # which would incorrectly match "Matchday 1" / "Matchday 2" playlists.
    if matchday is None:
        return None
    if comp_name in STAGE_AWARE_COMPS and stage not in ("LEAGUE_STAGE", "GROUP_STAGE"):
        return None
    if comp_name not in COMP_GW_PLAYLIST_PATTERNS:
        return None

    comp_patterns = COMP_GW_PLAYLIST_PATTERNS[comp_name]
    matchday_specific = any("{n}" in p for p in comp_patterns)
    cache_key = (channel_id, matchday if matchday_specific else None)
    if cache_key in cache:
        return cache[cache_key]

    patterns = [
        re.compile(p.replace("{n}", re.escape(str(matchday))), re.IGNORECASE)
        for p in comp_patterns
    ]

    # Channels may have >50 playlists (multiple seasons + bonus content), so
    # paginate until we find a match or exhaust the channel's playlist list.
    # Each page costs 1 quota unit; cap at 5 pages (250 playlists) to stay lean.
    _MAX_PAGES   = 5
    found        = None
    found_title  = ""
    page_token   = ""

    for _ in range(_MAX_PAGES):
        params: dict = {
            "part":       "snippet",
            "channelId":  channel_id,
            "maxResults": 50,
            "key":        yt_key,
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = requests.get(YT_PLAYLISTS, params=params, timeout=15)
        except requests.RequestException as exc:
            log.warning(f"Network error fetching playlists for channel {channel_id}: {exc}")
            break

        if resp.status_code == 403:
            raise QuotaCapReached("YouTube 403 on playlists.list — quota exhausted")
        if not resp.ok:
            log.warning(
                f"YouTube playlists.list HTTP {resp.status_code} for {channel_id} — skipping"
            )
            break

        quota.increment(cap)
        data = resp.json()

        for item in data.get("items", []):
            title = item.get("snippet", {}).get("title", "")
            pl_id = item.get("id", "")
            if pl_id and any(pat.search(title) for pat in patterns):
                found       = pl_id
                found_title = title
                log.info(
                    f"  Discovered GW playlist for {comp_name} MD{matchday}: "
                    f"{title!r} → {pl_id}"
                )
                break

        if found:
            break

        page_token = data.get("nextPageToken", "")
        if not page_token:
            break

    if not found:
        log.debug(
            f"No GW playlist found for {comp_name} MD{matchday} on channel {channel_id}"
        )

    cache[cache_key] = (found, found_title) if found else None
    return (found, found_title) if found else None


# ── Exception ─────────────────────────────────────────────────────────────────


class QuotaCapReached(Exception):
    """Daily YouTube unit cap hit — save state and exit cleanly (exit 0)."""


# ── Season / time helpers ─────────────────────────────────────────────────────

# Competitions that run in the calendar year (June-July) rather than the
# August-July domestic football season.  For these, the season year equals
# the calendar year of the tournament (e.g. WC 2026 → season=2026), even
# when current_season() would otherwise return year-1 (e.g. in May/June).
SUMMER_TOURNAMENT_COMPS: set[str] = {"World Cup", "Euro Cup", "Copa America"}

# Each summer tournament has a known anchor year and a recurrence period.
# season_for_competition uses these to find the most recent edition year that
# is <= today, so the app always shows the latest completed tournament while
# correctly stepping forward when a new edition year arrives.
#
# To register a new edition: update anchor_year to the next scheduled year
# once it is confirmed (the period handles all future editions automatically).
SUMMER_TOURNAMENT_CYCLE: dict[str, tuple[int, int]] = {
    "World Cup":    (2022, 4),   # 2022, 2026, 2030 …
    "Euro Cup":     (2024, 4),   # 2020, 2024, 2028 …
    "Copa America": (2024, 4),   # 2021, 2024, 2028 …
}


def season_for_competition(comp_name: str, now=None) -> int:
    """Return the football-data.org season year to query for a given competition.

    Domestic leagues and UCL/UEL delegate to current_season() from season_utils
    (the canonical August-threshold rule).  Summer tournaments use
    SUMMER_TOURNAMENT_CYCLE to return the most recent edition year <= today.

    now is injectable for unit tests; defaults to UTC today.
    """
    if comp_name not in SUMMER_TOURNAMENT_COMPS:
        return current_season(now)
    if now is None:
        now = datetime.now(timezone.utc)
    cycle = SUMMER_TOURNAMENT_CYCLE.get(comp_name)
    if cycle is None:
        return now.year
    anchor, period = cycle
    current_year = now.year
    completed = (current_year - anchor) // period
    return anchor + completed * period


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
    """Return a clean PL... playlist ID, or None if invalid.

    Accepts any PL-prefixed ID whose body consists solely of alphanumeric,
    underscore, or dash characters.  Length is intentionally unconstrained:
    YouTube serves real playlists at IDs shorter than the common 34-char form
    (e.g. PLXHZm5xDlEdQ at 13 chars).  Resolution + owner verification in
    verify_playlist_owners() is the authoritative validity check; character
    count is not.
    """
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    return v if re.fullmatch(r"PL[A-Za-z0-9_\-]+", v) else None


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
                clean = []
                for i in ids:
                    p = extract_playlist_id(i)
                    if p:
                        clean.append(p)
                    elif isinstance(i, str) and i.startswith("PL"):
                        log.warning(
                            "sources.json: playlist ID %r for %s/%s is invalid "
                            "(PL IDs must match PL[A-Za-z0-9_-]{20,}); skipping",
                            i, comp, bcast,
                        )
            elif isinstance(ids, str):
                p = extract_playlist_id(ids)
                if not p and isinstance(ids, str) and ids.startswith("PL"):
                    log.warning(
                        "sources.json: playlist ID %r for %s/%s is invalid; skipping",
                        ids, comp, bcast,
                    )
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
    elif stage == "LAST_32":
        return "round-of-32"
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


def gw_path(comp_name: str, stem: str, season: int) -> Path:
    return HIGHLIGHTS_DIR / COMPETITION_SLUG_MAP[comp_name] / str(season) / f"{stem}.json"


def is_same_tournament_edition(
    existing: dict | None,
    fixtures: list[dict],
    comp_name: str,
) -> bool:
    """
    Return True when the existing file belongs to the same tournament edition
    as the incoming fixtures.

    Only meaningful for non-annual summer tournaments (World Cup, Euro Cup).
    For these competitions the same file path (e.g. ``matchday-1.json``) is
    reused across editions, so we must detect when new-edition fixtures would
    otherwise be merged into old-edition data.

    Detection strategy: compare the calendar year of the first fixture date in
    the existing file against the calendar year of the first incoming fixture.
    A mismatch means a new tournament edition has started and the old file
    should be treated as empty (``merge_into_gw`` will overwrite it).

    For domestic leagues and UCL/UEL this always returns True (no-op).
    """
    if comp_name not in SUMMER_TOURNAMENT_COMPS:
        return True
    if existing is None or not fixtures:
        return True
    existing_matches = existing.get("matches", [])
    if not existing_matches:
        return True
    existing_year = existing_matches[0].get("date", "")[:4]
    new_year      = fixtures[0].get("date", "")[:4]
    if existing_year != new_year:
        log.info(
            f"{comp_name}: existing file is from {existing_year}, "
            f"incoming fixtures are from {new_year} — treating as new edition"
        )
        return False
    return True


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
    extra_allowlist: "list[str] | tuple[str, ...]" = (),
    debug_sink: "list | None" = None,
    date_window_days: int = VIDEO_WINDOW_DAYS,
    bypass_highlight_allowlist: bool = False,
) -> list[dict]:
    """
    Search a playlist for videos matching the given fixture.

    Acceptance criteria (ALL must be true):
      1. published_at is in [fixture_date, fixture_date + VIDEO_WINDOW_DAYS]
      2. If requires_competition_filter: title contains a competition keyword
      3. If requires_both_teams: title contains home_short AND away_short
         Otherwise: title contains home_short OR away_short
      4. is_highlight_title() passes (blocklist always; allowlist skipped when
         bypass_highlight_allowlist=True, i.e. the playlist title already signals
         this is a highlights playlist so the per-video title need not repeat it).

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
    window_end   = fixture_date + timedelta(days=date_window_days)
    home_tokens  = team_tokens(
        fixture.get("home_team", ""), fixture["home_short"], fixture.get("home_tla", "")
    )
    away_tokens  = team_tokens(
        fixture.get("away_team", ""), fixture["away_short"], fixture.get("away_tla", "")
    )
    keywords     = COMPETITION_KEYWORDS.get(comp_name, [])

    accepted:      list[dict] = []
    seen_ids:      set[str]   = set()
    page_token:    str        = ""
    pages_fetched: int        = 0

    # ── Debug helper: no-op when debug_sink is None ───────────────────────────
    # Caps collection at 30 records per search_playlist() call so UCL playlists
    # with hundreds of items don't bloat the sink.  The display cap in
    # _emit_debug_block() (fetch_highlights.py) is a separate, lower limit.
    _sink_start          = len(debug_sink) if debug_sink is not None else 0
    _MAX_DEBUG_RECS: int = 30

    def _rec(v_id: str, raw_title: str, norm: str, why: str) -> None:
        if debug_sink is not None and (len(debug_sink) - _sink_start) < _MAX_DEBUG_RECS:
            debug_sink.append({
                "video_id":    v_id,
                "title":       raw_title,
                "norm_title":  norm,
                "reason":      why,
                "playlist_id": playlist_id,
                "tier":        None,  # patched by _try() after the call returns
            })

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

            # Normalise up front so every debug record sees the same form
            # the matcher actually compares against.
            norm_title = _normalize(title)

            if pub_date < fixture_date or pub_date > window_end:
                _rec(video_id, title, norm_title, f"outside-date-window ({pub_str_date})")
                continue

            if requires_competition_filter and not any(
                kw in norm_title for kw in keywords
            ):
                _rec(video_id, title, norm_title, "no-comp-keyword")
                continue

            home_hit = any(tok in norm_title for tok in home_tokens)
            away_hit = any(tok in norm_title for tok in away_tokens)

            if requires_both_teams:
                # Report which team is missing to distinguish cross-match-guard
                # failures from genuine token mismatches (diacritics, aliases).
                if not home_hit:
                    _rec(video_id, title, norm_title,
                         f"cross-match-guard:home-missing "
                         f"(need one of {sorted(home_tokens)!r})")
                    continue
                if not away_hit:
                    _rec(video_id, title, norm_title,
                         f"cross-match-guard:away-missing "
                         f"(need one of {sorted(away_tokens)!r})")
                    continue
            else:
                if not home_hit and not away_hit:
                    _rec(video_id, title, norm_title, "no-token-overlap")
                    continue

            # Reject press conferences, interviews, previews, training clips, etc.
            # Blocklist always wins; allowlist is skipped when bypass_highlight_allowlist
            # is True (the playlist title already implies these are highlight videos).
            if not is_highlight_title(title, extra_allowlist, require_allowlist=not bypass_highlight_allowlist):
                lower = title.lower()
                blocked_by = next((t for t in TITLE_BLOCKLIST if t in lower), None)
                reason = (
                    f"title-filter:blocked:{blocked_by!r}"
                    if blocked_by
                    else "title-filter:no-allowlist-match"
                )
                _rec(video_id, title, norm_title, reason)
                continue

            seen_ids.add(video_id)
            _rec(video_id, title, norm_title, "passed-search-filter")
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
    gw_playlist_cache: dict | None = None,
    debug_sink: "list | None" = None,
) -> list[dict]:
    """
    Try each tier in priority order, stopping at the first that yields ≥1 video.
    Raises QuotaCapReached (propagated from search_playlist) if cap is hit.

    Tier order: 1c → 1d → 2 → 4 → 1a → 1b
    """
    if gw_playlist_cache is None:
        gw_playlist_cache = {}

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
        extra_allowlist: "list[str] | tuple[str, ...]" = (),
        date_window: int = VIDEO_WINDOW_DAYS,
        comp_exclusion_kws: "tuple[str, ...] | list[str]" = (),
        highlight_playlist: bool = False,
    ) -> list[dict] | None:
        if not playlist_id:
            return None
        # Track where this call starts in the shared sink so we can retroactively
        # tag the tier number on all records that search_playlist() appends.
        _sink_start = len(debug_sink) if debug_sink is not None else 0
        vids = search_playlist(
            playlist_id, yt_key, fixture, comp_name, quota, cap,
            requires_competition_filter=comp_filter,
            requires_both_teams=both_teams,
            extra_allowlist=extra_allowlist,
            debug_sink=debug_sink,
            date_window_days=date_window,
            bypass_highlight_allowlist=highlight_playlist,
        )
        if debug_sink is not None:
            for rec in debug_sink[_sink_start:]:
                rec["tier"] = tier
        if not vids:
            return None

        # Competition-exclusion filter: drop videos that belong to a different
        # competition (cups, knockout rounds) when an exclusion list is provided.
        if comp_exclusion_kws:
            kept = []
            for v in vids:
                nt = _normalize(v["title"])
                hit = next((kw for kw in comp_exclusion_kws if kw in nt), None)
                if hit:
                    if debug_sink is not None:
                        debug_sink.append({
                            "video_id":    v["video_id"],
                            "title":       v["title"],
                            "norm_title":  nt,
                            "reason":      f"comp-exclusion:{hit}",
                            "playlist_id": playlist_id,
                            "tier":        tier,
                        })
                    log.info(
                        f"  Exclusion: cross-competition keyword {hit!r} in title: "
                        f"{v['title']!r}"
                    )
                else:
                    kept.append(v)
            vids = kept
        if not vids:
            return None

        # Quality filter: reject clips that are too short or filmed in portrait mode.
        # Costs 1 quota unit per batch (up to 50 IDs); only called when candidates exist.
        details = fetch_video_details([v["video_id"] for v in vids], yt_key, quota, cap)
        filtered = []
        for v in vids:
            d = details.get(v["video_id"])
            if d is not None:
                if d["duration_seconds"] < MIN_VIDEO_DURATION_SECONDS:
                    if debug_sink is not None:
                        debug_sink.append({
                            "video_id":    v["video_id"],
                            "title":       v["title"],
                            "norm_title":  _normalize(v["title"]),
                            "reason":      f"too-short:{d['duration_seconds']}s",
                            "playlist_id": playlist_id,
                            "tier":        tier,
                        })
                    log.info(
                        f"  Quality: skipping short clip "
                        f"({d['duration_seconds']}s < {MIN_VIDEO_DURATION_SECONDS}s): "
                        f"{v['title']!r}"
                    )
                    continue
                if d["is_portrait"]:
                    if debug_sink is not None:
                        debug_sink.append({
                            "video_id":    v["video_id"],
                            "title":       v["title"],
                            "norm_title":  _normalize(v["title"]),
                            "reason":      "portrait-video",
                            "playlist_id": playlist_id,
                            "tier":        tier,
                        })
                    log.info(
                        f"  Quality: skipping portrait/vertical video: {v['title']!r}"
                    )
                    continue
            filtered.append(v)

        return [{**v, "tier_used": tier} for v in filtered] if filtered else None

    # publishedAt in playlistItems.list = date item was added to the playlist, not
    # video publication date.  Applies to all curated playlists: tier 1c/1d team
    # playlists, tier 2a GW playlists, and tier 4 broadcaster playlists.  Clubs and
    # broadcasters routinely add videos several days after upload, so a 3-day window
    # misses them permanently on subsequent recheck runs.
    _CURATED_PLAYLIST_WINDOW = 7

    # Tier 1c — home team competition-scoped playlist
    # requires_both_teams=True (no comp_filter): the playlist is already scoped to
    # this competition by its entry in teamPlaylists[comp_name], so requiring a
    # competition keyword in the title is redundant and blocks clubs (e.g. Paris FC)
    # whose titles use "résumé + opponent" format without naming the competition.
    # Requiring BOTH team names is at least as strict a cross-fixture guard, and
    # also protects against cross-competition false positives: a Europa League clip
    # that slipped into a LaLiga playlist would still need the *LaLiga* opponent in
    # its title to pass — which it won't have.
    # highlight_playlist=True: these playlists are curated by clubs specifically for
    # competition highlights, so the per-video title need not also say "highlights".
    result = _try(team_pl.get(comp_name, {}).get(home, ""), tier=1, both_teams=True, date_window=_CURATED_PLAYLIST_WINDOW, highlight_playlist=True)
    if result:
        return result

    # Tier 1d — away team competition-scoped playlist (same reasoning as 1c)
    result = _try(team_pl.get(comp_name, {}).get(away, ""), tier=1, both_teams=True, date_window=_CURATED_PLAYLIST_WINDOW, highlight_playlist=True)
    if result:
        return result

    # Tier 2 — official competition channel
    # 2a: try the per-gameweek playlist first (curated; far less likely to contain
    #     vertical social clips or wrong-fixture videos than the broad uploads feed).
    # 2b: fall back to the channel uploads playlist if no GW playlist is found.
    # comp_extra extends the title allowlist with competition-branded terms (e.g.
    # "bundesliga", "ligue 1") that are too broad for club-channel tier 1 searches
    # but safe here because tier 2 always uses requires_both_teams=True.
    ch = comp_ch.get(comp_name, "")
    if ch:
        comp_extra = COMP_CHANNEL_TITLE_TERMS.get(comp_name, ())
        gw_pl_result = find_gameweek_playlist(
            ch,
            fixture.get("matchday"),
            fixture.get("stage", ""),
            comp_name,
            yt_key=yt_key,
            quota=quota,
            cap=cap,
            cache=gw_playlist_cache,
        )
        if gw_pl_result:
            gw_pl, gw_pl_title = gw_pl_result
            result = _try(gw_pl, tier=2, both_teams=True, extra_allowlist=comp_extra, date_window=_CURATED_PLAYLIST_WINDOW, highlight_playlist="highlight" in gw_pl_title.lower())
            # LaLiga channel strict gate: only 'HIGHLIGHTS LALIGA' titles accepted from
            # this source.  'RESUMEN LALIGA EA SPORTS' passes is_highlight_title() via
            # the Spanish 'resumen' allowlist term but is rejected here because it is
            # the competition channel's own social/summary reel, not the broadcast cut.
            if result and comp_name == "LaLiga":
                if debug_sink is not None:
                    for v in result:
                        if not is_laliga_highlight_title(v["title"]):
                            debug_sink.append({
                                "video_id":    v["video_id"],
                                "title":       v["title"],
                                "norm_title":  _normalize(v["title"]),
                                "reason":      "laliga-gate:no-highlights-laliga",
                                "playlist_id": gw_pl,
                                "tier":        2,
                            })
                result = [v for v in result if is_laliga_highlight_title(v["title"])] or None
            if result:
                return result
        # Tier 2b: broad channel uploads (original fallback)
        uploads_pl = channel_to_uploads(ch)
        result = _try(uploads_pl, tier=2, both_teams=True, extra_allowlist=comp_extra)
        if result and comp_name == "LaLiga":
            if debug_sink is not None:
                for v in result:
                    if not is_laliga_highlight_title(v["title"]):
                        debug_sink.append({
                            "video_id":    v["video_id"],
                            "title":       v["title"],
                            "norm_title":  _normalize(v["title"]),
                            "reason":      "laliga-gate:no-highlights-laliga",
                            "playlist_id": uploads_pl,
                            "tier":        2,
                        })
            result = [v for v in result if is_laliga_highlight_title(v["title"])] or None
        if result:
            return result

    # Tier 4 — broadcaster playlists (same rationale as Tier 2a: curated PL…
    # playlists, requires_both_teams=True guards cross-fixture false positives).
    # highlight_playlist=True: these are explicitly configured broadcaster highlights
    # playlists, so the per-video title need not also contain "highlights".
    for _broadcaster, pl_ids in comp_pl.get(comp_name, {}).items():
        for pl_id in pl_ids:
            result = _try(pl_id, tier=4, both_teams=True, date_window=_CURATED_PLAYLIST_WINDOW, highlight_playlist=True)
            if result:
                return result

    # Tier 1a — home team club channel uploads.
    # Channel uploads contain ALL competitions, so we require both team names
    # (catches creative/viral titles without competition names) and exclude
    # known cup / knockout terms for this competition.
    _excl = COMP_EXCLUSION_KEYWORDS.get(comp_name, ())
    ch = team_ch.get(home, "")
    if ch:
        result = _try(channel_to_uploads(ch), tier=1, both_teams=True,
                      comp_exclusion_kws=_excl)
        if result:
            return result

    # Tier 1b — away team club channel uploads (same reasoning as 1a)
    ch = team_ch.get(away, "")
    if ch:
        result = _try(channel_to_uploads(ch), tier=1, both_teams=True,
                      comp_exclusion_kws=_excl)
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
            entry = {
                "match_id":  mid,
                "home_team": fix["home_team"],
                "away_team": fix["away_team"],
                "date":      fix["date"],
                "videos":    new_videos[:],
            }
            # Include crest URLs when provided (API-Sports competitions).
            if fix.get("home_crest"):
                entry["home_crest"] = fix["home_crest"]
            if fix.get("away_crest"):
                entry["away_crest"] = fix["away_crest"]
            by_id[mid] = entry
            changed = True
        else:
            existing_match = by_id[mid]
            # Back-fill crest URLs when newly available (e.g. after pipeline update).
            for crest_field in ("home_crest", "away_crest"):
                new_val = fix.get(crest_field, "")
                if new_val and not existing_match.get(crest_field):
                    existing_match[crest_field] = new_val
                    changed = True
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
        season   = season_for_competition(comp_name)
        comp_dir = HIGHLIGHTS_DIR / slug / str(season)
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


# ── Playlist owner verification ───────────────────────────────────────────────

# Words stripped when normalising broadcaster/channel names for fuzzy matching.
_OWNER_STOPWORDS: frozenset[str] = frozenset({
    "sport", "sports", "tv", "us", "usa", "the", "en", "vivo",
    "deportes", "fc", "sc",
})


def _owner_tokens(name: str) -> frozenset[str]:
    """Return lowercase content words from a broadcaster or channel name."""
    return frozenset(
        w for w in re.split(r"[\W_]+", name.lower())
        if w and w not in _OWNER_STOPWORDS
    )


def labels_match(label: str, channel_title: str) -> bool:
    """
    Return True if the sources.json broadcaster label and the YouTube
    channel_title refer to the same entity.

    Comparison is tolerant of qualifier words ('Sport', 'TV', 'US') and
    capitalisation.  Catches genuine wrong-owner cases (e.g. 'FIFA' vs
    'SBS Sport') while accepting 'CBS Sport Golazo' vs 'CBS Sports'.

    Strategy: strip common qualifier words, lowercase, split into tokens;
    match if the smaller token-set is wholly contained in the larger.
    """
    lt = _owner_tokens(label)
    ct = _owner_tokens(channel_title)
    if not lt or not ct:
        return False
    smaller, larger = (lt, ct) if len(lt) <= len(ct) else (ct, lt)
    return smaller.issubset(larger)


def fetch_playlist_owner(
    playlist_id: str,
    api_key: str,
    *,
    session: "requests.Session | None" = None,
    quota: "QuotaTracker | None" = None,
    quota_cap: int = BACKFILL_CAP,
) -> "dict | None":
    """
    Fetch playlist ownership metadata via playlists.list?part=snippet.

    Returns {"channel_id": str, "channel_title": str, "playlist_title": str}
    or None if the playlist is private, deleted, or otherwise unresolvable.

    Quota cost: 1 unit per call — identical to playlistItems.list.  playlistItems.list
    cannot return the PLAYLIST owner (only the item uploader); playlists.list is
    therefore the correct and most efficient endpoint for owner verification.
    Cost is counted against *quota* the same way all other YouTube calls are.

    Increment fires after a 200 OK (raise_for_status passes).  HTTP 4xx/5xx
    throws before the increment, consistent with the rest of the codebase.
    """
    http: requests.Session = session if session is not None else requests.Session()
    resp = http.get(
        YT_PLAYLISTS,
        params={"part": "snippet", "id": playlist_id, "key": api_key, "maxResults": 1},
        timeout=30,
    )
    resp.raise_for_status()
    # Count 1 unit against the shared daily budget (same cost as playlistItems.list).
    if quota is not None:
        quota.increment(quota_cap)
    items = resp.json().get("items", [])
    if not items:
        return None  # private, deleted, or bad ID
    sn = items[0]["snippet"]
    # channel_title is the owning channel's name — NOT the playlist title (sn["title"]).
    # The two differ: e.g. playlist title "FIFA World Cup 2026™ Match Highlights" vs
    # channel_title "SBS Sport".  Owner verification must key on channel_title only.
    return {
        "channel_id":     sn["channelId"],
        "channel_title":  sn["channelTitle"],   # the OWNER — used for label comparison
        "playlist_title": sn["title"],           # for audit; never used in label comparison
    }


def verify_playlist_owners(
    api_key: str,
    *,
    quota: "QuotaTracker | None" = None,
    quota_cap: int = BACKFILL_CAP,
    session: "requests.Session | None" = None,
    sources_path: "Path | None" = None,
    owners_path: "Path | None" = None,
) -> list[str]:
    """
    For every PL-prefixed playlist ID in sources.json, verify that the
    YouTube channel owning it matches the broadcaster label it is filed under.

    **On-change gating**: IDs that already have a resolved channel_id in
    playlist-owners.json are not re-fetched (quota saved); the label-vs-owner
    check re-runs on every call so a re-label in sources.json is caught
    without a re-fetch.

    **Seeded entries**: entries with channel_id = null (manually asserted,
    not API-verified) are treated the same as new IDs — they are fetched and
    the real channel_id is recorded.  Owner matching keys on the fetched
    channel_title (the channel's own name), never on the playlist title.

    Quota: each new-ID or seeded-entry fetch costs 1 unit (playlists.list
    ?part=snippet), counted against *quota* the same as playlistItems.list.

    Returns a (possibly empty) list of human-readable error strings for:
    - owner mismatches (right ID / wrong broadcaster label)
    - unresolvable IDs (private, deleted, network error)
    - uncached/seeded IDs when *api_key* is empty

    An empty return list means all IDs passed.
    """
    sp: Path = sources_path or SOURCES_JSON
    op: Path = owners_path  or PLAYLIST_OWNERS_PATH

    with open(sp, encoding="utf-8") as f:
        raw = json.load(f)

    cache: dict = load_json_file(op) or {}
    errors: list[str] = []
    new_entries: dict = {}

    for comp, broadcasters in raw.get("playlists", {}).items():
        if not isinstance(broadcasters, dict):
            continue
        for label, ids in broadcasters.items():
            items_list = ids if isinstance(ids, list) else ([ids] if isinstance(ids, str) else [])
            for pid in items_list:
                p = extract_playlist_id(pid) if isinstance(pid, str) else None
                if not p:
                    continue  # already caught by format guard

                if p in cache:
                    entry = cache[p]
                    cid = entry.get("channel_id")
                    ct  = entry.get("channel_title")

                    if entry.get("error"):
                        # Previously recorded as unresolvable — keep flagging.
                        errors.append(
                            f"{comp}/{label}: {p!r} previously failed to resolve — "
                            "update playlist-owners.json or replace the ID"
                        )
                        continue

                    if cid is not None:
                        # API-verified entry: skip re-fetch, re-check label only.
                        # Comparison is against channel_title (the owner's channel
                        # name), never against playlist_title.
                        if not labels_match(label, ct or ""):
                            errors.append(
                                f"{comp}/{label}: {p!r} is owned by {ct!r} — "
                                "label mismatch (re-file under the correct broadcaster)"
                            )
                        continue

                    # channel_id is None: manually seeded without API verification.
                    # Do not trust the asserted owner — fall through to fetch.
                    log.info(
                        "verify_playlist_owners: %s has no channel_id "
                        "(manually seeded) — fetching to verify",
                        p,
                    )

                # New ID or seeded-but-unverified — must fetch.
                if not api_key:
                    errors.append(
                        f"{comp}/{label}: {p!r} is not yet verified and "
                        "YOUTUBE_API_KEY is not set — run verify-playlist-owners "
                        "workflow after adding a new ID"
                    )
                    continue

                log.info(
                    "verify_playlist_owners: fetching owner for %s (%s / %s)",
                    p, comp, label,
                )
                try:
                    info = fetch_playlist_owner(
                        p, api_key, session=session, quota=quota, quota_cap=quota_cap,
                    )
                except Exception as exc:
                    errors.append(f"{comp}/{label}: {p!r} fetch failed — {exc}")
                    new_entries[p] = {
                        "competition": comp, "label": label,
                        "channel_id": None, "channel_title": None,
                        "playlist_title": None,
                        "verified_at": utc_now_iso(), "error": str(exc),
                    }
                    continue

                if info is None:
                    errors.append(
                        f"{comp}/{label}: {p!r} did not resolve — "
                        "playlist may be private or deleted"
                    )
                    new_entries[p] = {
                        "competition": comp, "label": label,
                        "channel_id": None, "channel_title": None,
                        "playlist_title": None,
                        "verified_at": utc_now_iso(), "error": "unresolvable",
                    }
                    continue

                new_entries[p] = {
                    "competition":    comp,
                    "label":          label,
                    "channel_id":     info["channel_id"],
                    "channel_title":  info["channel_title"],
                    "playlist_title": info["playlist_title"],
                    "verified_at":    utc_now_iso(),
                }
                if not labels_match(label, info["channel_title"]):
                    errors.append(
                        f"{comp}/{label}: {p!r} is owned by "
                        f"{info['channel_title']!r} (channel {info['channel_id']!r}) — "
                        "label mismatch (re-file under the correct broadcaster)"
                    )
                    log.warning(
                        "verify_playlist_owners: owner mismatch — %s filed under %r "
                        "but owned by %r",
                        p, label, info["channel_title"],
                    )

    if new_entries:
        cache.update(new_entries)
        write_json_atomic(op, cache)
        log.info(
            "verify_playlist_owners: wrote %d new entr%s to %s",
            len(new_entries),
            "y" if len(new_entries) == 1 else "ies",
            op.name,
        )

    return errors
    log.info(f"Written summary.json ({len(competitions)} competition(s))")
