#!/usr/bin/env python3
"""
Retroactive both-teams cleanup — removes videos that were stored for the wrong
fixture because only ONE team name appeared in the title.

Background
----------
The server-side `search_playlist()` originally accepted a video if the title
contained ``home_short OR away_short``.  For broad sources (tier 2 competition
channel, tier 4 broadcaster playlists) this let unrelated fixtures slip through.
E.g. a "Stade Rennais vs FC Nantes" recap was stored against the
"PSG vs FC Nantes" fixture because "Nantes" (away_short) appeared in both.

The live pipeline was fixed to require BOTH team names for tiers 2/4, but
existing JSON files still contain the wrongly-stored videos.  This script
re-evaluates each stored video against both team names and removes any video
whose title does not contain a keyword for BOTH the home team AND the away team.

How team keywords are derived
------------------------------
1.  Strip trailing year/number codes (e.g. "FC 1901" → "FC", "Brestois 29" → "Brestois")
2.  Strip common organisational suffixes (FC, AFC, SC, CF, AC, FK, SK, RC, AS,
    AJ, OGC, CD, UD, RB, SD, CA)
3.  Split on whitespace and hyphens; keep tokens of length ≥ 4
4.  Extend with any aliases from TEAM_ALIASES (handles abbreviations such as PSG)

A video is kept when at least ONE keyword from the home set AND at least ONE
keyword from the away set appear in the title (case-insensitive substring match).

Run via the GitHub Actions workflow or locally:
    python scripts/clean_wrong_fixture_videos.py
"""
import logging
import re
import subprocess
from pathlib import Path

