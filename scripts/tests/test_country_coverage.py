"""countries.json + reports/country-coverage.json (Web Admin Countries tab;
the app offers only status "live")."""
import json
from datetime import datetime, timezone
from pathlib import Path

import country_coverage as cc
from highlights_common import LAUNCH_COUNTRIES

ROOT = Path(cc.__file__).resolve().parents[1]


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def test_repo_countries_json_is_valid():
    raw = json.loads((ROOT / "countries.json").read_text(encoding="utf-8"))
    codes = [c["code"] for c in raw["countries"]]
    assert len(codes) == len(set(codes))
    assert "UK" not in codes
    for c in raw["countries"]:
        assert len(c["code"]) == 2 and c["code"].isupper() and c["name"]
        assert c["status"] in cc.STATUSES, c
    live = {c["code"] for c in raw["countries"] if c["status"] == "live"}
    assert {"US", "GB", "IE"} <= live


def test_launch_countries_are_the_live_and_testing_ones():
    """The alternatives pass and the rights registry work on LAUNCH_COUNTRIES;
    keep it equal to what countries.json says is live or being tested."""
    worked_on = {c["code"] for c in cc.load_countries() if c["status"] in ("live", "testing")}
    assert worked_on == set(LAUNCH_COUNTRIES)


def test_load_countries_drops_bad_entries(tmp_path):
    p = tmp_path / "countries.json"
    _write(p, {"countries": [{"code": "us", "name": "United States", "status": "live"},
                             {"code": "XYZ", "status": "live"}, {"code": "FR", "status": "maybe"},
                             {"code": "DE", "status": "hidden"}]})
    assert [(c["code"], c["status"]) for c in cc.load_countries(p)] == [("US", "live"), ("DE", "hidden")]


def _cache(tmp_path):
    hl = tmp_path / "highlights"
    _write(hl / "serie-a" / "2025" / "gameweek-1.json", {"matches": [
        {"match_id": 9, "videos": [{"video_id": "old", "region": {"allowed": []}}]}]})   # older season: ignored
    _write(hl / "serie-a" / "2026" / "gameweek-1.json", {"matches": [
        {"match_id": 1, "videos": [{"video_id": "a", "region": {"allowed": ["US"]}}]},
        {"match_id": 2, "videos": [{"video_id": "b", "region": {"allowed": ["US"]}},
                                   {"video_id": "c", "region": {"blocked": ["IT"]}}]},
        {"match_id": 3, "videos": [{"video_id": "d"}]},                                   # no region: everywhere
        {"match_id": 4, "videos": []},                                                    # no highlight: not counted
    ]})
    return hl


def test_build_counts_matches_with_a_playable_clip(tmp_path):
    body = cc.build(["US", "GB", "IT"], _cache(tmp_path))
    sa = body["competitions"]["serie-a"]
    assert (sa["season"], sa["matches"]) == (2026, 3)
    assert sa["playable"] == {"US": 3, "GB": 2, "IT": 1}
    assert body["matches"] == 3 and body["playable"] == {"US": 3, "GB": 2, "IT": 1}


def test_write_report_only_when_numbers_change(tmp_path):
    hl = _cache(tmp_path)
    countries = tmp_path / "countries.json"
    _write(countries, {"countries": [{"code": "US", "name": "US", "status": "live"},
                                     {"code": "GB", "name": "GB", "status": "live"}]})
    report = tmp_path / "reports" / "country-coverage.json"
    t1 = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    assert cc.write_report(countries, hl, report, t1) is True
    first = json.loads(report.read_text(encoding="utf-8"))
    assert first["generated_at"] == "2026-11-23T12:00:00Z"
    assert cc.write_report(countries, hl, report, datetime(2026, 11, 23, 13, tzinfo=timezone.utc)) is False
    assert json.loads(report.read_text(encoding="utf-8"))["generated_at"] == "2026-11-23T12:00:00Z"
    _write(hl / "serie-a" / "2026" / "gameweek-2.json", {"matches": [{"match_id": 5, "videos": [{"video_id": "e"}]}]})
    assert cc.write_report(countries, hl, report, t1) is True


def test_fetch_run_writes_the_report_and_workflow_commits_it():
    src = (ROOT / "scripts" / "fetch_highlights.py").read_text(encoding="utf-8")
    assert "write_country_coverage()" in src
    wf = (ROOT / ".github" / "workflows" / "fetch-highlights.yml").read_text(encoding="utf-8")
    assert "reports/country-coverage.json" in wf


def test_admin_countries_tab_wired():
    admin = (ROOT / "admin.html").read_text(encoding="utf-8")
    assert 'id="tab-countries"' in admin and 'id="panel-countries"' in admin
    assert "const COUNTRIES_FILE        = 'countries.json';" in admin
    assert "loadCountries();" in admin
    assert "withSpin(e.currentTarget, loadCountries)" in admin
    assert "ghWriteJson(COUNTRIES_FILE" in admin
