"""
source_health_scan.py — no network (fake YouTube client, fake quota tracker).

Covers every flag (EMPTY, STALE with/without recent fixtures, UNREACHABLE,
TITLE_DRIFT, LOW_COVERAGE, MISSING_SOURCE, OUTDATED_SOURCE, UNVERIFIED,
NO_HOLDER), region counts per launch country, the budget stop (partial: true),
proposals never touching sources.json, report output + retention.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import source_health_scan as scan
from highlights_common import LAUNCH_COUNTRIES, REPO_ROOT, SOURCES_JSON

NOW = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)
CH = "UC" + "B" * 22            # competition channel
CH_UU = "UU" + "B" * 22
CLUB = "UC" + "F" * 22          # club channel
CLUB_UU = "UU" + "F" * 22
PL_B = "PL" + "b" * 30          # broadcaster playlist
PL_T = "PL" + "t" * 30          # team playlist
PL_P = "PL" + "p" * 30          # approved registry playlist NOT in sources.json

HL = "WHAT A GAME! | SC FREIBURG - FC AUGSBURG | Highlights | Matchday 11 – Bundesliga 2026/27"
REPLAY = "SC Freiburg vs. FC Augsburg | Matchday 11 - Bundesliga 2026/27"


class FakeTracker:
    def __init__(self, units_used=0):
        self.units_used = units_used

    def increment(self, cap):
        self.units_used += 1


class FakeClient:
    def __init__(self, pages=None, details=None):
        self.pages = pages or {}
        self.details = details or {}
        self.page_calls = []
        self.detail_calls = []

    def playlist_page(self, pid):
        self.page_calls.append(pid)
        return self.pages.get(pid, page([]))

    def video_details(self, ids, quota, cap):
        self.detail_calls.append(list(ids))
        quota.increment(cap)
        return {i: self.details[i] for i in ids if i in self.details}


def page(items, status="ok", has_more=False):
    http = {"ok": 200, "not_found": 404, "error": 500}[status]
    return {"status": status, "http": http, "items": items, "has_more": has_more}


def item(vid, title, published="2026-11-20"):
    return {"video_id": vid, "title": title, "published": published}


SOURCES_RAW = {"competitions": {"Bundesliga": CH},
               "teamLists": {"Bundesliga": [{"name": "SC Freiburg"}]}}
CONFIG = {
    "competition_channels":  {"Bundesliga": CH},
    "competition_playlists": {"Bundesliga": {"Bundesliga TV": [PL_B]}},
    "team_playlists":        {"Bundesliga": {"1. FC Union Berlin": PL_T}},
    "team_channels":         {"SC Freiburg": CLUB},
}

RECENT_FIXTURE = {"match_id": 1, "status": "FINISHED", "utcDate": "2026-11-20T19:30:00Z"}
OLD_FIXTURE = {"match_id": 2, "status": "FINISHED", "utcDate": "2026-10-01T19:30:00Z"}


@pytest.fixture
def rights(tmp_path):
    d = tmp_path / "rights"
    d.mkdir()
    return d


def write_rights(d, season, entries):
    (d / f"{season}.json").write_text(json.dumps({"season": season, "entries": entries}),
                                      encoding="utf-8")


def entry(holder, country="US", status="unverified", channels=None, **kw):
    return {"competition": "bundesliga", "country": country, "holder": holder, "role": "live",
            "channels": channels or [], "status": status, **kw}


@pytest.fixture
def fixtures(monkeypatch):
    """Control the cached fixture data and highlight cache the scan reads."""
    state = {"fixtures": [RECENT_FIXTURE], "have": {1}}
    monkeypatch.setattr(scan, "load_fixture_records", lambda comp, slug, season: state["fixtures"])
    monkeypatch.setattr(scan, "highlight_match_ids", lambda slug, season: state["have"])
    return state


def run(client, rights_dir, budget=None, tracker=None, **kw):
    quota = scan.ScanQuota(tracker or FakeTracker(),
                           scan.SCAN_BUDGET_UNITS if budget is None else budget)
    return scan.run_scan(client=client, quota=quota, now=NOW, sources_raw=SOURCES_RAW,
                         config=CONFIG, rights_dir=rights_dir, **kw)


def src(report, pid):
    return next(s for s in report["competitions"]["Bundesliga"]["sources"] if s["playlist_id"] == pid)


def healthy_pages():
    return {
        CH_UU: page([item("hl1", HL)]),
        PL_B: page([item("b1", HL)]),
        PL_T: page([item("t1", HL)]),
        CLUB_UU: page([item("c1", HL)]),
    }


# ── Source flags ──────────────────────────────────────────────────────────────

def test_healthy_sources_have_no_flags(rights, fixtures):
    r = run(FakeClient(healthy_pages()), rights)
    for s in r["competitions"]["Bundesliga"]["sources"]:
        assert s["flags"] == [], s
        assert s["checked"] is True
    assert r["partial"] is False
    assert r["quota_used"] == 4 + 1          # 4 pages + 1 videos.list batch


def test_empty_flag(rights, fixtures):
    pages = healthy_pages()
    pages[PL_T] = page([])
    r = run(FakeClient(pages), rights)
    assert src(r, PL_T)["flags"] == ["EMPTY"]
    assert r["summary"]["EMPTY"] == 1


def test_unreachable_flag(rights, fixtures):
    pages = healthy_pages()
    pages[PL_B] = page([], status="not_found")
    r = run(FakeClient(pages), rights)
    assert src(r, PL_B)["flags"] == ["UNREACHABLE"]
    assert src(r, PL_B)["http_status"] == 404


def test_stale_flag_when_competition_had_recent_fixtures(rights, fixtures):
    pages = healthy_pages()
    pages[CH_UU] = page([item("old", HL, published="2026-10-01")])
    r = run(FakeClient(pages), rights)
    assert "STALE" in src(r, CH_UU)["flags"]


def test_stale_suppressed_without_recent_fixtures(rights, fixtures):
    fixtures["fixtures"] = [OLD_FIXTURE]          # international break / off-season
    pages = healthy_pages()
    pages[CH_UU] = page([item("old", HL, published="2026-10-01")])
    r = run(FakeClient(pages), rights)
    s = src(r, CH_UU)
    assert "STALE" not in s["flags"]
    assert s["stale_suppressed"] == "no_finished_fixtures_in_window"


def test_stale_suppressed_without_any_fixture_data(rights, fixtures):
    fixtures["fixtures"] = None
    pages = healthy_pages()
    pages[CH_UU] = page([item("old", HL, published="2026-10-01")])
    r = run(FakeClient(pages), rights)
    assert "STALE" not in src(r, CH_UU)["flags"]
    assert src(r, CH_UU)["stale_suppressed"] == "no_fixture_data"


def test_stale_not_judged_on_long_unordered_playlist_first_page(rights, fixtures):
    pages = healthy_pages()
    pages[PL_B] = page([item("old", HL, published="2025-09-01")], has_more=True)
    r = run(FakeClient(pages), rights)
    assert "STALE" not in src(r, PL_B)["flags"]
    assert src(r, PL_B)["stale_suppressed"] == "first_page_not_newest_first"


def test_title_drift_on_competition_channel(rights, fixtures):
    pages = healthy_pages()
    pages[CH_UU] = page([item(f"r{i}", REPLAY) for i in range(3)])
    r = run(FakeClient(pages), rights)
    s = src(r, CH_UU)
    assert s["flags"] == ["TITLE_DRIFT"]
    assert s["recent_uploads"] == 3 and s["recent_passing"] == 0
    assert s["drift_samples"][0] == {"title": REPLAY, "reason": "missing-required-term"}


def test_title_gate_is_source_scoped(rights, fixtures):
    # The same replay titles in a curated broadcaster playlist pass (the matcher
    # skips the allowlist there and has no required terms) → no drift.
    pages = healthy_pages()
    pages[PL_B] = page([item(f"r{i}", REPLAY) for i in range(3)])
    r = run(FakeClient(pages), rights)
    assert src(r, PL_B)["flags"] == []
    assert src(r, PL_B)["recent_passing"] == 3


def test_live_replay_titles_count_as_drift(rights, fixtures):
    pages = healthy_pages()
    pages[CH_UU] = page([item(f"l{i}", "LIVE 🔴 " + REPLAY) for i in range(3)])
    r = run(FakeClient(pages), rights)
    assert src(r, CH_UU)["drift_samples"][0]["reason"].startswith("blocked:")


def test_too_few_recent_uploads_is_not_drift(rights, fixtures):
    pages = healthy_pages()
    pages[CH_UU] = page([item("r1", REPLAY), item("r2", REPLAY)])
    r = run(FakeClient(pages), rights)
    assert "TITLE_DRIFT" not in src(r, CH_UU)["flags"]


# ── Coverage ──────────────────────────────────────────────────────────────────

def test_low_coverage_flag(rights, fixtures):
    fixtures["fixtures"] = [
        {"match_id": 10, "status": "FINISHED", "utcDate": "2026-11-15T14:30:00Z"},
        {"match_id": 11, "status": "FINISHED", "utcDate": "2026-11-15T14:30:00Z"},
        {"match_id": 12, "status": "FINISHED", "utcDate": "2026-11-16T14:30:00Z"},
        {"match_id": 13, "status": "FINISHED", "utcDate": "2026-11-23T03:00:00Z"},  # in grace
        {"match_id": 14, "status": "SCHEDULED", "utcDate": "2026-11-28T14:30:00Z"},
    ]
    fixtures["have"] = {10}
    cov = run(FakeClient(healthy_pages()), rights)["competitions"]["Bundesliga"]["coverage"]
    assert cov["finished"] == 4 and cov["awaiting"] == 1 and cov["eligible"] == 3
    assert cov["with_highlight"] == 1 and cov["ratio"] == pytest.approx(0.333)
    assert cov["flags"] == ["LOW_COVERAGE"]
    assert cov["missing_match_ids"] == [11, 12]


def test_coverage_ok(rights, fixtures):
    cov = run(FakeClient(healthy_pages()), rights)["competitions"]["Bundesliga"]["coverage"]
    assert cov["ratio"] == 1.0 and cov["flags"] == []


# ── Region ────────────────────────────────────────────────────────────────────

def test_region_counts_per_launch_country(rights, fixtures):
    pages = healthy_pages()
    pages[CH_UU] = page([item("g", HL), item("us", HL), item("de", HL), item("none", HL),
                         item("long", HL)])
    details = {
        "g":    {"duration_seconds": 600, "is_embeddable": True, "region_restriction": None},
        "us":   {"duration_seconds": 600, "is_embeddable": False,
                 "region_restriction": {"allowed": ["US"]}},
        "de":   {"duration_seconds": 600, "is_embeddable": True,
                 "region_restriction": {"blocked": ["AT", "CH", "DE", "LI", "LU"]}},
        "none": {"duration_seconds": 600, "is_embeddable": True,
                 "region_restriction": {"allowed": []}},
        "long": {"duration_seconds": 5400, "is_embeddable": True, "region_restriction": None},
    }
    r = run(FakeClient(pages, details), rights)
    reg = src(r, CH_UU)["region"]
    assert reg["checked"] == 5 and reg["embeddable"] == 4 and reg["over_max_duration"] == 1
    assert reg["playable"] == {"US": 4, "GB": 3, "CA": 3, "IE": 3, "ES": 3}
    comp = r["competitions"]["Bundesliga"]["region"]
    assert set(comp) == set(LAUNCH_COUNTRIES)
    assert comp["US"]["playable"] >= 4


def test_region_ids_batched_50_per_call(rights, fixtures):
    pages = healthy_pages()
    pages[CH_UU] = page([item(f"v{i}", HL) for i in range(40)])
    pages[PL_B] = page([item(f"b{i}", HL) for i in range(40)])
    pages[PL_T] = page([item(f"t{i}", HL) for i in range(40)])
    pages[CLUB_UU] = page([item(f"c{i}", HL) for i in range(40)])
    client = FakeClient(pages)
    run(client, rights)
    # 10 sampled per source × 4 sources = 40 IDs → one batch
    assert [len(b) for b in client.detail_calls] == [40]


# ── Registry findings ─────────────────────────────────────────────────────────

def findings(report, flag):
    return [f for f in report["competitions"]["Bundesliga"]["registry_findings"] if f["flag"] == flag]


def test_unverified_and_no_holder(rights, fixtures):
    write_rights(rights, 2026, [
        entry("USA Sports"), entry("Telemundo"),
        entry("Sky Sports", country="GB", status="approved"),
        entry(None, country="IE", notes="To confirm: holder not yet researched."),
    ])
    r = run(FakeClient(healthy_pages()), rights)
    unv = findings(r, "UNVERIFIED")
    assert [(f["country"], sorted(h["holder"] for h in f["holders"])) for f in unv] == [
        ("US", ["Telemundo", "USA Sports"])]
    no_holder = {f["country"]: f["note"] for f in findings(r, "NO_HOLDER")}
    assert set(no_holder) == {"CA", "IE", "ES"}
    assert no_holder["IE"].startswith("To confirm")


def test_competition_not_in_registry_gets_no_findings(rights, fixtures):
    write_rights(rights, 2026, [entry("X", country="US") | {"competition": "laliga"}])
    r = run(FakeClient(healthy_pages()), rights)
    assert r["competitions"]["Bundesliga"]["registry_findings"] == []


def test_missing_source_for_approved_channel(rights, fixtures):
    write_rights(rights, 2026, [entry("Sky Sports", country="GB", status="approved",
                                      channels=[{"channel_id": "", "playlist_ids": [PL_P, PL_B]}])])
    r = run(FakeClient(healthy_pages()), rights)
    miss = findings(r, "MISSING_SOURCE")
    assert [(f["country"], f["holder"], f["source_id"]) for f in miss] == [("GB", "Sky Sports", PL_P)]


def test_unverified_channels_never_raise_missing_source(rights, fixtures):
    write_rights(rights, 2026, [entry("Sky Sports", country="GB",
                                      channels=[{"channel_id": "", "playlist_ids": [PL_P]}])])
    r = run(FakeClient(healthy_pages()), rights)
    assert findings(r, "MISSING_SOURCE") == []


def test_outdated_source_linked_to_inactive_holder(rights, fixtures):
    # PL_B belonged to last season's holder; this season's holder lists nothing.
    write_rights(rights, 2025, [entry("ESPN", status="approved",
                                      channels=[{"channel_id": "", "playlist_ids": [PL_B]}])])
    write_rights(rights, 2026, [entry("USA Sports")])
    r = run(FakeClient(healthy_pages()), rights)
    out = findings(r, "OUTDATED_SOURCE")
    assert [(f["source_id"], f["label"], f["linked_holders"]) for f in out] == [
        (PL_B, "Bundesliga TV", ["ESPN"])]


def test_outdated_source_after_mid_season_valid_until(rights, fixtures):
    write_rights(rights, 2026, [
        entry("Old", status="approved", valid_until="2026-11-01",
              channels=[{"channel_id": "", "playlist_ids": [PL_B]}]),
        entry("New", valid_from="2026-11-02"),
    ])
    assert [f["source_id"] for f in findings(run(FakeClient(healthy_pages()), rights),
                                             "OUTDATED_SOURCE")] == [PL_B]


def test_source_still_listed_by_active_holder_is_not_outdated(rights, fixtures):
    write_rights(rights, 2025, [entry("ESPN", status="approved",
                                      channels=[{"channel_id": "", "playlist_ids": [PL_B]}])])
    write_rights(rights, 2026, [entry("Sky", status="approved",
                                      channels=[{"channel_id": "", "playlist_ids": [PL_B]}])])
    assert findings(run(FakeClient(healthy_pages()), rights), "OUTDATED_SOURCE") == []


# ── Proposals ─────────────────────────────────────────────────────────────────

def test_proposals_from_approved_unconfigured_source(rights, fixtures):
    write_rights(rights, 2026, [entry("Sky Sports", country="GB", status="approved",
                                      channels=[{"channel_id": "", "playlist_ids": [PL_P]}])])
    pages = healthy_pages()
    pages[PL_P] = page([item("p1", HL), item("p2", "Press conference | SC Freiburg - FC Augsburg"),
                        item("p3", HL, published="2026-09-01")])
    r = run(FakeClient(pages), rights)
    props = r["competitions"]["Bundesliga"]["proposals"]
    assert len(props) == 1
    p = props[0]
    assert (p["source_id"], p["holder"], p["country"], p["recent_passing"]) == (PL_P, "Sky Sports", "GB", 1)
    assert p["samples"][0]["video_id"] == "p1"
    assert r["summary"]["proposals"] == 1


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_scan_never_modifies_sources_cache_or_registry(rights, tmp_path, fixtures):
    """Full run against the REAL sources.json (empty pages everywhere): sources.json,
    the registry seeds and the highlights cache are byte-identical afterwards."""
    write_rights(rights, 2026, [entry("Sky Sports", country="GB", status="approved",
                                      channels=[{"channel_id": "", "playlist_ids": [PL_P]}])])
    watched = [Path(SOURCES_JSON), *sorted((REPO_ROOT / "rights").glob("*.json")),
               *sorted((REPO_ROOT / "highlights").glob("*.json"))]
    before = {p: _sha(p) for p in watched}
    quota = scan.ScanQuota(FakeTracker(), 10_000)
    report = scan.run_scan(client=FakeClient(), quota=quota, now=NOW, rights_dir=rights)
    scan.write_report(report, tmp_path / "out")
    assert {p: _sha(p) for p in watched} == before
    assert report["competitions"]["Bundesliga"]["registry_findings"]


# ── Budget ────────────────────────────────────────────────────────────────────

def test_default_budget_is_500_and_configurable():
    assert scan.SCAN_BUDGET_UNITS == 500
    src_text = Path(scan.__file__).read_text(encoding="utf-8")
    assert 'ap.add_argument("--budget", type=int, default=SCAN_BUDGET_UNITS)' in src_text


class FullPageClient(FakeClient):
    """Every playlist returns a FULL first page (50 items, more pages exist) of
    recent titles that pass every source's title gate — so every source sends
    the maximum region sample to videos.list. Worst case for quota."""

    TITLE = "Match Highlights LaLiga"   # passes tier-2 (incl. LaLiga gate / BL
                                        # required term), curated and club gates

    def playlist_page(self, pid):
        self.page_calls.append(pid)
        return page([item(f"{pid}-{i}", self.TITLE) for i in range(50)], has_more=True)

    def video_details(self, ids, quota, cap):
        self.detail_calls.append(list(ids))
        quota.increment(cap)
        return {i: {"duration_seconds": 600, "is_embeddable": True,
                    "region_restriction": None} for i in ids}


def test_full_pass_with_full_pages_completes_under_default_budget(tmp_path):
    """Real sources.json + config, every configured source a full first page,
    all region batches: the default budget finishes the pass (partial: false)."""
    client = FullPageClient()
    quota = scan.ScanQuota(FakeTracker(), scan.SCAN_BUDGET_UNITS)
    r = scan.run_scan(client=client, quota=quota, now=NOW, rights_dir=tmp_path)

    assert r["partial"] is False and r["skipped"] == [] and r["stop_reason"] is None
    pages = len(set(client.page_calls))
    batches = len(client.detail_calls)
    assert r["quota_used"] == pages + batches <= scan.SCAN_BUDGET_UNITS
    for comp in r["competitions"].values():
        for s in comp["sources"]:
            assert s["checked"] and s["recent_passing"] == 50, (comp["slug"], s["label"])
            assert s["region"]["checked"] == scan.REGION_SAMPLE_PER_SOURCE
    # Headroom left for growth (new sources, proposal fetches, error pages).
    assert scan.SCAN_BUDGET_UNITS - r["quota_used"] >= 100


def test_budget_stop_marks_partial_and_lists_skipped(rights, fixtures):
    client = FakeClient(healthy_pages())
    r = run(client, rights, budget=2)
    assert r["partial"] is True and r["stop_reason"] == "scan_budget"
    assert r["quota_used"] == 2 and len(client.page_calls) == 2
    skipped = {s.get("playlist_id") for s in r["skipped"] if "playlist_id" in s}
    assert skipped == {PL_T, CLUB_UU}
    assert any("videos_list_batch" in s for s in r["skipped"])
    unchecked = [s for s in r["competitions"]["Bundesliga"]["sources"] if not s["checked"]]
    assert len(unchecked) == 2


def test_daily_quota_ceiling_stops_scan(rights, fixtures):
    tracker = FakeTracker(units_used=scan.DAILY_CEILING - 2)
    client = FakeClient(healthy_pages())
    r = run(client, rights, tracker=tracker)
    assert r["partial"] is True and r["stop_reason"] == "daily_quota"
    assert r["quota_used"] == 1 and tracker.units_used == scan.DAILY_CEILING - 1


def test_quota_used_recorded_in_shared_tracker(rights, fixtures):
    tracker = FakeTracker(units_used=100)
    r = run(FakeClient(healthy_pages()), rights, tracker=tracker)
    assert tracker.units_used == 100 + r["quota_used"]


def test_no_api_key_is_partial_not_fatal(rights, fixtures):
    r = run(None, rights)
    assert r["partial"] is True and r["quota_used"] == 0
    assert {s["reason"] for s in r["skipped"]} == {"no_api_key"}


# ── Report output ─────────────────────────────────────────────────────────────

def test_report_structure(rights, fixtures):
    r = run(FakeClient(healthy_pages()), rights)
    assert set(r) >= {"generated_at", "report_season", "partial", "quota_used", "quota_budget",
                      "summary", "skipped", "competitions", "launch_countries"}
    assert set(scan.FLAGS) <= set(r["summary"])
    c = r["competitions"]["Bundesliga"]
    assert set(c) >= {"slug", "season", "sources", "coverage", "region", "registry_findings",
                      "proposals"}
    assert c["season"] == 2026 and r["generated_at"].startswith("2026-11-23T06:00")


def test_write_report_latest_dated_and_keeps_last_8(rights, fixtures, tmp_path):
    out = tmp_path / "reports"
    season_dir = out / "2026"
    season_dir.mkdir(parents=True)
    for d in range(1, 11):
        (season_dir / f"2026-10-{d:02d}.json").write_text("{}", encoding="utf-8")
    (season_dir / "notes.txt").write_text("keep me", encoding="utf-8")
    paths = scan.write_report(run(FakeClient(healthy_pages()), rights), out)
    assert [p.name for p in paths] == ["latest.json", "2026-11-23.json"]
    dated = sorted(p.name for p in season_dir.glob("2026-*.json"))
    assert len(dated) == 8 and dated[-1] == "2026-11-23.json" and dated[0] == "2026-10-04.json"
    assert (season_dir / "notes.txt").exists()
    assert json.loads((season_dir / "latest.json").read_text(encoding="utf-8"))["report_season"] == 2026


def test_scan_uses_only_allowed_youtube_endpoints():
    text = Path(scan.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "youtube/v3/search" not in code and "youtube/v3/channels" not in code
    assert "YT_PLAYLISTS" not in code and "api-sports.io" not in code.lower()
    assert "fixture_providers" not in code      # the API-Sports client module
    assert scan.YT_PLAYLIST.endswith("/playlistItems")
