"""
Rights registry (rights/{season}.json) — no network.

  - Both seed files validate against rights/rights.schema.json.
  - Seed data: all entries unverified, no channels; to-confirm pairs have no holder.
  - resolve_season reuses the August rule (31 Jul / 1 Aug) and resolves
    tournaments by edition.
  - active_entries applies valid_from / valid_until, and switches to the 2027
    file once the 2027 season starts.
  - A missing season file returns [] with a warning.
"""

import json
import logging
from datetime import date
from pathlib import Path

import jsonschema
import pytest

import rights_registry as rr
from highlights_common import COMPETITION_SLUG_MAP, LAUNCH_COUNTRIES, REPO_ROOT

RIGHTS = REPO_ROOT / "rights"
SCHEMA = json.loads((RIGHTS / "rights.schema.json").read_text(encoding="utf-8"))
SEED_SEASONS = (2026, 2027)


def _load(season):
    return json.loads((RIGHTS / f"{season}.json").read_text(encoding="utf-8"))


# ── Schema + seed data ────────────────────────────────────────────────────────

def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("season", SEED_SEASONS)
def test_seed_file_validates_against_schema(season):
    jsonschema.Draft202012Validator(SCHEMA).validate(_load(season))


@pytest.mark.parametrize("season", SEED_SEASONS)
def test_seed_entries_unverified_without_channels(season):
    doc = _load(season)
    assert doc["season"] == season
    for e in doc["entries"]:
        assert e["status"] == "unverified", e
        assert e["channels"] == [], e
        assert e["competition"] in COMPETITION_SLUG_MAP.values(), e
        assert e["country"] in LAUNCH_COUNTRIES and e["country"] != "UK", e


@pytest.mark.parametrize("season", SEED_SEASONS)
def test_to_confirm_pairs_have_no_guessed_holder(season):
    for e in _load(season)["entries"]:
        if e["holder"] is None:
            assert "to confirm" in e.get("notes", "").lower(), e
        else:
            assert "to confirm" not in e.get("notes", "").lower(), e


def test_schema_rejects_uk_and_bad_role():
    v = jsonschema.Draft202012Validator(SCHEMA)
    base = {"competition": "laliga", "country": "GB", "holder": "BBC", "role": "highlights",
            "channels": [], "status": "unverified"}
    assert v.is_valid({"season": 2026, "entries": [base]})
    assert not v.is_valid({"season": 2026, "entries": [{**base, "country": "UK"}]})
    assert not v.is_valid({"season": 2026, "entries": [{**base, "role": "both"}]})
    assert not v.is_valid({"season": 2026, "entries": [{**base, "status": "maybe"}]})
    assert not v.is_valid({"season": 2026, "entries": [
        {**base, "channels": [{"channel_id": "not-a-channel", "playlist_ids": []}]}]})


def test_seed_2026_covers_every_pair_and_keeps_roles_explicit():
    entries = _load(2026)["entries"]
    comps = ["premier-league", "laliga", "serie-a", "bundesliga", "ligue-1", "ucl", "uel"]
    pairs = {(e["competition"], e["country"]) for e in entries}
    assert pairs == {(c, cc) for c in comps for cc in LAUNCH_COUNTRIES}
    gb_laliga = {(e["holder"], e["role"]) for e in entries
                 if e["competition"] == "laliga" and e["country"] == "GB"}
    assert gb_laliga == {("Premier Sports", "live"), ("Disney+", "live"),
                         ("BBC", "highlights"), ("DAZN", "highlights")}
    tbc = {(e["competition"], e["country"]) for e in entries if e["holder"] is None}
    assert tbc == {("premier-league", "IE"), ("bundesliga", "IE"), ("uel", "IE")}


# ── Season resolution (reuses season_for_competition) ─────────────────────────

def test_resolve_season_august_threshold():
    assert rr.resolve_season("bundesliga", date(2027, 7, 31)) == 2026
    assert rr.resolve_season("bundesliga", date(2027, 8, 1)) == 2027
    assert rr.resolve_season("Champions League", date(2027, 7, 31)) == 2026
    assert rr.resolve_season("ucl", date(2027, 8, 1)) == 2027


