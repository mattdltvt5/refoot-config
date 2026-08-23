#!/usr/bin/env python3
"""One-time backfill of Copa America per-match goal & card events.

Copa America 2024 is a completed tournament, so its events are static: this fetches
them once via API-Sports /fixtures/events (free tier covers seasons 2022-2024;
~32 fixtures ~= ~33 calls incl. the fixtures list, well under the 100/day cap) and
writes events/copa-america/{season}.json, keyed by match_id. The Flutter
match-detail view reads that file by match_id.

Never scheduled (quota discipline) -- run via workflow_dispatch. football-data is
NOT used here; API-Sports only, exactly as the rest of the Copa pipeline.
"""
import os
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fixture_providers import (
    APISPORTS_COMPETITIONS,
    ApiSportsProvider,
    ApisportsQuotaTracker,
)
from highlights_common import write_json_atomic

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("copa-events")

COMP_NAME = "Copa America"
SLUG = "copa-america"


def build_events(provider, cfg):
    """Return (events_by_match_id: dict[str, list], total_fixtures: int).

    events_by_match_id maps str(match_id) -> normalized event list; matches with no
    goal/card events are omitted. Returns (None, 0) when no fixtures are available.
    """
    by_stem = provider.get_fixtures(COMP_NAME, cfg)
    if not by_stem:
        return None, 0
    match_ids = [
        fx["match_id"]
        for fixtures in by_stem.values()
        for fx in fixtures
        if fx.get("match_id") is not None
    ]
    events = {}
    for mid in match_ids:
        evs = provider.get_events(mid)
        if evs:
            events[str(mid)] = evs
    return events, len(match_ids)


def main():
    if not os.environ.get("APISPORTS_API_KEY", ""):
        print("ERROR: APISPORTS_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    cfg = APISPORTS_COMPETITIONS[COMP_NAME]
    season = cfg["season"]
    provider = ApiSportsProvider(quota_tracker=ApisportsQuotaTracker())
    events, total = build_events(provider, cfg)
    if events is None:
        print("API-Sports returned no fixtures -- aborting", file=sys.stderr)
        sys.exit(1)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "competition":  COMP_NAME,
        "slug":         SLUG,
        "season":       season,
        "events":       events,
    }
    path = Path("events") / SLUG / f"{season}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, out)
    print(f"Wrote {path}: events for {len(events)}/{total} matches")


if __name__ == "__main__":
    main()
