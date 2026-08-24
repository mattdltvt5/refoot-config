"""Tests for the off-season channel-candidate finder (the ONE sanctioned
search.list job). No network / no key — search + channels calls are injected."""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

from find_channel_candidates import (
    score_candidate,
    rank_candidates,
    find_candidates,
    _effective_search_cap,
    _assert_not_incremental,
    SEARCH_UNIT_COST,
    MIN_PLAUSIBLE,
)


# ── Synthetic config: one AVAILABLE comp with a mapped + a missing team, plus an
#    UNAVAILABLE comp that must be skipped entirely. ───────────────────────────
def _sources():
    return {
        "teamLists": {
            "LeagueA": [{"name": "Real Example CF"}, {"name": "Newtown FC"}],
            "LeagueB": [{"name": "Some EL Club"}],   # comp not in `available` → skipped
        },
        "teams": {"Real Example CF": "UC_mapped"},   # mapped → NOT missing
        "teamPlaylists": {"LeagueA": {"Real Example CF": ""}},
        "competitions": {"LeagueA": "UC_leagueA_ch"},  # Tier-2 → covered
        "playlists": {},
    }

_AVAIL = {"LeagueA"}


# Canned search results keyed by team, and channel enrichment keyed by id.
def _fake_search(results_by_team):
    calls = {"n": 0, "teams": []}
    def searcher(team):
        calls["n"] += 1
        calls["teams"].append(team)
        return list(results_by_team.get(team, []))
    return searcher, calls

def _fake_enrich(meta):
    def enricher(ids):
        return {i: meta[i] for i in ids if i in meta}
    return enricher


# ── 1. Ranking: official (exact name + verified + high subs) beats a lookalike ──
def test_ranking_prefers_official_over_fan_lookalike():
    team = "Newtown FC"
    raw = [
        {"channelId": "UC_fan",  "channelTitle": "Newtown Fan Zone HD"},
        {"channelId": "UC_real", "channelTitle": "Newtown FC"},
    ]
    meta = {
        "UC_real": {"channelTitle": "Newtown FC", "subscriberCount": 900_000,
                    "verified": True, "thumbnail": "t"},
        "UC_fan":  {"channelTitle": "Newtown Fan Zone HD", "subscriberCount": 5_000,
                    "verified": False, "thumbnail": "t"},
    }
    for r in raw:
        r.update({k: meta[r["channelId"]][k]
                  for k in ("subscriberCount", "verified", "channelTitle")})
    ranked = rank_candidates(team, raw)
    assert ranked[0]["channelId"] == "UC_real"
    assert ranked[0]["score"] > ranked[1]["score"]
    # The fan channel's ambiguity is surfaced, not hidden.
    fan = next(c for c in ranked if c["channelId"] == "UC_fan")
    assert "fan" in fan["evidence"].lower()

def test_score_downranks_hard_fake_terms():
    good, _ = score_candidate("Newtown FC", {"channelTitle": "Newtown FC", "verified": True})
    fan, ev = score_candidate("Newtown FC", {"channelTitle": "Newtown FC Fan Page"})
    assert good > fan
    assert "fan" in ev.lower()


# ── 2. Hard search cap: aborts extra searches + records partial in flags[] ──────
def test_search_cap_enforced_partial_write():
    src = {
        "teamLists": {"LeagueA": [{"name": "Team One FC"}, {"name": "Team Two FC"},
                                  {"name": "Team Three FC"}]},
        "teams": {}, "teamPlaylists": {},
        "competitions": {"LeagueA": "UC_ch"}, "playlists": {},
    }
    results = {t: [{"channelId": f"UC_{t}", "channelTitle": t}]
               for t in ("Team One FC", "Team Two FC", "Team Three FC")}
    meta = {f"UC_{t}": {"channelTitle": t, "subscriberCount": 100_000,
                        "verified": True, "thumbnail": "t"} for t in results}
    searcher, calls = _fake_search(results)
    report = find_candidates(src, {"LeagueA"}, searcher=searcher,
                             enricher=_fake_enrich(meta), max_searches=2)
    assert calls["n"] == 2                               # never exceeded the cap
    assert report["estimated_search_units"] == 2 * SEARCH_UNIT_COST
    capped = [f for f in report["flags"]
              if f["reason"] == "search_cap_reached_not_searched"]
    assert len(capped) == 1                              # the 3rd team was not searched
    # And the two searched teams produced candidates (partial results preserved).
    assert sum(len(v) for v in report["candidates"].values()) == 2

