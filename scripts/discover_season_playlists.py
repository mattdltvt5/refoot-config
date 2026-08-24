#!/usr/bin/env python3
"""
discover_season_playlists.py — resolve current-season highlights playlists.

For each AVAILABLE competition's already-known source channels (Tier-4 broadcaster
playlists + Tier-1c/1d team playlists in sources.json), resolve the OWNING channel
of the currently-mapped playlist, list that channel's playlists, and pick the
current-season highlights playlist by name (playlist_discovery.select_current_
season_playlist). Confident matches are written to highlights/discovered-
playlists.json, which the fetch path then uses in place of the hardcoded rotating
IDs. On NO confident match the mapped last-known-good ID is kept and a loud,
machine-readable FLAG is recorded for human review — never a silent switch.

Scoped strictly to AVAILABLE competitions (playlist_discovery.available_
competitions): Europa League and Copa América are skipped today (no fixtures to
attach highlights to) and auto-include if that changes.

READ path: playlists.list only (owner resolve + channel listing). NEVER
search.list. Quota is trivial (a few dozen–low-hundreds units), counted and
printed. WRITE: only discovered-playlists.json (committed with [skip ci] by the
workflow).
"""

import json
import logging
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlights_common import (
    SOURCES_JSON,
    fetch_playlist_owner,
    load_json_file,
    season_for_competition,
    utc_now_iso,
)
from season_utils import current_season
from playlist_discovery import (
    DISCOVERED_PATH,
    available_competitions,
    compute_missing_channels,
    list_channel_playlists,
    merge_discovered_seasons,
    migrate_flat_discovered,
    season_leaf,
    select_current_season_playlist,
    write_discovered_if_changed,
    TEAM_MATCHER_VERSION,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("discover_season_playlists")

# Safety ceiling so a pathological channel list can never blow the daily budget.
_UNIT_CEILING = 600


def _log_missing_channels(missing: "list[dict]") -> None:
    """Loud, structured per-competition summary of roster teams with no own channel.
    Always logs (even 0 missing) so the signal is current every run. The ACTIONABLE
    subset (no own channel AND no working competition tier) is warned separately."""
    if not missing:
        log.info("Missing-channel detector: 0 teams without an own channel. ✓")
        return
    by_comp: "dict[str, list[dict]]" = {}
    for m in missing:
        by_comp.setdefault(m["competition"], []).append(m)
    for comp in sorted(by_comp):
        teams = [m["team"] for m in by_comp[comp]]
        log.info("Missing own channel — %s (%d): %s", comp, len(teams),
                 ", ".join(sorted(teams)))
    actionable = [m for m in missing if not m["covered_via_other_tier"]]
    if actionable:
        log.warning("ACTIONABLE missing channels (no own channel AND no working "
                    "competition tier) — %d: %s", len(actionable),
                    json.dumps(actionable, ensure_ascii=False))
    else:
        log.info("All missing-own-channel teams are covered via a competition/"
                 "broadcaster tier (none actionable).")


def main() -> None:
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        log.error("YOUTUBE_API_KEY is not set — cannot run discovery")
        sys.exit(1)

    raw = json.load(open(SOURCES_JSON, encoding="utf-8"))
    playlists_cfg = raw.get("playlists", {})       # Tier-4: comp -> {broadcaster -> [PL...]}
    team_pls      = raw.get("teamPlaylists", {})    # Tier-1c/d: comp -> {team -> PL}

    avail = available_competitions()
    log.info("Available competitions (discovery scope): %s", sorted(avail))
    log.info("Skipped (unavailable — no fixtures to attach): %s",
             sorted(set(raw.get("competitions", {})) - avail))

    session = requests.Session()
    counter = [0]
    owner_cache: dict = {}     # playlist_id -> owner dict | None
    channel_cache: dict = {}   # channel_id  -> [playlists]

    # Season-nested this-run resolutions: {season -> {comp -> {source -> id}}}.
    # Keyed by each competition's OWN season (season_for_competition) so tournament
    # editions land under their edition year, not the run's league season.
    run_resolved: dict = {}
    run_team: dict = {}
    flags: list = []

    def over_budget() -> bool:
        return counter[0] >= _UNIT_CEILING

    def resolve_owner(pid):
        if pid in owner_cache:
            return owner_cache[pid]
        try:
            owner = fetch_playlist_owner(pid, api_key, session=session, quota=None)
            counter[0] += 1
        except Exception as exc:
            log.warning("owner resolve failed for %s: %s", pid, exc)
            owner = None
        owner_cache[pid] = owner
        return owner

    def channel_playlists(channel_id):
        if channel_id not in channel_cache:
            channel_cache[channel_id] = list_channel_playlists(
                channel_id, api_key, session=session, counter=counter)
        return channel_cache[channel_id]

    def resolve_one(comp, source_label, pid, *, require_gate, team_mode=False):
        """Resolve the current-season playlist for one mapped source; returns pid|None."""
        owner = resolve_owner(pid)
        if owner is None:
            flags.append({"competition": comp, "source": source_label,
                          "mapped_id": pid, "reason": "channel_unresolvable_reseed"})
            log.warning("[%s/%s] mapped playlist %s is dead/private — FLAG reseed",
                        comp, source_label, pid)
            return None
        pls = channel_playlists(owner["channel_id"])
        match = select_current_season_playlist(
            comp, pls, require_competition_gate=require_gate, team_mode=team_mode)
        if match and match.get("id"):
            if match["id"] == pid:
                log.info("[%s/%s] already current: %r (%s)",
                         comp, source_label, match["title"], pid)
            else:
                log.info("[%s/%s] RESOLVED current-season playlist: %r (%s) "
                         "[was %s]", comp, source_label, match["title"],
                         match["id"], pid)
            return match["id"]
        flags.append({"competition": comp, "source": source_label,
                      "mapped_id": pid, "reason": "no_confident_match_kept_last_known_good",
                      "owner_channel": owner.get("channel_title", "")})
        log.warning("[%s/%s] no confident current-season match on channel %r — "
                    "KEEPING last-known-good %s and flagging",
                    comp, source_label, owner.get("channel_title", ""), pid)
        return None

    # ── Tier 4 broadcaster playlists ──
    for comp, bmap in playlists_cfg.items():
        if comp not in avail or not isinstance(bmap, dict):
            continue
        for broadcaster, ids in bmap.items():
            for pid in (ids if isinstance(ids, list) else [ids]):
                if not pid or over_budget():
                    continue
                new_id = resolve_one(comp, broadcaster, pid, require_gate=True)
                if new_id:
                    s = str(season_for_competition(comp))
                    # Single season playlist today; format reserved 'undetermined'
                    # (season-vs-gameweek detection is a separate concern).
                    run_resolved.setdefault(s, {}).setdefault(comp, {})[broadcaster] = \
                        season_leaf(new_id)

    # ── Tier 1c/1d team playlists (single-team channels → no competition-gate) ──
    for comp, tmap in team_pls.items():
        if comp not in avail or not isinstance(tmap, dict):
            continue
        for team, pid in tmap.items():
            if not pid or over_budget():
                continue
            new_id = resolve_one(comp, f"team:{team}", pid, require_gate=False,
                                 team_mode=True)
            if new_id:
                s = str(season_for_competition(comp))
                run_team.setdefault(s, {}).setdefault(comp, {})[team] = season_leaf(new_id)

    # Standing "missing channel" detector — pure config, no API. Runs every run
    # (even a no-op) so newly-promoted/added clubs surface automatically. It only
    # FLAGS; it never fetches or adds channels.
    missing = compute_missing_channels(raw, avail, flags)
    _log_missing_channels(missing)

    # Load existing store (migrating the old flat shape) and MERGE this run's
    # resolutions in — preserving all prior seasons and any same-season entries
    # this run didn't re-resolve. Never a fresh empty overwrite.
    existing = migrate_flat_discovered(load_json_file(DISCOVERED_PATH))
    merged = merge_discovered_seasons(existing, run_resolved, run_team)

    payload = {
        "generated_at":    utc_now_iso(),
        "current_season":  current_season(),
        "resolved":        merged["resolved"],
        "team":            merged["team"],
        "team_matcher_version": TEAM_MATCHER_VERSION,
        "flags":           flags,
        "missing_channels": missing,
        "estimated_units": counter[0],
    }
    wrote = write_discovered_if_changed(DISCOVERED_PATH, payload)

    log.info("%s %s: %d comp overrides this run, %d team overrides this run, "
             "%d flags, ~%d units",
             "Wrote" if wrote else "Unchanged (skipped write)", DISCOVERED_PATH.name,
             sum(len(v) for comps in run_resolved.values() for v in comps.values()),
             sum(len(v) for comps in run_team.values() for v in comps.values()),
             len(flags), counter[0])
    if flags:
        log.warning("FLAGS for human review (%d): %s", len(flags),
                    json.dumps(flags, ensure_ascii=False))
    if over_budget():
        log.warning("Unit ceiling %d reached — some sources may be unresolved this run",
                    _UNIT_CEILING)


if __name__ == "__main__":
    main()
