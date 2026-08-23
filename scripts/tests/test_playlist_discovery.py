"""Tests for automatic per-season playlist discovery (playlist_discovery.py).

The matcher is fed SYNTHETIC playlist lists built from REAL titles observed in
the keyed read-only discovery run — no network / API key required. Clock is
pinned to 2026-09-01 so the "current" season is 2026/27 (edition years: WC 2026,
Euro 2024) regardless of when the suite runs.
"""

import json
import sys
import pathlib
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from playlist_discovery import (
    select_current_season_playlist,
    current_season_tokens,
    is_competition_available,
    available_competitions,
    apply_discovered_overrides,
)

NOW = datetime(2026, 9, 1)


def _pl(title, pid, items=50, pub="2026-08-20"):
    return {"title": title, "id": pid, "itemCount": items, "publishedAt": pub}


def _pick(comp, titles_ids, **kw):
    pls = [_pl(t, i) for t, i in titles_ids]
    return select_current_season_playlist(comp, pls, now=NOW, **kw)


# ── Season tokens ────────────────────────────────────────────────────────────

def test_season_tokens_league_three_formats():
    mode, toks = current_season_tokens("Serie A", now=NOW)
    assert mode == "season"
    assert toks == ["2026/27", "26/27", "2026-27"]

def test_season_tokens_tournament_edition_year():
    assert current_season_tokens("World Cup", now=NOW) == ("edition", ["2026"])
    assert current_season_tokens("Euro Cup", now=NOW) == ("edition", ["2024"])


# ── Core: picks current season, excludes decoys, gates by competition ─────────

CBS = [
    ("2026/27 Champions League Extended Highlights", "CUR_CL"),
    ("2025/26 Champions League Extended Highlights", "OLD_CL"),
    ("24/25 UEFA Champions League Extended Highlights", "OLD_CL2"),
    ("26/27 Serie A: Extended Highlights", "CUR_SA"),
    ("25/26 Serie A | Extended Highlights", "OLD_SA"),
    ("Champions League Highlights | Extended Highlights from EVERY UCL game", "DECOY_EVERY"),
    ("Scoreline | Match Highlights Across Multiple Leagues", "DECOY_MIX"),
    ("UCL Classic Highlights", "DECOY_CLASSIC"),
    ("Post-Match Interviews | UCL, UEL, UECL and more!", "DECOY_INT"),
]

def test_picks_current_over_prior_season():
    assert _pick("Champions League", CBS).get("id") == "CUR_CL"

def test_competition_gate_never_cross_picks():
    # Resolving Serie A on the same shared channel must pick the Serie A playlist,
    # never a Champions League one.
    assert _pick("Serie A", CBS).get("id") == "CUR_SA"

def test_excludes_every_and_multiple_leagues_decoys():
    got = _pick("Champions League", CBS)
    assert got["id"] not in {"DECOY_EVERY", "DECOY_MIX", "DECOY_CLASSIC", "DECOY_INT"}


# ── Season format variants all accepted ──────────────────────────────────────

def test_accepts_yyyy_slash_yy():
    assert _pick("Serie A", [("2026/27 Serie A Extended Highlights", "A")]).get("id") == "A"

def test_accepts_yy_slash_yy():
    assert _pick("Serie A", [("26/27 Serie A | Extended Highlights", "B")]).get("id") == "B"

def test_accepts_yyyy_dash_yy():
    assert _pick("Serie A", [("Serie A 2026-27 Extended Highlights", "C")]).get("id") == "C"


# ── Tournaments match by edition year ────────────────────────────────────────

def test_tournament_edition_year_match():
    got = _pick("World Cup", [
        ("Full Game Highlights | 2026 FIFA World Cup", "WC26"),   # 'full game' is NOT a decoy
        ("2022 FIFA World Cup Highlights", "WC22"),
    ])
    assert got.get("id") == "WC26"


# ── No-season rolling fallback (ESPN LaLiga style) ───────────────────────────

