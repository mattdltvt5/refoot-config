"""Pure-function unit tests for highlights_common.py.

No network calls, no API keys, no file writes — runs in the pre-commit hook.

Invariants covered:
  1.  _normalize: NFKD drops combining marks (diacritics stripped)
  2.  team_tokens: TEAM_TITLE_ALIASES override path replaces _auto_tokens entirely
  3.  is_highlight_title: blocklist always wins over allowlist
  4.  Nott'm Forest apostrophe (U+0027) alias present and survives normalisation
  6.  Aston Villa bare "Villa" token present (regression for commit ee41769)
 15.  require_allowlist=False (bypass) skips allowlist but never the blocklist
"""

from highlights_common import (
    TEAM_TITLE_ALIASES,
    _auto_tokens,
    _leading_alias_word,
    _normalize,
    _team_word_index,
    is_highlight_title,
    team_tokens,
)


# ── Invariant 1: _normalize diacritic stripping ──────────────────────────────

def test_normalize_strips_acute_accent():
    """NFKD dropping Mn (combining marks) removes acute accent from é."""
    assert _normalize("Atlético") == "atletico"


def test_normalize_strips_umlaut():
    """NFKD dropping Mn removes umlaut from ü."""
    assert _normalize("Bayern München") == "bayern munchen"


def test_normalize_strips_cedilla_ligature():
    """NFKD dropping Mn removes cedilla from ç."""
    assert _normalize("Barça") == "barca"


def test_normalize_casefolds():
    """_normalize casefolding converts uppercase to lowercase."""
    assert _normalize("Arsenal FC") == "arsenal fc"


def test_normalize_preserves_apostrophe():
    """Apostrophe U+0027 is not a combining mark (category Po) — must survive."""
    assert _normalize("Nott'm Forest") == "nott'm forest"


# ── Invariant 2: team_tokens alias override ───────────────────────────────────

def test_team_tokens_override_path_used_for_registered_team():
    """team_tokens returns exactly the normalised TEAM_TITLE_ALIASES list."""
    tokens = team_tokens("FC Barcelona", "Barcelona")
    expected = [_normalize(a) for a in TEAM_TITLE_ALIASES["FC Barcelona"]]
    assert tokens == expected


def test_team_tokens_contains_short_form_for_barcelona():
    """FC Barcelona alias includes the bare 'barcelona' short form."""
    tokens = team_tokens("FC Barcelona", "Barcelona")
    assert "barcelona" in tokens


def test_team_tokens_unregistered_uses_auto_derivation():
    """A team not in TEAM_TITLE_ALIASES falls through to _auto_tokens."""
    tokens = team_tokens("Hypothetical United FC", "Hypothetical United")
    # The base name must appear in some normalised form
    assert any("hypothetical" in t for t in tokens)


# ── Invariant 3: blocklist beats allowlist ────────────────────────────────────

def test_blocklist_rejects_before_allowlist():
    """A title containing a blocklist term is rejected even when it also has an allowlist term.

    "press conference" is in TITLE_BLOCKLIST; "highlights" is in TITLE_ALLOWLIST.
    Blocklist is evaluated first — the allowlist match must not rescue the video.
    """
    result = is_highlight_title("Arsenal vs Chelsea Highlights | Press Conference")
    assert result is False, (
        "blocklist term 'press conference' must win over allowlist term 'highlights'"
    )


def test_clean_highlight_title_accepted():
    """A title with an allowlist term and no blocklist term is accepted."""
    assert is_highlight_title("Arsenal vs Chelsea | Match Highlights") is True


def test_title_with_no_allowlist_term_rejected():
    """A title with no allowlist term is rejected by default (require_allowlist=True)."""
    assert is_highlight_title("Arsenal vs Chelsea 2-1") is False


# ── Invariant 15: bypass (require_allowlist=False) ───────────────────────────

def test_require_allowlist_false_skips_allowlist_check():
    """require_allowlist=False (tier 1c/1d/4 bypass) accepts a no-keyword title."""
    # Scoreline-only title: no blocklist term, no allowlist term.
    # With bypass: allowlist skipped → accepted.
    assert is_highlight_title("Arsenal vs Chelsea 2-1", require_allowlist=False) is True


def test_blocklist_still_applies_when_allowlist_bypassed():
    """Even with require_allowlist=False, a blocklist term still rejects the video."""
    result = is_highlight_title(
        "Arsenal vs Chelsea | Interview", require_allowlist=False
    )
    assert result is False, (
        "blocklist term 'interview' must reject even when allowlist is bypassed"
    )


# ── Invariant 4: Nott'm Forest apostrophe alias ───────────────────────────────

