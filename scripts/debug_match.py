#!/usr/bin/env python3
"""
debug_match.py — Dry-run fixture↔title matcher diagnostic.

Tests the OLD (plain-shortName) and NEW (alias-aware) matching logic against:
  1. Already-cached video metadata from ligue-1/*.json
  2. Hypothetical Ligue 1 official-channel titles for known-failing fixtures

Zero YouTube API calls, zero quota consumed.

Usage:
    python scripts/debug_match.py [--fixtures-only]

    --fixtures-only   skip regression check; only show failing fixture analysis
"""

import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

# ── Bootstrap: make highlights_common importable without installing ───────────
sys.path.insert(0, os.path.dirname(__file__))
from highlights_common import (
    HIGHLIGHTS_DIR,
    TEAM_TITLE_ALIASES,
    _normalize,
    team_tokens,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

GW_DIR = HIGHLIGHTS_DIR / "ligue-1"


def old_match(home_short: str, away_short: str, title: str, both_teams: bool) -> bool:
    """Current matcher: plain FD shortName substring check."""
    t = title.lower()
    h = home_short.lower()
    a = away_short.lower()
    if both_teams:
        return h in t and a in t
    return h in t or a in t


def new_match(
    home_team: str, home_short: str,
    away_team: str, away_short: str,
    title: str,
    both_teams: bool,
) -> bool:
    """New matcher: alias-aware, diacritic-normalised token check."""
    t = _normalize(title)
    ht = team_tokens(home_team, home_short)
    at = team_tokens(away_team, away_short)
    home_hit = any(tok in t for tok in ht)
    away_hit = any(tok in t for tok in at)
    if both_teams:
        return home_hit and away_hit
    return home_hit or away_hit


def infer_short(team_name: str) -> str:
    """
    Infer FD shortName from the canonical team name.

    Based on observed beIN SPORTS tier-4 matches where both teams' names must
    appear in the title.  For unconfigured teams, falls back to the full name.
    Explicit FD shortName values confirmed from the existing matched data.
    """
    INFERRED: dict[str, str] = {
        "Stade Rennais FC 1901":  "Rennes",
        "Lille OSC":              "Lille",
        "Racing Club de Lens":    "Lens",
        "Le Havre AC":            "Le Havre",
        "Olympique Lyonnais":     "Lyon",
        "Olympique de Marseille": "Marseille",
        "Stade Brestois 29":      "Brest",
        "Paris FC":               "Paris FC",
        "Paris Saint-Germain FC": "PSG",
        "AS Monaco FC":           "Monaco",
        "RC Strasbourg Alsace":   "Strasbourg",
        "AJ Auxerre":             "Auxerre",
        "FC Nantes":              "Nantes",
        "FC Lorient":             "Lorient",
        "FC Metz":                "Metz",
        "Toulouse FC":            "Toulouse",
        "OGC Nice":               "Nice",
        "Angers SCO":             "Angers SCO",
    }
    return INFERRED.get(team_name, team_name)


# ── Step 2: dry-run over KNOWN-FAILING fixtures ───────────────────────────────

FAILING_FIXTURES = [
    # (gw, home_team, away_team) — per task brief
    (1,  "Angers SCO",             "Paris FC"),
    (3,  "Paris FC",               "FC Metz"),
    (3,  "Angers SCO",             "Stade Rennais FC 1901"),
    (9,  "Lille OSC",              "FC Metz"),
    (10, "Toulouse FC",            "Stade Rennais FC 1901"),
]

# Hypothetical Ligue 1 official-channel title variants for each fixture.
# Format observed: "{HOME} - {AWAY} ({score}) - Week {N} - Ligue 1 McDonald's 25/26"
# The official channel uses branded short names (LOSC, Stade Rennais, etc.),
# so we generate multiple candidate titles per fixture.
OFFICIAL_CHANNEL_FORMS: dict[str, list[str]] = {
    "Stade Rennais FC 1901": ["Stade Rennais", "Rennais", "Rennes"],
    "Lille OSC":              ["LOSC", "Lille"],
    "Racing Club de Lens":    ["RC Lens", "Lens"],
    "Le Havre AC":            ["Le Havre"],
    "Olympique Lyonnais":     ["Lyon", "OL"],
    "Olympique de Marseille": ["Marseille", "OM"],
    "Stade Brestois 29":      ["Brest", "Stade Brestois"],
    "Paris FC":               ["Paris FC", "Paris"],
    "Paris Saint-Germain FC": ["PSG", "Paris Saint-Germain"],
    "AS Monaco FC":           ["Monaco", "AS Monaco"],
    "RC Strasbourg Alsace":   ["Strasbourg", "RC Strasbourg"],
    "AJ Auxerre":             ["Auxerre"],
    "FC Nantes":              ["Nantes", "FC Nantes"],
    "FC Lorient":             ["Lorient"],
    "FC Metz":                ["Metz", "FC Metz"],
    "Toulouse FC":            ["Toulouse"],
    "OGC Nice":               ["Nice"],
    "Angers SCO":             ["Angers SCO", "Angers"],
}


def make_hypothetical_titles(gw: int, home: str, away: str) -> list[str]:
    """Generate hypothetical Ligue 1 official channel titles for this fixture."""
    titles = []
    for hf in OFFICIAL_CHANNEL_FORMS.get(home, [home]):
        for af in OFFICIAL_CHANNEL_FORMS.get(away, [away]):
            titles.append(
                f"{hf} - {af} (1-0) - Week {gw} - Ligue 1 McDonald's 25/26"
            )
    return titles


print("=" * 70)
print("STEP 2 — DRY-RUN OVER KNOWN-FAILING FIXTURES")
print("=" * 70)
print()

for gw, home, away in FAILING_FIXTURES:
    h_short = infer_short(home)
    a_short = infer_short(away)
    h_tok   = team_tokens(home, h_short)
    a_tok   = team_tokens(away, a_short)
    titles  = make_hypothetical_titles(gw, home, away)

    print(f"GW{gw}  {home} vs {away}")
    print(f"  FD shortName  home={h_short!r}  away={a_short!r}")
    print(f"  Alias tokens  home={h_tok}  away={a_tok}")
    print()

    for title in titles:
        old = old_match(h_short, a_short, title, both_teams=True)
        new = new_match(home, h_short, away, a_short, title, both_teams=True)
        flag = ""
        if old != new:
            flag = "  ← FIXED" if new else "  ← REGRESSION"
        elif not old and not new:
            flag = "  ✗ still fails"
        status = ("OLD=✓" if old else "OLD=✗") + "  " + ("NEW=✓" if new else "NEW=✗")
        print(f"  {status}{flag}")
        print(f"    {title!r}")
    print()

# ── Step 5: regression check over all already-covered Ligue 1 fixtures ───────

fixtures_only = "--fixtures-only" in sys.argv

print("=" * 70)
print("STEP 5 — REGRESSION CHECK: all covered Ligue 1 fixtures")
print("=" * 70)
print()

total = 0
covered = 0
old_pass = 0
new_pass = 0
regressions: list[tuple] = []
improvements: list[tuple] = []

gw_files = sorted(GW_DIR.glob("gameweek-*.json"))
for path in gw_files:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for m in data.get("matches", []):
        videos = m.get("videos", [])
        if not videos:
            total += 1
            continue
        total += 1
        covered += 1
        home = m["home_team"]
        away = m["away_team"]
        h_short = infer_short(home)
        a_short = infer_short(away)
        # Check whether the stored video title would still match
        # with both matchers (both_teams=True as the strictest check).
        # For tier 1 sources (both_teams=False), it's even easier — skip those.
        tier = videos[0].get("tier_used", 4)
        both = tier in (2, 4)  # tier 2/4 use both_teams=True
        for vid in videos:
            title = vid["title"]
            o = old_match(h_short, a_short, title, both_teams=both)
            n = new_match(home, h_short, away, a_short, title, both_teams=both)
            if o:
                old_pass += 1
            if n:
                new_pass += 1
            if o and not n:
                regressions.append((path.name, home, away, title, tier))
            elif not o and n:
                improvements.append((path.name, home, away, title, tier))

print(f"Fixtures total: {total}")
print(f"Currently covered (≥1 video): {covered}")
print(f"OLD matcher passes: {old_pass}")
print(f"NEW matcher passes: {new_pass}")
print()

if regressions:
    print(f"❌ REGRESSIONS ({len(regressions)}) — new matcher loses previously-matching videos:")
    for fn, h, a, title, tier in regressions:
        print(f"  {fn}  tier={tier}  [{h} vs {a}]")
        print(f"    {title!r}")
    print()
else:
    print("✓ Zero regressions — every previously-matched video still matches.")

if improvements:
    print(f"✓ IMPROVEMENTS ({len(improvements)}) — new matcher now accepts:")
    for fn, h, a, title, tier in improvements[:20]:
        print(f"  {fn}  tier={tier}  [{h} vs {a}]")
        print(f"    {title!r}")
    if len(improvements) > 20:
        print(f"  ... and {len(improvements) - 20} more")
    print()

print()
print("COVERAGE DELTA: " + (
    f"+{new_pass - old_pass}" if new_pass >= old_pass else str(new_pass - old_pass)
))