def test_rolling_no_season_fallback():
    got = _pick("LaLiga", [
        ("LALIGA Highlights | ESPN FC", "ROLL"),            # no season, has highlight term
        ("LALIGA 24/25 Highlights | ESPN FC", "OLD"),       # past season → must NOT win
    ])
    assert got.get("id") == "ROLL"

def test_no_highlight_term_still_matches_by_season():
    # Real case: "Ligue 1 2026/27" (beIN) and "UEFA Europa League 2026/27" carry
    # no highlight word — season + competition gate must still select them.
    assert _pick("Ligue 1", [("Ligue 1 2026/27", "L1")]).get("id") == "L1"


# ── No confident match → None (caller keeps last-known-good) ──────────────────

def test_no_match_returns_none():
    # Only a PAST-season playlist exists → not current, not rolling → None.
    assert _pick("Serie A", [("25/26 Serie A | Extended Highlights", "OLD")]) is None

def test_empty_returns_none():
    assert _pick("Serie A", []) is None


# ── Team playlists: single-team channel, competition-gate disabled ────────────

def test_team_playlist_no_competition_gate():
    got = _pick("Premier League", [
        ("2026/27 Match Highlights", "CUR"),   # no "premier league" in title
        ("2025/26 Highlights", "OLD"),
    ], require_competition_gate=False)
    assert got.get("id") == "CUR"


# ── Availability scoping (synthetic repo tree; no network) ────────────────────

def test_availability_signal(tmp_path):
    (tmp_path / "fixtures" / "premier-league").mkdir(parents=True)
    (tmp_path / "fixtures" / "premier-league" / "2026.json").write_text(
        json.dumps({"fixtures": [{"match_id": 1}]}), encoding="utf-8")
    (tmp_path / "tournament-groups").mkdir()
    (tmp_path / "tournament-groups" / "ucl.json").write_text(
        json.dumps({"matches": [{"id": 1, "utcDate": "2026-03-10T20:00:00Z"}]}), encoding="utf-8")
    # Copa present but 0 dated matches → unavailable; no uel file → unavailable.
    (tmp_path / "tournament-groups" / "copa-america.json").write_text(
        json.dumps({"matches": [{"homeTeam": {}, "score": {}}]}), encoding="utf-8")

    assert is_competition_available("Premier League", repo_root=tmp_path) is True
    assert is_competition_available("Champions League", repo_root=tmp_path) is True
    assert is_competition_available("Copa America", repo_root=tmp_path) is False
    assert is_competition_available("Europa League", repo_root=tmp_path) is False
    avail = available_competitions(repo_root=tmp_path)
    assert "Europa League" not in avail and "Copa America" not in avail
    assert {"Premier League", "Champions League"} <= avail


# ── Override loader ──────────────────────────────────────────────────────────

def test_apply_discovered_overrides(tmp_path):
    path = tmp_path / "discovered-playlists.json"
    path.write_text(json.dumps({
        "resolved": {"Champions League": {"CBS Sport Golazo": "NEW_CL"}},
        "team":     {"Premier League": {"Arsenal FC": "NEW_ARS"}},
    }), encoding="utf-8")
    config = {
        "competition_playlists": {"Champions League": {"CBS Sport Golazo": ["OLD_CL"],
                                                       "TUDN USA": ["KEEP"]}},
        "team_playlists":        {"Premier League": {"Arsenal FC": "OLD_ARS",
                                                     "Chelsea FC": "KEEP2"}},
    }
    out = apply_discovered_overrides(config, path=path)
    assert out["competition_playlists"]["Champions League"]["CBS Sport Golazo"] == ["NEW_CL"]
    assert out["competition_playlists"]["Champions League"]["TUDN USA"] == ["KEEP"]  # untouched
    assert out["team_playlists"]["Premier League"]["Arsenal FC"] == "NEW_ARS"
    assert out["team_playlists"]["Premier League"]["Chelsea FC"] == "KEEP2"          # untouched

def test_apply_discovered_overrides_noop_when_absent(tmp_path):
    config = {"competition_playlists": {"X": {"b": ["ID"]}}, "team_playlists": {}}
    out = apply_discovered_overrides(config, path=tmp_path / "missing.json")
    assert out["competition_playlists"]["X"]["b"] == ["ID"]