def test_nottingham_forest_has_apostrophe_alias():
    """Nottingham Forest FC must have a 'Nott\\'m Forest' entry in TEAM_TITLE_ALIASES.

    Regression: without this alias, broadcaster titles like
    "Nott'm Forest 2-1 Arsenal | Highlights" never match.
    """
    assert "Nottingham Forest FC" in TEAM_TITLE_ALIASES, (
        "Nottingham Forest FC must be in TEAM_TITLE_ALIASES"
    )
    aliases = TEAM_TITLE_ALIASES["Nottingham Forest FC"]
    apostrophe_aliases = [a for a in aliases if "'" in a]
    assert apostrophe_aliases, (
        f"No apostrophe-containing alias found; aliases: {aliases}"
    )


def test_nottingham_forest_apostrophe_token_matches_broadcaster_title():
    """Normalised Nott'm Forest token must appear in a normalised broadcaster title."""
    tokens = team_tokens("Nottingham Forest FC", "Nott'm Forest")
    sample_title = "Nott'm Forest vs Arsenal | Match Highlights"
    norm = _normalize(sample_title)
    assert any(tok in norm for tok in tokens), (
        f"No token matched; tokens={tokens}, norm_title={norm!r}"
    )


# ── Invariant 6: Aston Villa bare-word "Villa" token ─────────────────────────

def test_aston_villa_has_bare_villa_token():
    """'villa' bare-word token must be present for Aston Villa FC.

    Regression for commit ee41769 — the alias was absent before that fix.
    Broadcast titles often use just "Villa" without "Aston".
    """
    assert "Aston Villa FC" in TEAM_TITLE_ALIASES, (
        "Aston Villa FC must be in TEAM_TITLE_ALIASES"
    )
    tokens = team_tokens("Aston Villa FC", "Aston Villa")
    assert "villa" in tokens, (
        f"Bare 'villa' token missing; tokens: {tokens}"
    )


# ── Invariant: uniqueness-gated auto leading-word alias (Layer 1) ─────────────

def test_auto_tokens_derives_unique_leading_word():
    """A club whose leading word is unique across tracked teams gets it as a bare
    token. 'Ipswich' is unique → auto-derived, so broadcaster titles like
    'Man Utd 5-2 Ipswich' join without a manual TEAM_TITLE_ALIASES entry."""
    tokens = _auto_tokens("Ipswich Town FC", "Ipswich Town", "IPS")
    assert "ipswich" in tokens, f"unique leading word not derived; tokens: {tokens}"


def test_auto_tokens_rejects_ambiguous_leading_word():
    """A leading word shared by ≥2 tracked clubs must NOT become a bare token —
    'Manchester' (City + United) and 'Madrid' (Real + Atlético + Rayo) are unsafe."""
    man = _auto_tokens("Manchester United FC", "Manchester United", "MUN")
    assert "manchester" not in man, f"ambiguous 'manchester' leaked; tokens: {man}"
    mad = _auto_tokens("Real Madrid CF", "Real Madrid", "RMA")
    assert "madrid" not in mad, f"ambiguous 'madrid' leaked; tokens: {mad}"


def test_leading_alias_word_screens_shape():
    """_leading_alias_word skips org abbreviations and rejects generic prefixes."""
    assert _leading_alias_word("AFC Bournemouth") == "bournemouth"  # skips 'AFC'
    assert _leading_alias_word("Ipswich Town FC") == "ipswich"
    assert _leading_alias_word("Real Madrid CF") is None            # 'real' is generic


def test_team_word_index_flags_shared_words():
    """The collision index maps a shared word to ≥2 teams and a unique one to ≤1."""
    idx = _team_word_index()
    assert len(idx.get("manchester", set())) >= 2   # City + United
    assert len(idx.get("ipswich", set())) == 1


# ── UCL small-club alias gaps (PAE AEK, ŠK Slovan Bratislava) ─────────────────

def test_pae_aek_has_aek_alias():
    """'PAE AEK' must expose 'aek' — broadcasters drop the Greek 'PAE' prefix.
    Without this only 'pae aek' is derived and titles like 'AEK 1-0 …' never match."""
    assert "PAE AEK" in TEAM_TITLE_ALIASES
    tokens = team_tokens("PAE AEK", "", "AEK")
    assert "aek" in tokens, f"bare 'aek' token missing; tokens: {tokens}"


def test_slovan_bratislava_has_slovan_aliases():
    """'ŠK Slovan Bratislava' must expose 'slovan bratislava' and 'slovan' — the
    leading 'ŠK' club prefix is otherwise not stripped, leaving only the full form."""
    assert "ŠK Slovan Bratislava" in TEAM_TITLE_ALIASES
    tokens = team_tokens("ŠK Slovan Bratislava", "", "SBA")
    assert "slovan bratislava" in tokens, f"tokens: {tokens}"
    assert "slovan" in tokens, f"bare 'slovan' token missing; tokens: {tokens}"
