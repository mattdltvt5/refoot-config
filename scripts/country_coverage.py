"""Country coverage report: for each country in countries.json, how many matches
in each competition's latest season have at least one highlight clip that plays
there (from the cached per-video `region`, see video_regions.py).

Read-only toward the cache; no API calls. Writes reports/country-coverage.json,
but only when the numbers change (generated_at alone never triggers a write).
The Web Admin's Countries tab shows it next to each country's status and its
rights-registry entries; countries.json's statuses decide which countries the
app offers (status "live").
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from highlights_common import (
    COMPETITION_SLUG_MAP,
    HIGHLIGHTS_DIR,
    REPO_ROOT,
    load_json_file,
    region_is_playable_in,
    write_json_atomic,
)

log = logging.getLogger(__name__)

COUNTRIES_JSON = REPO_ROOT / "countries.json"
REPORT_PATH = REPO_ROOT / "reports" / "country-coverage.json"
STATUSES = ("live", "testing", "hidden")


def load_countries(path: Path = COUNTRIES_JSON) -> list[dict]:
    """countries.json's list: [{code, name, status}], codes upper-case."""
    data = load_json_file(path) or {}
    out = []
    for c in data.get("countries") or []:
        code = str(c.get("code", "")).upper()
        if len(code) == 2 and c.get("status") in STATUSES:
            out.append({"code": code, "name": c.get("name") or code, "status": c["status"]})
    return out


def _latest_season_dir(slug_dir: Path) -> "Path | None":
    seasons = [p for p in slug_dir.iterdir() if p.is_dir() and p.name.isdigit()] if slug_dir.is_dir() else []
    return max(seasons, key=lambda p: int(p.name)) if seasons else None


def build(countries: list[str], root: Path = HIGHLIGHTS_DIR) -> dict:
    """The report body (without generated_at)."""
    comps: dict[str, dict] = {}
    for comp_name, slug in COMPETITION_SLUG_MAP.items():
        season_dir = _latest_season_dir(root / slug)
        if season_dir is None or slug in comps:
            continue
        matches = 0
        playable = {cc: 0 for cc in countries}
        for path in sorted(season_dir.glob("*.json")):
            data = load_json_file(path)
            if not isinstance(data, dict):
                continue
            for m in data.get("matches", []):
                vids = m.get("videos") or []
                if not vids:
                    continue
                matches += 1
                for cc in countries:
                    if any(region_is_playable_in(v.get("region") or None, cc) for v in vids):
                        playable[cc] += 1
        if matches:
            comps[slug] = {"competition": comp_name, "season": int(season_dir.name),
                           "matches": matches, "playable": playable}
    total = sum(c["matches"] for c in comps.values())
    totals = {cc: sum(c["playable"][cc] for c in comps.values()) for cc in countries}
    return {"countries": countries, "matches": total, "playable": totals, "competitions": comps}


def write_report(countries_path: Path = COUNTRIES_JSON, root: Path = HIGHLIGHTS_DIR,
                 report_path: Path = REPORT_PATH, now: "datetime | None" = None) -> bool:
    """Rebuild the report; write it only when the numbers changed. True if written."""
    codes = [c["code"] for c in load_countries(countries_path)]
    if not codes:
        return False
    body = build(codes, root)
    old = load_json_file(report_path) or {}
    if {k: v for k, v in old.items() if k != "generated_at"} == body:
        return False
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(report_path, {"generated_at": stamp, **body})
    log.info(f"Country coverage: wrote {report_path.name} ({body['matches']} matches, {len(codes)} countries)")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    write_report()
