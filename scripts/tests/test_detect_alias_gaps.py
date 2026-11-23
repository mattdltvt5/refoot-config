"""Unit tests for the alias-gap detector's pure decision logic.

No network, no API key — imports detect_alias_gaps (import-safe: its runnable
body is under main()).
"""
from detect_alias_gaps import (
    is_alias_miss, propose_alias, unique_alias, to_fix_tournament, TOURNAMENTS,
)


# ── tournament coverage (UCL/Euro/WC/Copa) ────────────────────────────────────

def test_tournaments_include_ucl():
    assert TOURNAMENTS.get("ucl") == "Champions League"


def test_to_fix_tournament_shape():
    m = {
        "match_id": 575332, "utcDate": "2026-09-09T19:00:00Z", "matchday": 1,
        "homeTeam": {"id": 524, "name": "Paris Saint-Germain FC", "tla": "PSG"},
        "awayTeam": {"id": 7509, "name": "ŠK Slovan Bratislava", "tla": "SBA"},
    }
    fix = to_fix_tournament(m)
    assert fix["match_id"] == 575332
    assert fix["home_team"] == "Paris Saint-Germain FC" and fix["home_tla"] == "PSG"
    assert fix["away_team"] == "ŠK Slovan Bratislava" and fix["away_tla"] == "SBA"
    assert fix["home_short"] == "" and fix["away_short"] == ""   # no shortName in tournament data
    assert fix["date"] == "2026-09-09" and fix["matchday"] == 1


# ── is_alias_miss: only a pure cross-match team miss counts ───────────────────

def test_is_alias_miss_true_for_pure_cross_match():
    assert is_alias_miss(["cross-match-guard:away-missing (need one of ['ipswich town'])"])


def test_is_alias_miss_false_when_any_other_reason():
    # date-window rejection means it wasn't purely a token miss
    assert not is_alias_miss([
        "cross-match-guard:away-missing (...)",
        "outside-date-window (2026-09-07)",
    ])


def test_is_alias_miss_false_for_empty():
    assert not is_alias_miss([])


# ── propose_alias: only a word-subsequence of the official name ───────────────

def test_propose_alias_extracts_official_short_form():
    alias, is_multi = propose_alias(
        "Ipswich Town FC", "A Bruno Hat-Trick! | Man Utd 5-2 Ipswich | Highlights",
        current_tokens={"ipswich town fc", "ipswich town"})
    assert alias == "ipswich" and is_multi is False


def test_propose_alias_skips_already_covered_token():
    # If 'ipswich' is already a token (Layer 1), nothing new is proposed.
    alias, _ = propose_alias(
        "Ipswich Town FC", "Man Utd 5-2 Ipswich | Highlights",
        current_tokens={"ipswich town fc", "ipswich town", "ipswich"})
    assert alias is None


def test_propose_alias_refuses_nickname_not_in_official_name():
    # 'Tractor Boys' is not part of the FD name → cannot be proposed.
    alias, _ = propose_alias(
        "Ipswich Town FC", "COME ON, TRACTOR BOYS! Town win",
        current_tokens={"ipswich town fc", "ipswich town"})
    assert alias is None


# ── unique_alias: the collision gate ──────────────────────────────────────────

def test_unique_alias_accepts_unique_word():
    assert unique_alias("ipswich", "Ipswich Town FC")


def test_unique_alias_rejects_shared_word():
    # 'manchester' belongs to City + United → not safe as a bare alias.
    assert not unique_alias("manchester", "Manchester United FC")


# ── admin rejections (alias-rejections.json) ─────────────────────────────────

import detect_alias_gaps as dag  # noqa: E402

def test_load_rejections_normalises_and_ignores_non_lists(tmp_path):
    p = tmp_path / "alias-rejections.json"
    p.write_text('{"_comment": "x", "1. FC Union Berlin": ["1. FC", " 1. fc "], '
                 '"Eintracht Frankfurt": ["eintracht"]}', encoding="utf-8")
    rej = dag.load_rejections(p)
    assert rej == {"1. FC Union Berlin": {"1. fc"}, "Eintracht Frankfurt": {"eintracht"}}
    assert dag.is_rejected(rej, "1. FC Union Berlin", "1. Fc")
    assert not dag.is_rejected(rej, "1. FC Union Berlin", "union")
    assert not dag.is_rejected(rej, "FC Augsburg", "1. fc")


def test_load_rejections_missing_or_broken_is_empty(tmp_path):
    assert dag.load_rejections(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert dag.load_rejections(bad) == {}


def test_repo_rejections_file_is_valid_and_main_uses_it():
    import json
    from pathlib import Path
    raw = json.loads((Path(dag.__file__).resolve().parents[1] / "alias-rejections.json").read_text(encoding="utf-8"))
    assert "_comment" in raw
    assert all(isinstance(v, list) for k, v in raw.items() if k != "_comment")
    src = Path(dag.__file__).read_text(encoding="utf-8")
    assert "rejections = load_rejections()" in src
    assert "is_rejected(rejections, fd_name, alias)" in src


def test_admin_has_reject_wired_to_the_rejections_file():
    from pathlib import Path
    admin = (Path(dag.__file__).resolve().parents[1] / "admin.html").read_text(encoding="utf-8")
    assert "const ALIAS_REJECT_FILE   = 'alias-rejections.json';" in admin
    assert "rejectAlias(team, row.alias, card, rej)" in admin
    assert "if (aliasRejected(team, row.alias)) return;" in admin


def test_admin_reads_decision_files_fresh_not_from_the_cdn():
    """raw.githubusercontent.com can serve a copy up to ~5 min old, so a
    just-rejected alias reappeared on reload. Decision files go through
    readConfigJson (GitHub contents API when signed in), and this session's
    decisions are merged into every reload."""
    from pathlib import Path
    admin = (Path(dag.__file__).resolve().parents[1] / "admin.html").read_text(encoding="utf-8")
    assert "async function readConfigJson(path)" in admin
    for f in ("ALIAS_OVERRIDE_FILE", "ALIAS_REJECT_FILE", "COUNTRIES_FILE"):
        assert f"readConfigJson({f})" in admin, f
        assert f"fetch(base + {f}" not in admin, f
    assert "mergeLists(lists(rejRaw), aliasSessionRejected)" in admin
    assert "mergeLists(lists(ovrRaw), aliasSessionApproved)" in admin