def test_effective_cap_clamped_to_quota_fraction():
    # 50% of 10_000 units ÷ 100 units/search = 50 max, regardless of a huge request.
    assert _effective_search_cap(10_000) == 50
    assert _effective_search_cap(3) == 3


# ── 3. Schema + exact-key contract ──────────────────────────────────────────────
def test_output_schema_and_exact_team_keys():
    src = _sources()
    results = {"Newtown FC": [{"channelId": "UC_new", "channelTitle": "Newtown FC"}]}
    meta = {"UC_new": {"channelTitle": "Newtown FC", "subscriberCount": 500_000,
                       "verified": True, "thumbnail": "http://thumb"}}
    report = find_candidates(src, _AVAIL, searcher=_fake_search(results)[0],
                             enricher=_fake_enrich(meta))
    assert set(report) == {"generated_at", "estimated_search_units", "candidates", "flags"}
    # Keyed by competition → EXACT teamLists name.
    assert "Newtown FC" in report["candidates"]["LeagueA"]
    cand = report["candidates"]["LeagueA"]["Newtown FC"][0]
    assert set(cand) == {"channelId", "channelTitle", "url", "thumbnail",
                         "subscriberCount", "verified", "score", "evidence"}
    assert cand["url"] == "https://youtube.com/channel/UC_new"
    assert cand["subscriberCount"] == 500_000 and cand["verified"] is True


# ── 4. No plausible candidate → flags[] ─────────────────────────────────────────
def test_no_plausible_candidate_goes_to_flags():
    src = _sources()
    # Returned channel shares no club tokens and has no positive signals.
    results = {"Newtown FC": [{"channelId": "UC_x", "channelTitle": "Random Cooking Vlog"}]}
    meta = {"UC_x": {"channelTitle": "Random Cooking Vlog", "subscriberCount": 10,
                     "verified": False, "thumbnail": None}}
    report = find_candidates(src, _AVAIL, searcher=_fake_search(results)[0],
                             enricher=_fake_enrich(meta))
    assert report["candidates"] == {}
    flag = next(f for f in report["flags"] if f["team"] == "Newtown FC")
    assert flag["reason"] == "no_plausible_candidate"
    assert flag["best_score"] < MIN_PLAUSIBLE


# ── 5. Scope: only flagged available-comp missing teams are searched ────────────
def test_scope_excludes_mapped_and_unavailable_teams():
    src = _sources()
    results = {"Newtown FC": [{"channelId": "UC_new", "channelTitle": "Newtown FC"}]}
    meta = {"UC_new": {"channelTitle": "Newtown FC", "subscriberCount": 500_000,
                       "verified": True, "thumbnail": "t"}}
    searcher, calls = _fake_search(results)
    find_candidates(src, _AVAIL, searcher=searcher, enricher=_fake_enrich(meta))
    # Real Example CF is mapped (excluded); Some EL Club is in an unavailable comp
    # (excluded). Only the genuinely-missing available-comp team is searched.
    assert calls["teams"] == ["Newtown FC"]


# ── Guardrail: must never be importable by the incremental pipeline ─────────────
def test_assert_not_incremental_trips_on_pipeline_import():
    _assert_not_incremental()                # clean when the pipeline isn't loaded
    sys.modules["fetch_highlights"] = object()
    try:
        with pytest.raises(RuntimeError):
            _assert_not_incremental()
    finally:
        del sys.modules["fetch_highlights"]
