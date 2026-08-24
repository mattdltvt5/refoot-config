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
    migrate_flat_discovered,
    merge_discovered_seasons,
    write_discovered_if_changed,
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


# ── Season-nested store: migrate / merge / write ─────────────────────────────

def test_merge_preserves_prior_seasons():
    existing = {"resolved": {"2025": {"Champions League": {"CBS": "CL25"}}},
                "team":     {"2025": {"Premier League": {"Arsenal FC": "ARS25"}}}}
    merged = merge_discovered_seasons(
        existing,
        {"2026": {"Champions League": {"CBS": "CL26"}}},   # new season this run
        {"2026": {"Premier League": {"Arsenal FC": "ARS26"}}})
    # Both seasons present; prior season untouched.
    assert merged["resolved"]["2025"]["Champions League"]["CBS"] == "CL25"
    assert merged["resolved"]["2026"]["Champions League"]["CBS"] == "CL26"
    assert merged["team"]["2025"]["Premier League"]["Arsenal FC"] == "ARS25"
    assert merged["team"]["2026"]["Premier League"]["Arsenal FC"] == "ARS26"

def test_merge_same_season_updates_leaf_keeps_others():
    existing = {"resolved": {"2026": {"Champions League": {"CBS": "OLD", "TUDN": "KEEP"}}},
                "team": {}}
    merged = merge_discovered_seasons(
        existing, {"2026": {"Champions League": {"CBS": "NEW"}}}, {})
    assert merged["resolved"]["2026"]["Champions League"]["CBS"] == "NEW"   # updated
    assert merged["resolved"]["2026"]["Champions League"]["TUDN"] == "KEEP" # preserved

def test_migrate_flat_uses_current_season_stamp():
    flat = {"current_season": 2025,
            "resolved": {"Champions League": {"CBS": "CL"}},
            "team": {"Premier League": {"Arsenal FC": "ARS"}}}
    m = migrate_flat_discovered(flat)
    assert m["resolved"] == {"2025": {"Champions League": {"CBS": "CL"}}}
    assert m["team"] == {"2025": {"Premier League": {"Arsenal FC": "ARS"}}}

def test_migrate_flat_without_stamp_is_empty_not_crash():
    m = migrate_flat_discovered({"resolved": {"Champions League": {"CBS": "CL"}}})
    assert m == {"resolved": {}, "team": {}}

def test_migrate_none_and_already_nested():
    assert migrate_flat_discovered(None) == {"resolved": {}, "team": {}}
    nested = {"resolved": {"2026": {"X": {"b": "i"}}}, "team": {}}
    assert migrate_flat_discovered(nested)["resolved"] == {"2026": {"X": {"b": "i"}}}

def test_write_discovered_is_idempotent(tmp_path):
    path = tmp_path / "discovered-playlists.json"
    base = {"current_season": 2026, "flags": [],
            "resolved": {"2026": {"Champions League": {"CBS": "CL26"}}}, "team": {}}
    assert write_discovered_if_changed(path, {**base, "generated_at": "T1",
                                              "estimated_units": 10}) is True
    bytes1 = path.read_bytes()
    # Same content, different run metadata → no rewrite, byte-identical file.
    assert write_discovered_if_changed(path, {**base, "generated_at": "T2",
                                              "estimated_units": 99}) is False
    assert path.read_bytes() == bytes1
    # Changed content → rewrite.
    changed = {**base, "resolved": {"2026": {"Champions League": {"CBS": "CL26b"}}},
               "generated_at": "T3", "estimated_units": 11}
    assert write_discovered_if_changed(path, changed) is True


# ── Season-aware override loader ─────────────────────────────────────────────

def test_apply_discovered_overrides_season_correct(tmp_path):
    # now=2026 → season_for_competition = 2026 for CL and PL.
    path = tmp_path / "discovered-playlists.json"
    path.write_text(json.dumps({
        "resolved": {"2026": {"Champions League": {"CBS Sport Golazo": "NEW_CL"}},
                     "2025": {"Champions League": {"CBS Sport Golazo": "OLD_CL_PRIOR"}}},
        "team":     {"2026": {"Premier League": {"Arsenal FC": "NEW_ARS"}}},
    }), encoding="utf-8")
    config = {
        "competition_playlists": {"Champions League": {"CBS Sport Golazo": ["OLD_CL"],
                                                       "TUDN USA": ["KEEP"]}},
        "team_playlists":        {"Premier League": {"Arsenal FC": "OLD_ARS",
                                                     "Chelsea FC": "KEEP2"}},
    }
    out = apply_discovered_overrides(config, path=path, now=NOW)
    # Applies the 2026 (target-season) id, NOT the 2025 one.
    assert out["competition_playlists"]["Champions League"]["CBS Sport Golazo"] == ["NEW_CL"]
    assert out["competition_playlists"]["Champions League"]["TUDN USA"] == ["KEEP"]
    assert out["team_playlists"]["Premier League"]["Arsenal FC"] == "NEW_ARS"
    assert out["team_playlists"]["Premier League"]["Chelsea FC"] == "KEEP2"

def test_apply_discovered_overrides_absent_season_keeps_last_known_good(tmp_path):
    # Only a PRIOR-season (2025) entry exists; target season 2026 has none →
    # no override, sources.json last-known-good retained.
    path = tmp_path / "discovered-playlists.json"
    path.write_text(json.dumps({
        "resolved": {"2025": {"Champions League": {"CBS Sport Golazo": "PRIOR"}}},
        "team": {},
    }), encoding="utf-8")
    config = {"competition_playlists": {"Champions League": {"CBS Sport Golazo": ["OLD_CL"]}},
              "team_playlists": {}}
    out = apply_discovered_overrides(config, path=path, now=NOW)
    assert out["competition_playlists"]["Champions League"]["CBS Sport Golazo"] == ["OLD_CL"]

def test_apply_discovered_overrides_old_flat_shape_compat(tmp_path):
    # Old flat file with a 2026 stamp → migrated on read → applied for season 2026.
    path = tmp_path / "discovered-playlists.json"
    path.write_text(json.dumps({
        "current_season": 2026,
        "resolved": {"Champions League": {"CBS Sport Golazo": "FLAT_CL"}},
        "team": {},
    }), encoding="utf-8")
    config = {"competition_playlists": {"Champions League": {"CBS Sport Golazo": ["OLD_CL"]}},
              "team_playlists": {}}
    out = apply_discovered_overrides(config, path=path, now=NOW)
    assert out["competition_playlists"]["Champions League"]["CBS Sport Golazo"] == ["FLAT_CL"]

def test_apply_discovered_overrides_noop_when_absent(tmp_path):
    config = {"competition_playlists": {"X": {"b": ["ID"]}}, "team_playlists": {}}
    out = apply_discovered_overrides(config, path=tmp_path / "missing.json")
    assert out["competition_playlists"]["X"]["b"] == ["ID"]