def test_resolve_season_tournament_by_edition_not_august():
    # World Cup 2026 is played June–July 2026: still the 2026 edition before AND
    # after 1 August, and in spring 2027 (no August cut).
    assert rr.resolve_season("world-cup", date(2026, 6, 20)) == 2026
    assert rr.resolve_season("world-cup", date(2026, 7, 31)) == 2026
    assert rr.resolve_season("world-cup", date(2026, 8, 1)) == 2026
    assert rr.resolve_season("World Cup", date(2027, 3, 1)) == 2026
    assert rr.resolve_season("euro-cup", date(2028, 6, 15)) == 2028


# ── active_entries ────────────────────────────────────────────────────────────

def _holders(entries):
    return sorted(e["holder"] for e in entries if e["holder"])


def test_seed_ucl_gb_switches_with_2027_season():
    assert _holders(rr.active_entries("ucl", "GB", date(2027, 7, 31))) == ["TNT Sports"]
    assert _holders(rr.active_entries("ucl", "GB", date(2027, 8, 1))) == ["Paramount+", "Prime Video"]
    assert _holders(rr.active_entries("Champions League", "IE", date(2027, 9, 1))) == ["Paramount+"]
    assert _holders(rr.active_entries("ucl", "CA", date(2027, 9, 1))) == ["Paramount+"]
    assert _holders(rr.active_entries("uel", "GB", date(2027, 9, 1))) == ["Sky Sports"]


def test_seed_bundesliga_us_live_holders():
    got = rr.active_entries("bundesliga", "US", date(2026, 11, 21))
    assert _holders(got) == ["Telemundo", "USA Sports"]
    assert {e["role"] for e in got} == {"live"}


def _write(tmp_path, season, entries):
    (tmp_path / f"{season}.json").write_text(
        json.dumps({"season": season, "entries": entries}), encoding="utf-8")


def _entry(holder, **kw):
    return {"competition": "bundesliga", "country": "US", "holder": holder, "role": "live",
            "channels": [], "status": "unverified", **kw}


def test_mid_season_valid_from_switches_holder_on_the_right_date(tmp_path):
    _write(tmp_path, 2026, [
        _entry("Old Holder", valid_until="2027-01-14"),
        _entry("New Holder", valid_from="2027-01-15"),
    ])
    assert _holders(rr.active_entries("bundesliga", "US", date(2027, 1, 14), tmp_path)) == ["Old Holder"]
    assert _holders(rr.active_entries("bundesliga", "US", date(2027, 1, 15), tmp_path)) == ["New Holder"]
    assert _holders(rr.active_entries("bundesliga", "US", date(2026, 8, 1), tmp_path)) == ["Old Holder"]


def test_entry_outside_its_range_is_inactive(tmp_path):
    _write(tmp_path, 2026, [_entry("Window", valid_from="2026-10-01", valid_until="2026-12-31")])
    assert rr.active_entries("bundesliga", "US", date(2026, 9, 30), tmp_path) == []
    assert _holders(rr.active_entries("bundesliga", "US", date(2026, 12, 31), tmp_path)) == ["Window"]
    assert rr.active_entries("bundesliga", "US", date(2027, 1, 1), tmp_path) == []


def test_rejected_entries_are_never_active(tmp_path):
    _write(tmp_path, 2026, [_entry("Nope", status="rejected"), _entry("Yes")])
    assert _holders(rr.active_entries("bundesliga", "US", date(2026, 11, 1), tmp_path)) == ["Yes"]


def test_missing_season_file_returns_empty_with_warning(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert rr.load_registry(2031, tmp_path) == []
        assert rr.active_entries("bundesliga", "US", date(2031, 9, 1), tmp_path) == []
    assert "no registry file for season 2031" in caplog.text


def test_malformed_entry_is_skipped_not_fatal(tmp_path, caplog):
    _write(tmp_path, 2026, [_entry("Good"), {"competition": "bundesliga"},
                            _entry("UK holder", country="UK")])
    with caplog.at_level(logging.WARNING):
        got = rr.load_registry(2026, tmp_path)
    assert [e["holder"] for e in got] == ["Good"]


def test_entry_source_ids_maps_channel_to_uploads():
    e = _entry("X", channels=[{"channel_id": "UC" + "a" * 22, "playlist_ids": ["PL" + "b" * 20]}])
    assert rr.entry_source_ids(e) == {"UU" + "a" * 22, "PL" + "b" * 20}
