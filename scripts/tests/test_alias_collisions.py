"""Standing regression guard for the team-title alias system.

Enforces the core safety invariant: within any one competition, no title token
is shared by two different teams. A shared token could let the matcher attribute
one club's highlight to another (a wrong join), so any alias change — manual
TEAM_TITLE_ALIASES edit OR an auto-derived leading word — that introduces such a
collision must fail CI.

Reads the real roster from sources.json teamLists, so it stays valid as teams
are promoted/relegated.
"""
import io
import json
from collections import defaultdict
from pathlib import Path

import highlights_common as H
from highlights_common import team_tokens, TEAM_TITLE_ALIASES, _leading_alias_word, _team_word_index

_SOURCES = Path(__file__).resolve().parents[2] / "sources.json"


def _roster():
    raw = json.load(io.open(_SOURCES, encoding="utf-8"))
    return raw.get("teamLists", {})


def test_no_within_competition_token_collisions():
    """No token may match ≥2 teams inside the same competition."""
    collisions = []
    for comp, teams in _roster().items():
        tok2teams = defaultdict(set)
        for t in teams:
            name = t.get("name", "")
            for tk in set(team_tokens(name, "", t.get("tla", ""))):
                tok2teams[tk].add(name)
        for tk, owners in tok2teams.items():
            if len(owners) >= 2:
                collisions.append((comp, tk, sorted(owners)))
    assert not collisions, (
        "within-competition token collisions found (would enable wrong joins):\n"
        + "\n".join(f"  [{c}] '{tk}' -> {o}" for c, tk, o in collisions)
    )


def test_layer1_derives_expected_distinctive_shortforms():
    """Positive guard: known auto teams still get their distinctive leading word."""
    def lead(name):
        w = _leading_alias_word(name)
        return w if w and _team_word_index().get(w, set()) <= {name} else None

    # Coventry City FC / Hull City AFC have no manual entry → auto leading word.
    assert lead("Coventry City FC") == "coventry"
    assert lead("Hull City AFC") == "hull"
    # ...but the generic club-type word is never derived.
    assert lead("Some City FC") != "city"


def test_alias_overrides_append_not_replace(monkeypatch):
    """Admin-approved overrides (team-aliases.json) ADD tokens, keep the base."""
    monkeypatch.setattr(H, "_team_alias_overrides",
                        lambda: {"FK Shakhtar Donetsk": ["Shakhtar", "Shakhtar Donetsk"]})
    toks = team_tokens("FK Shakhtar Donetsk", "", "SHD")
    assert "shakhtar" in toks and "shakhtar donetsk" in toks  # override added
    assert "fk shakhtar donetsk" in toks                       # base kept


def test_alias_overrides_empty_is_noop(monkeypatch):
    """No override file / empty dict leaves team_tokens exactly as before."""
    monkeypatch.setattr(H, "_team_alias_overrides", lambda: {})
    assert team_tokens("Chelsea FC", "Chelsea", "CHE") == \
        [H._normalize(a) for a in TEAM_TITLE_ALIASES["Chelsea FC"]]


def test_generic_words_never_become_standalone_tokens():
    """No team's effective tokens include a bare generic club-type word."""
    banned = {"town", "city", "united", "rovers", "wanderers", "county"}
    offenders = []
    for comp, teams in _roster().items():
        for t in teams:
            name = t.get("name", "")
            bad = banned & set(team_tokens(name, "", t.get("tla", "")))
            if bad:
                offenders.append((comp, name, sorted(bad)))
    assert not offenders, f"generic bare tokens leaked: {offenders}"