from highlights_common import (
    HIGHLIGHTS_DIR,
    COMPETITION_FILE_STEMS,
    COMPETITION_SLUG_MAP,
    load_json_file,
    write_json_atomic,
    generate_summary,
    utc_now_iso,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ── Suffix / generic-word lists ───────────────────────────────────────────────

_ORG_SUFFIXES: set[str] = {
    "fc", "afc", "sc", "cf", "ac", "fk", "sk", "rc", "as", "aj",
    "ogc", "cd", "ud", "rb", "sd", "ca", "bk", "if",
}

_GENERIC_PREFIXES: set[str] = {
    "stade", "olympique", "sporting", "athletic", "club", "real",
    "inter", "association", "racing", "paris",  # "paris" alone is too generic for PSG
}

# ── Known abbreviations not derivable from the full team name ─────────────────
#
# Keys are the football-data.org full team name (home_team / away_team field).
# Values are additional lowercase tokens to add to the keyword set.
#
# "Paris Saint-Germain FC" → titles often use "psg" not "paris"
# "Internazionale" → titles often use "inter" (already extractable) or "nerazzurri"
TEAM_ALIASES: dict[str, list[str]] = {
    # ── French Ligue 1 ──
    # Club names often appear as stem forms in titles (e.g. "Brest" not "Brestois",
    # "Rennes" not "Rennais", "Nantes" already short enough).
    "Paris Saint-Germain FC":     ["psg", "paris sg", "paris saint-germain"],
    "Stade Brestois 29":          ["brest"],          # titles: "après Brest" not "Brestois"
    "Stade Rennais FC 1901":      ["rennes", "rennais"],
    "Olympique de Marseille":     ["marseille", "om"],
    "Olympique Lyonnais":         ["lyon", "ol"],
    "RC Lens":                    ["lens"],
    "RC Strasbourg Alsace":       ["strasbourg"],
    "Angers SCO":                 ["angers"],

    # ── Spanish LaLiga ──
    "Atlético de Madrid":         ["atletico", "atlético", "atleti"],
    "Real Betis Balompié":        ["betis"],
    "Athletic Club":              ["athletic", "bilbao"],
    "RCD Espanyol de Barcelona":  ["espanyol"],
    "Deportivo Alavés":           ["alaves", "alavés"],

    # ── German Bundesliga ──
    "Borussia Dortmund":          ["bvb"],
    "Bayern München":             ["bayern", "fcb"],
    "Borussia Mönchengladbach":   ["gladbach", "monchengladbach"],
    "TSG 1899 Hoffenheim":        ["hoffenheim"],
    "Bayer 04 Leverkusen":        ["leverkusen"],

    # ── Italian Serie A ──
    "FC Internazionale Milano":   ["inter", "internazionale"],
    "SS Lazio":                   ["lazio"],
    "AS Roma":                    ["roma"],

    # ── English Premier League ──
    "Manchester United FC":       ["man utd", "man united"],
    "Manchester City FC":         ["man city"],
    "Tottenham Hotspur FC":       ["spurs", "tottenham"],
    "Newcastle United FC":        ["newcastle"],
    "Leicester City FC":          ["leicester"],
    "West Ham United FC":         ["west ham"],
    "Wolverhampton Wanderers FC": ["wolves", "wolverhampton"],
    "Nottingham Forest FC":       ["forest", "nottingham"],
    "Brighton & Hove Albion FC":  ["brighton"],
    "Crystal Palace FC":          ["palace", "crystal palace"],
    "Aston Villa FC":             ["villa", "aston villa"],
    "AFC Bournemouth":            ["bournemouth"],
    "Ipswich Town FC":            ["ipswich"],
}


def _keywords_for_team(full_name: str) -> set[str]:
    """
    Derive the set of lowercase keyword strings to look for in a video title.

    Strategy:
      1. Strip trailing year/number codes
      2. Strip org suffixes
      3. Tokenise on whitespace and hyphens; keep tokens ≥ 4 chars that are not
         purely generic (org suffixes stripped in step 2 won't appear again, but
         "stade", "olympique", etc. ARE meaningful — we keep them)
      4. Add known aliases from TEAM_ALIASES
    """
    # Step 1 — strip trailing number codes ("FC 1901" → "FC", "Brestois 29" → "Brestois")
    cleaned = re.sub(r"\s+\d{2,4}$", "", full_name.strip()).strip()

    # Step 2 — strip a single trailing org suffix if present
    lower = cleaned.lower()
    for suf in _ORG_SUFFIXES:
        if lower.endswith(" " + suf):
            cleaned = cleaned[: -(len(suf) + 1)].strip()
            break

    # Step 3 — tokenise on whitespace and hyphens; keep tokens ≥ 4 chars
    tokens = re.split(r"[\s\-]+", cleaned)
    keywords: set[str] = {t.lower() for t in tokens if len(t) >= 4}

    # Step 4 — add known aliases
    for alias in TEAM_ALIASES.get(full_name, []):
        keywords.add(alias.lower())

    return keywords


def _video_matches_fixture(title: str, home_kws: set[str], away_kws: set[str]) -> bool:
    """Return True when the title contains ≥1 home keyword AND ≥1 away keyword."""
    lower = title.lower()
    has_home = any(kw in lower for kw in home_kws)
    has_away = any(kw in lower for kw in away_kws)
    return has_home and has_away


def clean_gameweek_file(path: Path) -> tuple[int, int]:
    """
    Remove cross-fixture videos from a single gameweek JSON file.

    For every match, any video whose title does not contain BOTH a home-team
    keyword AND an away-team keyword is removed.

    Returns (videos_removed, matches_affected).
    The file is rewritten atomically only when at least one video is removed.
    """
    data = load_json_file(path)
    if not data:
        return 0, 0

    videos_removed   = 0
    matches_affected = 0

    for match in data.get("matches", []):
        home_team = match.get("home_team", "")
        away_team = match.get("away_team", "")
        home_kws  = _keywords_for_team(home_team)
        away_kws  = _keywords_for_team(away_team)

        original = match.get("videos", [])
        kept     = []
        for v in original:
            title = v.get("title", "")
            if _video_matches_fixture(title, home_kws, away_kws):
                kept.append(v)
            else:
                log.info(
                    f"  Removing cross-fixture video from "
                    f"{home_team} vs {away_team}: {title!r}"
                )

        removed = len(original) - len(kept)
        if removed > 0:
            match["videos"]   = kept
            videos_removed   += removed
            matches_affected += 1

    if videos_removed > 0:
        data["generated_at"] = utc_now_iso()
        write_json_atomic(path, data)

    return videos_removed, matches_affected


def main() -> None:
    total_videos_removed   = 0
    total_matches_affected = 0
    changed_files: list[Path] = []

    for comp_name, slug in COMPETITION_SLUG_MAP.items():
        comp_dir = HIGHLIGHTS_DIR / slug
        if not comp_dir.exists():
            continue

        stems = COMPETITION_FILE_STEMS.get(comp_name, [])
        files = [comp_dir / f"{stem}.json" for stem in stems
                 if (comp_dir / f"{stem}.json").exists()]
        if not files:
            continue

        log.info(f"── {comp_name} ({len(files)} file(s)) ──")
        for f in files:
            removed, affected = clean_gameweek_file(f)
            total_videos_removed   += removed
            total_matches_affected += affected
            if removed > 0:
                changed_files.append(f)

    log.info(
        f"\nCleanup complete: {total_videos_removed} video(s) removed "
        f"from {total_matches_affected} match(es) across "
        f"{len(changed_files)} file(s)."
    )

    generate_summary()

    if not changed_files:
        log.info("No files changed — nothing to commit.")
        return

    changed_files.append(HIGHLIGHTS_DIR / "summary.json")

    try:
        subprocess.run(
            ["git", "config", "user.email",
             "github-actions[bot]@users.noreply.github.com"],
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "github-actions[bot]"],
            check=True,
        )
        subprocess.run(
            ["git", "add"] + [str(f) for f in changed_files],
            check=True,
        )
        result = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if result.returncode == 0:
            log.info("No staged changes — skipping commit.")
        else:
            subprocess.run(
                ["git", "commit", "-m",
                 "chore: remove cross-fixture videos (retroactive both-teams cleanup) [skip ci]"],
                check=True,
            )
            subprocess.run(["git", "pull", "--rebase"], check=True)
            subprocess.run(["git", "push"], check=True)
            log.info("Committed and pushed.")
    except subprocess.CalledProcessError as e:
        log.error(f"Git operation failed: {e} — data written locally")


if __name__ == "__main__":
    main()
