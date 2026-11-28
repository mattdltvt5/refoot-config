#!/usr/bin/env python3
"""
scripts/fixture_providers.py

Pluggable fixture provider layer.

FixtureProvider         -- base class: get_fixtures() plus optional capabilities
                           (get_standings / get_tournament_groups) gated by supports()
FootballDataProvider    -- football-data.org (all existing FD competitions)
ApiSportsProvider       -- API-Sports free tier (Copa America 2024; EL parked)
TheSportsDBProvider     -- TheSportsDB v1 (Europa League): football-data-shaped
                           matches + computed league table, rounds cached in
                           highlights/thesportsdb-rounds.json
ApisportsQuotaTracker   -- daily call-count tracker (highlights/apisports-quota-tracker.json)

PROVIDERS / get_provider()  -- registry: a COMPETITION_CONFIG "provider" value
                           (highlights_common) -> provider instance. The job scripts
                           only ever get providers through it (iter_job_providers).

APISPORTS_COMPETITIONS  -- config-driven registry; adding a competition is a one-entry
                           config change with no code modification needed.

Free-tier constraint:
    API-Sports free plan covers seasons 2022-2024 only.  Seasons 2025+ return a paywall
    error.  The free tier is a historical backfill source only -- it cannot cover the
    current season.  Live/current-season coverage requires a paid plan.
"""

import json
import logging
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

import requests

from highlights_common import (
    COMPETITION_CONFIG,
    FD_BASE,
    FD_SLEEP_SECONDS,
    HIGHLIGHTS_DIR,
    JOB_TOURNAMENT_GROUPS,
    PROVIDER_APISPORTS,
    PROVIDER_FOOTBALLDATA,
    PROVIDER_THESPORTSDB,
    SUMMER_TOURNAMENT_COMPS,
    TEAM_TITLE_ALIASES,
    _normalize,
    competitions_for_job,
    fd_get,
    load_json_file,
    override_crest,
    season_for_competition,
    stage_to_file_stem,
    utc_now_iso,
    write_json_atomic,
)

log = logging.getLogger(__name__)


# ── FINISHED-preservation merge guard ─────────────────────────────────────────
#
# football-data.org intermittently re-serves an already-played match as
# status="TIMED" with a null score (an upstream data-quality flicker seen on the
# 2026-27 season).  Because write_fixtures_artifacts() overwrites
# fixtures/{slug}/{season}.json on every ~5-minute run, an unguarded write would
# clobber a good FINISHED result with that regressed, scoreless record — and the
# app renders a null score as "not started", hiding both the score and the
# still-cached highlight.
#
# The guard is a TERMINALITY invariant: a cached FINISHED record carrying a real
# score is never replaced by an incoming record with a NON-terminal status, UNLESS
# the incoming record announces a genuine terminal non-played correction
# (postponement / abandonment).  A played match never un-finishes, so this rejects
# BOTH the scoreless TIMED/null flicker AND a stale IN_PLAY/PAUSED that still
# carries the final score (football-data.org sometimes leaves a played match stuck
# in-play).  Normal progression (TIMED → FINISHED) and real re-writes (an incoming
# FINISHED with a corrected score) are always allowed through.

FINISHED_STATUS = "FINISHED"

# The in-progress statuses football-data.org emits (also the app's live gate).
LIVE_STATUSES = frozenset({"IN_PLAY", "PAUSED"})

# A match cannot stay live indefinitely.  Once its kickoff is older than this —
# comfortably longer than any real match including extra time, penalties and
# stoppage — an IN_PLAY/PAUSED status is no longer credible and is finalized.
STALE_LIVE_AFTER = timedelta(hours=4)

# How long a held live record (see merge_preserve_finished) may outlast an
# upstream that keeps re-serving the match as not started. Past this the
# incoming record wins again, so a genuinely changed fixture can't stay stuck.
HOLD_LIVE_FOR = timedelta(hours=24)

# Terminal non-played statuses: a legitimate forward correction that SHOULD
# overwrite a previously-FINISHED cache even though it carries no score.  Spelled
# exactly as football-data.org emits them (see FD v4 match `status` vocabulary).
TERMINAL_UNPLAYED_STATUSES = frozenset({
    "POSTPONED", "CANCELLED", "SUSPENDED", "AWARDED",
})

# Every match status football-data.org documents. Anything else is unreadable
# data, not a result: on 2026-11-04 Rayo Vallecano v Athletic Club (564698, an
# abandoned game re-scheduled for 9 Dec) arrived with status
# "2026-10-10 12:00:00Z" and its old 0-0, which the app showed as a played 0-0.
FD_STATUSES = frozenset({
    "SCHEDULED", "TIMED", "IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT",
    "FINISHED", "SUSPENDED", "POSTPONED", "CANCELLED", "AWARDED",
})


def sanitize_status(status, full_time: dict, match_id=None) -> tuple[str, dict]:
    """(status, score.fullTime) safe to publish: a status football-data.org
    doesn't document becomes SCHEDULED with no score (an unreadable status
    can't vouch for a result), with a warning. Known statuses pass unchanged."""
    if status in FD_STATUSES:
        return status, full_time
    log.warning(
        f"match {match_id}: unknown status {status!r} - published as SCHEDULED "
        f"without its score {full_time}"
    )
    return "SCHEDULED", {"home": None, "away": None}


def _has_resolved_score(record: dict) -> bool:
    """True when both full-time goal fields are genuinely present.

    Uses `is not None` so a real 0-0 (home=0, away=0) counts as resolved rather
    than being mistaken for an empty/unplayed score.
    """
    ft = ((record or {}).get("score") or {}).get("fullTime") or {}
    return ft.get("home") is not None and ft.get("away") is not None


def merge_preserve_finished(cached: dict, incoming: dict, now: datetime | None = None) -> dict:
    """Decide which of two records for the SAME match_id to keep.

    Treats a scored FINISHED as TERMINAL: if the cached record is FINISHED with a
    resolved (non-null) score, only two incoming records may replace it —
      * another FINISHED (a genuine score correction / re-write), or
      * a terminal non-played correction (POSTPONED/CANCELLED/SUSPENDED/AWARDED).
    Any other incoming status is rejected, because a played match never
    un-finishes.  This blocks BOTH the FINISHED→TIMED/null (scoreless) flicker and
    the FINISHED→IN_PLAY/PAUSED reversion where upstream keeps the final score but
    flips the status back to in-play.

    Two more rules protect a game in progress (2026-10-24: football-data.org
    alternated IN_PLAY-with-score and TIMED-with-no-score copies of the same
    match every few minutes, and left four LaLiga/Serie A games at TIMED/null for
    up to 10 h after they ended, so the app showed them as not started):
      * Never take an OLDER copy (checked first, for every status - finished
        records too): when both records carry football-data.org's
        `lastUpdated`, an incoming record older than the cached one is rejected.
      * A game under way never goes back to "not started": a cached live record
        (IN_PLAY/PAUSED with a score) is kept when the incoming one is scoreless
        and neither live, FINISHED nor a terminal non-played status - but only
        once the (incoming) kickoff has passed and for at most HOLD_LIVE_FOR, so a
        fixture moved to a later date, or a genuinely changed one, still flows.

    Otherwise the incoming record wins, so normal progression (TIMED → IN_PLAY →
    FINISHED) and live score changes (incl. a disallowed goal) are never blocked.
    """
    if not cached:
        return incoming
    # Older copy first, for every status: football-data.org serves two cached
    # snapshots in turn (2026-10-26: finished matches alternated lastUpdated
    # 10-26T10:20 / 10-25T00:20 on every run), so without this a FINISHED
    # record flipped between them and the data was re-committed every run.
    cached_lu = _parse_utc(cached.get("lastUpdated"))
    incoming_lu = _parse_utc(incoming.get("lastUpdated"))
    if cached_lu and incoming_lu and incoming_lu < cached_lu:
        return cached
    cached_is_scored_finished = (
        cached.get("status") == FINISHED_STATUS and _has_resolved_score(cached)
    )
    if cached_is_scored_finished:
        if incoming.get("status") == FINISHED_STATUS:
            return incoming
        if incoming.get("status") in TERMINAL_UNPLAYED_STATUSES:
            return incoming
        return cached
    if _holds_live(cached, incoming, now):
        return cached
    return incoming


def _holds_live(cached: dict, incoming: dict, now: datetime | None = None) -> bool:
    """True when [cached] is a live record with a score that [incoming] would
    send back to "not started" (scoreless, not live / FINISHED / called off)
    after kickoff, within HOLD_LIVE_FOR of it."""
    if cached.get("status") not in LIVE_STATUSES or not _has_resolved_score(cached):
        return False
    status = incoming.get("status")
    if (status in LIVE_STATUSES or status == FINISHED_STATUS
            or status in TERMINAL_UNPLAYED_STATUSES or _has_resolved_score(incoming)):
        return False
    kickoff = _parse_utc(incoming.get("utcDate") or cached.get("utcDate"))
    if kickoff is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return kickoff <= ref and ref - kickoff < HOLD_LIVE_FOR


def preserve_finished(data, prev, now=None):
    """Keep finished results that football-data.org briefly re-serves unplayed.

    The same FINISHED->TIMED/null flicker the league fixtures guard against
    (merge_preserve_finished) also hits /competitions/CL: on
    2026-10-15 at 00:46 and 01:16 UTC every UCL game of 13-14 Oct came back
    TIMED with no score, so ucl.json - and the Home index built from it - showed
    them as not started until the next run. A scored FINISHED match on disk is
    kept unless the new record is FINISHED too or a terminal non-played status,
    and a live record is held per merge_preserve_finished's live rules.
    Applies to knockout `matches` (keyed by id) and `groupMatches` (match_id);
    used by sync_tournaments (UCL/WC/Euro) and sync_copa_tournament (Copa).
    Returns how many records were kept from disk.
    """
    kept = 0
    for key, idf in (("matches", "id"), ("groupMatches", "match_id")):
        old = {m.get(idf): m for m in prev.get(key, []) or [] if m.get(idf) is not None}
        out = []
        for m in data.get(key, []):
            cached = old.get(m.get(idf))
            chosen = merge_preserve_finished(cached, m, now) if cached else m
            if chosen is not m:
                kept += 1
                # The cached record wins whole, but keep the newest video_id.
                chosen = {**chosen, "video_id": m.get("video_id") or chosen.get("video_id")}
            out.append(chosen)
        data[key] = out
    return kept


def _parse_utc(value: str):
    """Parse an ISO-8601 UTC kickoff string to an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (ValueError, TypeError):
        return None


def coerce_stale_live_to_finished(
    fixtures: list[dict], now: datetime | None = None
) -> list[dict]:
    """Finalize any match left LIVE long after kickoff.

    football-data.org occasionally leaves a played match stuck at IN_PLAY/PAUSED
    (its status never advances to FINISHED), which the client renders as a
    perpetual LIVE badge.  Once kickoff is older than STALE_LIVE_AFTER a live
    status is no longer credible, so the record is coerced to FINISHED — keeping
    whatever score it carries.  Only records with a resolved score are coerced (a
    scoreless stuck-live record is left for the guard/ledger to handle) and only
    when a kickoff time is present.  Returns a new list; input records are not
    mutated; incoming order is preserved.
    """
    ref = now or datetime.now(timezone.utc)
    out: list[dict] = []
    for fx in fixtures:
        if fx.get("status") in LIVE_STATUSES and _has_resolved_score(fx):
            ko = _parse_utc(fx.get("utcDate"))
            if ko is not None and (ref - ko) > STALE_LIVE_AFTER:
                fx = {**fx, "status": FINISHED_STATUS}
        out.append(fx)
    return out


def merge_fixtures_preserving_finished(
    cached_fixtures: list[dict], incoming_fixtures: list[dict],
    now: datetime | None = None,
) -> list[dict]:
    """Merge an incoming fixtures list against the cached list, keyed by match_id.

    Applies merge_preserve_finished() per match.  The returned list preserves the
    incoming ordering and length; new match_ids (absent from the cache) pass
    through unchanged.  Records with a None match_id are passed through unmerged
    (no stable key to pair on).  Pairing is strictly by match_id — never by team
    name or list position — because match_ids are stable across the regression.
    """
    cached_by_id = {
        r.get("match_id"): r
        for r in (cached_fixtures or [])
        if r.get("match_id") is not None
    }
    merged: list[dict] = []
    for inc in incoming_fixtures:
        mid = inc.get("match_id")
        cached = cached_by_id.get(mid) if mid is not None else None
        merged.append(merge_preserve_finished(cached, inc, now) if cached else inc)
    return merged


# ── Durable results ledger (self-healing) ─────────────────────────────────────
#
# The FINISHED-preservation guard above protects the *cache* from a good→bad
# overwrite, but it cannot recover a result that was already lost — a score nulled
# before the guard shipped, or a from-scratch cache rebuild.  The results ledger
# is an FD-independent, append-only record of every final score the pipeline has
# ever observed: once a match is seen FINISHED with a resolved score, its result
# is recorded in results/{slug}/{season}.json and re-asserted on every subsequent
# run.  An upstream FINISHED→TIMED/null reversion therefore self-heals — the real
# score is restored automatically on the next run, with no manual intervention.
#
# Corrections still flow: an incoming FINISHED with a *different* score updates
# the ledger, and a terminal non-played status (POSTPONED/CANCELLED/SUSPENDED/
# AWARDED) clears the FINISHED entry so a genuine postponement is honoured.


def _is_terminal_unplayed(record: dict) -> bool:
    return (record or {}).get("status") in TERMINAL_UNPLAYED_STATUSES


def update_results_ledger(ledger: dict, fixtures: list[dict]) -> dict:
    """Return an updated ledger given the outgoing fixtures for one {slug}/{season}.

    - A fixture that is FINISHED with a resolved score is recorded, or its score
      updated on a correction; ``first_finished_at`` is set once and preserved.
    - A fixture with a terminal non-played status removes any FINISHED entry, so a
      genuine postponement/abandonment is not force-restored to FINISHED.

    Keyed by str(match_id); fixtures with a None match_id are ignored.  The input
    ledger is not mutated.
    """
    out = dict(ledger or {})
    for fx in fixtures:
        mid = fx.get("match_id")
        if mid is None:
            continue
        key = str(mid)
        if fx.get("status") == FINISHED_STATUS and _has_resolved_score(fx):
            ft = (fx.get("score") or {}).get("fullTime") or {}
            prev = out.get(key) or {}
            out[key] = {
                "status":            FINISHED_STATUS,
                "score":             {"fullTime": {"home": ft.get("home"), "away": ft.get("away")}},
                "utcDate":           fx.get("utcDate", prev.get("utcDate", "")),
                "first_finished_at": prev.get("first_finished_at") or utc_now_iso(),
            }
        elif _is_terminal_unplayed(fx):
            out.pop(key, None)   # genuine forward correction — stop asserting FINISHED
    return out


def apply_results_ledger(fixtures: list[dict], ledger: dict) -> list[dict]:
    """Overlay the ledger onto fixtures, re-asserting a known FINISHED result.

    For each fixture whose match_id the ledger records as FINISHED, force
    status=FINISHED + the ledger score UNLESS the current fixture is ITSELF
    FINISHED (a real re-write / score correction is left to flow) or reports a
    terminal non-played status (a genuine postponement).  This re-asserts a lost
    result whether the current record regressed to a scoreless TIMED/null OR to a
    stale IN_PLAY/PAUSED that still carries a score.  All other fields are taken
    from the current fixture, so fresh crests/teams/kickoff times still flow.

    Returns a new list; incoming order is preserved.
    """
    out: list[dict] = []
    for fx in fixtures:
        mid = fx.get("match_id")
        rec = ledger.get(str(mid)) if (ledger and mid is not None) else None
        if (rec and rec.get("status") == FINISHED_STATUS
                and fx.get("status") != FINISHED_STATUS
                and not _is_terminal_unplayed(fx)):
            healed = dict(fx)
            ft = (rec.get("score") or {}).get("fullTime") or {}
            healed["status"] = FINISHED_STATUS
            healed["score"]  = {"fullTime": {"home": ft.get("home"), "away": ft.get("away")}}
            out.append(healed)
        else:
            out.append(fx)
    return out


# ── API-Sports quota constants ────────────────────────────────────────────────

APISPORTS_QUOTA_PATH = HIGHLIGHTS_DIR / "apisports-quota-tracker.json"
APISPORTS_DAILY_CAP  = 100   # free tier: 100 requests/day

# Free tier covers 2022-2024 only; never query a season beyond this.
_FREE_TIER_SEASON_MAX = 2024
_AS_BASE_URL          = "https://v3.football.api-sports.io"
_AS_INTER_REQ_SLEEP   = 7    # seconds -- free tier allows 10 req/min
_AS_MIN_DAILY_REM     = 5    # bail early when this few daily calls remain


# ── APISPORTS_COMPETITIONS config ─────────────────────────────────────────────
#
# Each entry drives ApiSportsProvider entirely -- adding a competition is a
# config-only change.
#
# round_map  -- verbatim API-Sports league.round string -> file stem
# stage_map  -- verbatim API-Sports league.round string -> (FD-equivalent stage, matchday)
#               stage/matchday values match what stage_to_file_stem() and
#               find_gameweek_playlist() expect, so Copa America flows through
#               the same downstream resolution logic as FD-sourced competitions.

APISPORTS_COMPETITIONS: dict = {
    "Copa America": {
        "league_id": 9,
        "season":    2024,
        # Maps verbatim API-Sports league.round -> output file stem
        "round_map": {
            "Group Stage - 1": "matchday-1",
            "Group Stage - 2": "matchday-2",
            "Group Stage - 3": "matchday-3",
            "Quarter-finals":  "quarter-final",
            "Semi-finals":     "semi-final",
            "3rd Place Final": "third-place",
            "Final":           "final",
        },
        # Maps verbatim API-Sports league.round -> (FD-equivalent stage value, matchday int|None)
        "stage_map": {
            "Group Stage - 1": ("GROUP_STAGE", 1),
            "Group Stage - 2": ("GROUP_STAGE", 2),
            "Group Stage - 3": ("GROUP_STAGE", 3),
            "Quarter-finals":  ("QUARTER_FINALS", None),
            "Semi-finals":     ("SEMI_FINALS", None),
            "3rd Place Final": ("THIRD_PLACE", None),
            "Final":           ("FINAL", None),
        },
    },
    # Europa League (id=3, season=2024, 269 fixtures) is intentionally parked.
    # Free tier covers 2022-2024; current EL seasons (2025+) require a paid plan.
    # To add historical EL backfill, add a "Europa League" entry here with its
    # own round_map and stage_map and run backfill_copa_america.py with COMP_NAME
    # set to "Europa League".
}


# ── Base class ────────────────────────────────────────────────────────────────


# Capabilities a provider may offer (FixtureProvider.supports).
CAP_FIXTURES          = "fixtures"           # finished fixtures by file stem (highlights jobs)
CAP_FULL_SEASON       = "full_season"        # all-status season list (fixtures/{slug} artifact)
CAP_STANDINGS         = "standings"          # get_standings()
CAP_TOURNAMENT_GROUPS = "tournament_groups"  # get_tournament_groups()


class FixtureProvider:
    """Base class. get_fixtures() is the provider's own (provider-specific
    signature) fixture call; the job scripts use the uniform per-competition
    entry points below, each gated by supports(capability)."""

    #: The capabilities this provider implements; the base offers none.
    CAPABILITIES: frozenset = frozenset()
    #: True for a provider that may only be called from its own scheduled jobs
    #: (API-Sports): the generic job loops skip it and log why, so it can never
    #: be reached from the incremental workflow.
    SCHEDULED_JOBS_ONLY: bool = False

    @classmethod
    def from_config(cls, config: dict) -> "FixtureProvider":
        """Build an instance from the run config (credentials etc.)."""
        return cls()

    @classmethod
    def supports(cls, capability: str) -> bool:
        return capability in cls.CAPABILITIES

    def get_fixtures(self, *args, **kwargs) -> dict:
        raise NotImplementedError

    # Uniform per-competition entry points (comp_cfg = a COMPETITION_CONFIG entry).

    def competition_fixtures(self, comp_name: str, comp_cfg: dict, season: int) -> dict:
        """{file_stem: [fixture]} for the competition's FINISHED matches."""
        raise NotImplementedError

    def competition_full_season(self, comp_name: str, comp_cfg: dict, season: int) -> list:
        """GroupMatch-compatible dicts for every match status."""
        raise NotImplementedError

    def get_standings(self, comp_name: str, comp_cfg: dict, season=None) -> dict:
        """The competition's standings payload."""
        raise NotImplementedError

    def get_tournament_groups(self, comp_name: str, comp_cfg: dict, season=None):
        """(standings_payload, matches_payload) for the tournament cache."""
        raise NotImplementedError


# ── football-data.org raw fetches (urllib; base_url/season overridable for tests)

# football-data's free tier allows 10 requests a minute. The tournament and
# standings jobs run right after fetch_highlights' burst of fixtures requests, so
# their first calls can be answered 429; one retry after this pause (the same
# back-off fd_get uses) lets them through instead of skipping the refresh.
FD_RATE_LIMIT_BACKOFF_SECONDS = 60


def _fd_get_json(url, api_key):
    """GET [url] with the FD auth header and parse the JSON. On HTTP 429, wait
    FD_RATE_LIMIT_BACKOFF_SECONDS and retry once; a second 429 (or any other
    error) raises urllib.error.HTTPError as before, so callers keep their cache."""
    for attempt in (1, 2):
        req = urllib.request.Request(url, headers={"X-Auth-Token": api_key})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            print(f"football-data.org rate-limited (429) - retrying in "
                  f"{FD_RATE_LIMIT_BACKOFF_SECONDS} s", file=sys.stderr)
            time.sleep(FD_RATE_LIMIT_BACKOFF_SECONDS)


def fd_fetch_matches(comp_id, api_key, base_url=FD_BASE, season=None):
    """Fetch /matches for a competition. Returns the parsed JSON dict.

    No status filter — the tournament cache needs ALL matches (TIMED knockout
    slots as well as FINISHED ones) so unplayed fixtures appear with null
    scores.  Raises urllib.error.HTTPError on non-200 responses (after one
    retry on 429).
    """
    url = f"{base_url}/competitions/{comp_id}/matches"
    if season is not None:
        url += f"?season={season}"
    return _fd_get_json(url, api_key)


def fd_fetch_standings(comp_id, api_key, base_url=FD_BASE, season=None):
    """Fetch /standings for a competition. Returns the parsed JSON dict.

    Raises urllib.error.HTTPError on non-200 responses (after one retry on
    429). Passing season avoids FD defaulting to a not-yet-started future season.
    """
    url = f"{base_url}/competitions/{comp_id}/standings"
    if season is not None:
        url += f"?season={season}"
    return _fd_get_json(url, api_key)


# ── FootballDataProvider ──────────────────────────────────────────────────────


class FootballDataProvider(FixtureProvider):
    """
    Fetches fixtures from football-data.org for one competition/season.

    The FD request fetches ALL match statuses (no status=FINISHED filter) so a
    single request can serve both:
      - get_fixtures()     → highlights path, FINISHED only, grouped by file stem
      - get_full_season()  → fixtures-artifact path, all statuses, GroupMatch shape

    The raw FD response is cached in _raw_cache per (code, season) so both callers
    share one HTTP request -- no double-fetch.

    Produces the same fixture dict shape as before for get_fixtures(); the new
    get_full_season() / _normalize_artifact() produces the GroupMatch-compatible
    shape used by the fixtures/{slug}.json artifact.
    """

    CAPABILITIES = frozenset({CAP_FIXTURES, CAP_FULL_SEASON, CAP_STANDINGS, CAP_TOURNAMENT_GROUPS})

    def __init__(self, fd_key: str, base_url: str = FD_BASE) -> None:
        self._fd_key    = fd_key
        self._base_url  = base_url
        self._raw_cache: dict[tuple, list] = {}  # (code, season) → raw match list

    @classmethod
    def from_config(cls, config: dict) -> "FootballDataProvider":
        return cls(config.get("fd_key", ""), base_url=config.get("fd_base_url", FD_BASE))

    # ── Uniform per-competition entry points ─────────────────────────────────

    def competition_fixtures(self, comp_name: str, comp_cfg: dict, season: int) -> dict:
        return self.get_fixtures(comp_cfg["fd_code"], comp_name, season)

    def competition_full_season(self, comp_name: str, comp_cfg: dict, season: int) -> list:
        return self.get_full_season(comp_cfg["fd_code"], comp_name, season)

    def get_standings(self, comp_name: str, comp_cfg: dict, season=None) -> dict:
        """/standings payload (verbatim). Raises urllib.error.HTTPError on non-200."""
        return fd_fetch_standings(comp_cfg["fd_id"], self._fd_key, self._base_url, season)

    def get_tournament_groups(self, comp_name: str, comp_cfg: dict, season=None):
        """(standings_payload, matches_payload), both verbatim, with the free-tier
        pause between the two requests. Raises urllib.error.HTTPError on non-200."""
        standings_payload = fd_fetch_standings(
            comp_cfg["fd_id"], self._fd_key, self._base_url, season)
        time.sleep(FD_SLEEP_SECONDS)
        matches_payload = fd_fetch_matches(
            comp_cfg["fd_id"], self._fd_key, self._base_url, season)
        return standings_payload, matches_payload

    # ── Internal: single FD request, cached ──────────────────────────────────

    def _fetch_raw(self, code: str, season: int) -> list:
        """
        Fetch all matches for one competition/season from FD (no status filter).

        Caches the result in _raw_cache so get_fixtures() and get_full_season()
        share one HTTP request when called with the same (code, season).
        Returns [] on any fetch error.  Callers handle throttling externally.
        """
        key = (code, season)
        if key in self._raw_cache:
            return self._raw_cache[key]
        try:
            resp = fd_get(
                f"{self._base_url}/competitions/{code}/matches",
                self._fd_key,
                {"season": str(season)},   # no status= filter → all statuses
            )
        except SystemExit:
            raise
        except Exception as exc:
            log.warning(f"Network error fetching {code}: {exc}")
            self._raw_cache[key] = []
            return []
        if resp.status_code == 404:
            log.warning(
                f"Competition {code} season {season} not found (404) -- skipping"
            )
            self._raw_cache[key] = []
            return []
        if not resp.ok:
            log.warning(
                f"football-data.org HTTP {resp.status_code} for {code} -- skipping"
            )
            self._raw_cache[key] = []
            return []
        self._raw_cache[key] = resp.json().get("matches", [])
        return self._raw_cache[key]

    # ── Highlights path ───────────────────────────────────────────────────────

    def get_fixtures(self, code: str, comp_name: str, season: int) -> dict:
        """
        Returns {file_stem: [fixture_dict, ...]} for FINISHED matches only.
        Callers are responsible for pre-request throttling (FD_SLEEP_SECONDS).
        Uses the cached raw response — no additional FD call if get_full_season()
        was already called for the same (code, season).
        """
        raw = self._fetch_raw(code, season)
        if not raw:
            return {}
        return self._normalize(raw, comp_name)

    def _normalize(self, matches: list, comp_name: str) -> dict:
        return finished_fixtures_by_stem(matches, comp_name)

    # ── Fixtures-artifact path ────────────────────────────────────────────────

    def get_full_season(self, code: str, comp_name: str, season: int) -> list[dict]:
        """
        Returns a flat list of GroupMatch-compatible dicts for ALL match statuses
        (scheduled, in-play, finished) for the given domestic-league competition.

        Shape matches tournament-groups/ groupMatches:
            {group, matchday, sourceRound, homeTeam{id,name,shortName,tla,crest},
             awayTeam{...}, score{fullTime{home,away}}, status, utcDate}

        The Flutter app's GroupMatch.fromJson reads homeTeam['crest'] and
        score['fullTime']['home'/'away'], so this shape is consumed unchanged.
        null scores are used for SCHEDULED/TIMED/IN_PLAY matches (no score yet).

        Uses the cached raw response — no additional FD call if get_fixtures() was
        already called for the same (code, season).
        """
        raw = self._fetch_raw(code, season)
        if not raw:
            return []
        return self._normalize_artifact(raw, comp_name, season)

    def _normalize_artifact(self, matches: list, comp_name: str, season: int) -> list[dict]:
        """Produce GroupMatch-compatible dicts for all match statuses."""
        result = []
        for m in matches:
            matchday = m.get("matchday")
            if matchday is None:
                continue
            utc_str = m.get("utcDate", "")
            status  = m.get("status", "")
            home    = m.get("homeTeam", {}) or {}
            away    = m.get("awayTeam", {}) or {}
            score   = m.get("score", {}) or {}
            ft      = score.get("fullTime", {}) or {}
            status, ft = sanitize_status(status, ft, m.get("id"))
            result.append({
                "match_id":    m.get("id"),
                "group":       "",
                "matchday":    matchday,
                "sourceRound": f"Gameweek {matchday}",
                "homeTeam": {
                    "id":        home.get("id"),
                    "name":      home.get("name", ""),
                    "shortName": home.get("shortName") or home.get("name", ""),
                    "tla":       home.get("tla", ""),
                    "crest":     override_crest(home.get("id"), home.get("crest", "")),
                },
                "awayTeam": {
                    "id":        away.get("id"),
                    "name":      away.get("name", ""),
                    "shortName": away.get("shortName") or away.get("name", ""),
                    "tla":       away.get("tla", ""),
                    "crest":     override_crest(away.get("id"), away.get("crest", "")),
                },
                "score": {
                    "fullTime": {
                        "home": ft.get("home"),   # None when unplayed
                        "away": ft.get("away"),
                    }
                },
                "status":  status,
                "utcDate": utc_str,
                # football-data.org's own "last changed" time: the merge never
                # replaces a record with an older copy of it.
                "lastUpdated": m.get("lastUpdated"),
            })
        result.sort(key=lambda x: (x["matchday"], x.get("utcDate", "")))
        finished = sum(1 for r in result if r["status"] == "FINISHED")
        log.info(
            f"{comp_name}: {len(result)} fixture(s) for season {season} artifact "
            f"({finished} FINISHED, {len(result) - finished} non-finished)"
        )
        return result


# ── ApiSportsProvider ─────────────────────────────────────────────────────────


# API-Sports fixture.status.short values for a match that is over.
_APISPORTS_FINISHED = {"FT", "AET", "PEN"}


def _apisports_status(fix: dict) -> str:
    short = (fix.get("fixture") or {}).get("status", {}).get("short", "")
    return "FINISHED" if short in _APISPORTS_FINISHED else short


def _apisports_score(fix: dict) -> dict:
    """API-Sports score in the football-data.org shape the scoreline guard reads.
    API-Sports "fulltime" is the 90-minute score, "extratime" the extra-time
    goals only, "penalty" the shootout; "goals" is the result before penalties."""
    raw = fix.get("score") or {}

    def pair(v):
        v = v or {}
        return {"home": v.get("home"), "away": v.get("away")}

    return {
        "fullTime":    pair(fix.get("goals")),
        "regularTime": pair(raw.get("fulltime")),
        "extraTime":   pair(raw.get("extratime")),
        "penalties":   pair(raw.get("penalty")),
    }


class ApiSportsProvider(FixtureProvider):
    """
    Fetches fixtures from API-Sports (free tier, historical backfill only).

    Security: never hardcode APISPORTS_API_KEY -- env var only, sourced from a
    GitHub Actions secret.  Do not use for live / current-season data; free
    tier covers 2022-2024 only.
    """

    CAPABILITIES = frozenset({CAP_FIXTURES})
    SCHEDULED_JOBS_ONLY = True

    @classmethod
    def from_config(cls, config: dict) -> "ApiSportsProvider":
        return cls(quota_tracker=config.get("apisports_quota"))

    def competition_fixtures(self, comp_name: str, comp_cfg: dict, season: int) -> dict:
        return self.get_fixtures(comp_name, APISPORTS_COMPETITIONS[comp_name])

    def __init__(self, quota_tracker=None) -> None:
        api_key = os.environ.get("APISPORTS_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("APISPORTS_API_KEY environment variable is not set")
        self._api_key     = api_key
        self._last_call   = 0.0
        self._calls_today = 0
        self._quota       = quota_tracker  # optional ApisportsQuotaTracker

    def get_fixtures(self, comp_name: str, comp_config: dict) -> dict:
        """
        Fetch all fixtures for one API-Sports competition/season.
        comp_config must contain: league_id, season, round_map, stage_map.
        Returns {file_stem: [fixture_dict, ...]} or {} on any error.
        """
        league_id = comp_config["league_id"]
        season    = comp_config["season"]

        if season > _FREE_TIER_SEASON_MAX:
            log.error(
                f"API-Sports: season {season} exceeds free-tier max "
                f"({_FREE_TIER_SEASON_MAX}) -- would hit paywall; aborting"
            )
            return {}

        body = self._get("/fixtures", {"league": league_id, "season": season})
        if body is None:
            return {}

        fixtures = body.get("response", [])
        if not fixtures:
            log.warning(f"API-Sports: 0 fixtures for {comp_name} season {season}")
            return {}

        return self._normalize(fixtures, comp_name, comp_config)

    def _normalize(self, fixtures: list, comp_name: str, comp_config: dict) -> dict:
        round_map = comp_config["round_map"]
        stage_map = comp_config["stage_map"]
        by_stem: dict = {}

        for fix in fixtures:
            league_round = fix.get("league", {}).get("round", "")
            stem = round_map.get(league_round)
            if stem is None:
                log.debug(
                    f"API-Sports: unmapped round {league_round!r} for {comp_name} -- skipping"
                )
                continue

            stage_val, matchday = stage_map.get(league_round, ("", None))
            date_str = fix.get("fixture", {}).get("date", "")[:10]
            if not date_str:
                continue

            home = fix.get("teams", {}).get("home", {})
            away = fix.get("teams", {}).get("away", {})
            home_id = home.get("id")
            away_id = away.get("id")

            by_stem.setdefault(stem, []).append({
                "match_id":   fix["fixture"]["id"],
                "home_team":  home.get("name", ""),
                "home_short": home.get("name", ""),  # API-Sports has no separate shortName
                "home_tla":   "",                     # API-Sports provides no TLA
                "home_crest": f"https://media.api-sports.io/football/teams/{home_id}.png" if home_id else "",
                "away_team":  away.get("name", ""),
                "away_short": away.get("name", ""),
                "away_tla":   "",
                "away_crest": f"https://media.api-sports.io/football/teams/{away_id}.png" if away_id else "",
                "date":       date_str,
                "matchday":   matchday,
                "stage":      stage_val,
                "status":     _apisports_status(fix),
                "score":      _apisports_score(fix),
            })

        total = sum(len(v) for v in by_stem.values())
        log.info(
            f"API-Sports: {comp_name} season {comp_config['season']}: "
            f"{total} fixture(s) across {len(by_stem)} stage(s)"
        )
        return by_stem

    def get_events(self, fixture_id):
        """Fetch goal & card events for one fixture via API-Sports /fixtures/events.
        Returns a normalized list (see _normalize_events); [] on error/none. The
        free-tier season constraint is enforced by get_fixtures in the surrounding
        backfill, so this is only called for in-range (2022-2024) fixtures."""
        body = self._get("/fixtures/events", {"fixture": fixture_id})
        if not body:
            return []
        return self._normalize_events(body.get("response", []))

    @staticmethod
    def _normalize_events(raw):
        """Map API-Sports event objects to the cache event schema. Keeps only Goal
        and Card events (feature #5). phase = 'extra' when the clock is past 90'
        (prorroga); the penalty-shootout aggregate is carried by the match's
        penaltiesResult, not per-event here. detail preserves Normal Goal / Penalty /
        Own Goal / Yellow Card / Red Card so the app can render precisely."""
        out = []
        for ev in raw or []:
            etype = ev.get("type")
            if etype not in ("Goal", "Card"):
                continue
            t = ev.get("time") or {}
            elapsed = t.get("elapsed")
            out.append({
                "kind":   "goal" if etype == "Goal" else "card",
                "minute": elapsed,
                "extra":  t.get("extra"),
                "phase":  "extra" if isinstance(elapsed, int) and elapsed > 90 else "regular",
                "team":   (ev.get("team") or {}).get("name", ""),
                "player": (ev.get("player") or {}).get("name", ""),
                "detail": ev.get("detail") or "",
            })
        out.sort(key=lambda e: (e["minute"] if isinstance(e["minute"], int) else 999,
                                e.get("extra") or 0))
        return out

    def _get(self, path: str, params: dict) -> dict | None:
        """
        Throttled GET to API-Sports.
        Returns parsed response body or None on any error (network, HTTP, API-level).
        Paywall errors (season locked) log a warning and return None -- callers get {}.
        """
        if self._last_call > 0:
            elapsed = time.monotonic() - self._last_call
            if elapsed < _AS_INTER_REQ_SLEEP:
                time.sleep(_AS_INTER_REQ_SLEEP - elapsed)

        url = f"{_AS_BASE_URL}/{path.lstrip('/')}"
        self._last_call   = time.monotonic()
        self._calls_today += 1

        if self._quota is not None:
            try:
                self._quota.increment()
            except RuntimeError as exc:
                log.error(str(exc))
                return None

        try:
            resp = requests.get(
                url,
                headers={"x-apisports-key": self._api_key},
                params=params,
                timeout=15,
            )
        except requests.RequestException as exc:
            log.warning(f"API-Sports: network error on {url}: {exc}")
            return None

        try:
            remaining = int(resp.headers.get("x-ratelimit-requests-remaining", "999"))
        except (ValueError, TypeError):
            remaining = 999
        log.info(f"API-Sports: call #{self._calls_today}, daily_remaining={remaining}")
        if remaining < _AS_MIN_DAILY_REM:
            log.warning(
                f"API-Sports: only {remaining} daily calls remaining -- aborting"
            )
            return None

        if resp.status_code != 200:
            log.warning(f"API-Sports: HTTP {resp.status_code} from {url}")
            return None

        body   = resp.json()
        errors = body.get("errors")
        if errors:
            err_str = str(errors).lower()
            if "does not have access to this season" in err_str or "free plans" in err_str:
                log.warning(f"API-Sports: season locked (free-tier paywall): {errors}")
                return None
            log.warning(f"API-Sports: API-level errors: {errors}")
            return None

        return body


# ── TheSportsDBProvider ───────────────────────────────────────────────────────
#
# TheSportsDB (thesportsdb.com, v1 JSON API) supplies competitions football-data's
# free tier refuses (the Europa League answers 403). The public free key "123"
# needs no account; set THESPORTSDB_API_KEY (a GitHub Actions secret) to use a
# personal premium key instead. Free-tier facts this class is built around
# (measured 2026-11-27):
#   - documented as 30 requests a minute, but bursts of ~10-13 requests drew
#     429s at 2.5 s and even 4 s spacing, sometimes still refused 65 s later.
#     So: TSDB_SLEEP_SECONDS between requests, one retry after
#     TSDB_RATE_LIMIT_BACKOFF_SECONDS, no more requests in that run once the
#     retry fails, and at most TSDB_MAX_REQUESTS_PER_RUN per run (the first
#     fill of a season spreads over two runs; afterwards a run needs 1-3);
#   - eventsround.php returns a whole round (capped at 50 events); the other list
#     endpoints stop at 1-15 events, and lookuptable is empty for UEFA club
#     competitions, so the league table is computed from the results;
#   - no live scores on the free key, so statuses can arrive late;
#   - strTimestamp is UTC without a "Z";
#   - qualifying rounds reuse the league-phase round numbers (2026-27 round 1 =
#     July qualifiers + September matchday 1), so every round keeps only events
#     on/after the season's tsdb_season_start (default 1 September);
#   - knockout round codes: 32 play-off, 16 round of 16, 125 quarter-final,
#     150 semi-final, 200 final;
#   - terms: the free key may not ship inside an app-store app (this pipeline and
#     the web app are fine); badges are third-party marks, used unmodified.
#
# Output is football-data-shaped (match and standings payloads), so the job
# scripts, the highlights matcher and the app read it with no TheSportsDB code.
# Rounds are cached in TSDB_ROUNDS_CACHE so the ~5-minute runs re-request only
# rounds that can still change (see tsdb_round_due).

TSDB_BASE = "https://www.thesportsdb.com/api/v1/json"
TSDB_FREE_KEY = "123"
TSDB_SLEEP_SECONDS = 4                 # 15 req/min (see the free-tier notes above)
TSDB_RATE_LIMIT_BACKOFF_SECONDS = 65
TSDB_MAX_REQUESTS_PER_RUN = 8
TSDB_ROUNDS_CACHE = HIGHLIGHTS_DIR / "thesportsdb-rounds.json"

# TheSportsDB ids are moved into their own range so a team or match can never
# share an id with a football-data one (FD team ids are below 100k, match ids
# around 600k). Clubs that also appear in our football-data data get their FD
# identity instead (TsdbTeamIndex), so favourites and team pages carry over.
TSDB_ID_OFFSET = 1_000_000_000

TSDB_DEFAULT_LEAGUE_ROUNDS = 8
TSDB_DEFAULT_KNOCKOUT_ROUNDS = {
    32: "PLAYOFFS", 16: "LAST_16", 125: "QUARTER_FINALS", 150: "SEMI_FINALS",
    200: "FINAL",
}
TSDB_DEFAULT_SEASON_START = "09-01"    # MM-DD: earlier events are qualifiers
_SINGLE_LEG_STAGES = frozenset({"FINAL", "THIRD_PLACE"})

# Round refresh policy (tsdb_round_due).
TSDB_SETTLED_AFTER = timedelta(days=3)     # finished this long ago: never re-fetched
TSDB_ACTIVE_BEFORE = timedelta(days=1)     # kickoff within a day: round is active
TSDB_ACTIVE_REFRESH = timedelta(minutes=30)
TSDB_IDLE_REFRESH = timedelta(hours=24)    # future / not yet drawn rounds

# TheSportsDB strStatus -> football-data status. The app shows a match as live
# only for IN_PLAY / PAUSED, so extra time and shootouts map to IN_PLAY.
_TSDB_STATUS = {
    "NS": "TIMED", "Not Started": "TIMED", "TBD": "SCHEDULED",
    "Time to be defined": "SCHEDULED",
    "1H": "IN_PLAY", "2H": "IN_PLAY", "ET": "IN_PLAY", "P": "IN_PLAY", "LIVE": "IN_PLAY",
    "HT": "PAUSED", "BT": "PAUSED",
    "FT": "FINISHED", "AET": "FINISHED", "PEN": "FINISHED", "AP": "FINISHED",
    "Match Finished": "FINISHED",
    "PST": "POSTPONED", "Postponed": "POSTPONED",
    "CANC": "CANCELLED", "Cancelled": "CANCELLED",
    "ABD": "SUSPENDED", "Abandoned": "SUSPENDED", "SUSP": "SUSPENDED", "INT": "SUSPENDED",
    "AWD": "AWARDED", "WO": "AWARDED",
}
# A round whose events all have one of these statuses can no longer change.
_TSDB_SETTLED = frozenset({"FINISHED", "CANCELLED", "AWARDED"})

# Only these event fields are cached (the rest is media / betting noise).
_TSDB_EVENT_FIELDS = (
    "idEvent", "idLeague", "intRound", "strTimestamp", "dateEvent", "strTime",
    "idHomeTeam", "strHomeTeam", "strHomeTeamBadge",
    "idAwayTeam", "strAwayTeam", "strAwayTeamBadge",
    "intHomeScore", "intAwayScore", "intHomeScoreExtra", "intAwayScoreExtra",
    "strStatus", "strPostponed",
)


def _tsdb_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def tsdb_season(season: int) -> str:
    """TheSportsDB's season string for a season starting in [season]."""
    return f"{season}-{season + 1}"


def tsdb_kickoff(event: dict) -> str | None:
    """The event's kickoff as football-data's "YYYY-MM-DDTHH:MM:SSZ" (UTC), or None."""
    raw = (event.get("strTimestamp") or "").strip()
    if not raw:
        day = (event.get("dateEvent") or "").strip()
        if not day:
            return None
        raw = f"{day}T{(event.get('strTime') or '00:00:00').strip()}"
    try:
        dt = datetime.fromisoformat(raw.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)   # TheSportsDB timestamps are UTC
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tsdb_status(event: dict) -> str:
    """football-data status for a TheSportsDB event. Unknown values pass through
    unchanged so sanitize_status publishes them as SCHEDULED, with a warning."""
    if (event.get("strPostponed") or "").strip().lower() == "yes":
        return "POSTPONED"
    raw = (event.get("strStatus") or "").strip()
    if not raw:
        return "TIMED"
    return _TSDB_STATUS.get(raw, raw)


def tsdb_score(event: dict, status: str) -> dict:
    """football-data-shaped score. As football-data does, a shootout's fullTime
    includes the shootout goals (penalties holds them on their own); TheSportsDB's
    intHome/AwayScore is the result after extra time and intHome/AwayScoreExtra the
    shootout."""
    home, away = _tsdb_int(event.get("intHomeScore")), _tsdb_int(event.get("intAwayScore"))
    empty = {"home": None, "away": None}
    score = {"winner": None, "duration": "REGULAR", "fullTime": dict(empty),
             "halfTime": dict(empty)}
    if status not in ("FINISHED", "IN_PLAY", "PAUSED") or home is None or away is None:
        return score
    raw = (event.get("strStatus") or "").strip()
    if raw in ("PEN", "AP"):
        score["duration"] = "PENALTY_SHOOTOUT"
        ph = _tsdb_int(event.get("intHomeScoreExtra"))
        pa = _tsdb_int(event.get("intAwayScoreExtra"))
        if ph is not None and pa is not None:
            score["penalties"] = {"home": ph, "away": pa}
            home, away = home + ph, away + pa
    elif raw == "AET":
        score["duration"] = "EXTRA_TIME"
    score["fullTime"] = {"home": home, "away": away}
    if status == "FINISHED":
        score["winner"] = ("HOME_TEAM" if home > away
                           else "AWAY_TEAM" if away > home else "DRAW")
    return score


def tsdb_round_due(entry: dict | None, now: datetime) -> bool:
    """Whether a cached round should be requested again.

    - never fetched (or unreadable) -> yes;
    - empty (knockout not drawn yet) -> once a day;
    - every event finished / cancelled / awarded and the last kickoff more than
      TSDB_SETTLED_AFTER ago -> never (a postponed game keeps the round open);
    - a kickoff within the last TSDB_SETTLED_AFTER or the next TSDB_ACTIVE_BEFORE
      -> every TSDB_ACTIVE_REFRESH;
    - otherwise (future round) -> once a day.
    """
    if not entry:
        return True
    try:
        fetched = datetime.fromisoformat((entry.get("last_updated") or "").replace("Z", "+00:00"))
    except ValueError:
        return True
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age = now - fetched
    events = entry.get("events") or []
    kickoffs = []
    for e in events:
        k = tsdb_kickoff(e)
        if k:
            kickoffs.append(datetime.fromisoformat(k.replace("Z", "+00:00")))
    if not kickoffs:
        return age >= TSDB_IDLE_REFRESH
    settled = all(tsdb_status(e) in _TSDB_SETTLED for e in events)
    if settled and max(kickoffs) < now - TSDB_SETTLED_AFTER:
        return False
    if any(now - TSDB_SETTLED_AFTER <= k <= now + TSDB_ACTIVE_BEFORE for k in kickoffs):
        return age >= TSDB_ACTIVE_REFRESH
    return age >= TSDB_IDLE_REFRESH


# Words dropped before comparing club names ("AC Milan" ~ "Milan", "Lille OSC" ~
# "Lille"): legal forms and generic club words, never a distinguishing name.
_TEAM_KEY_STOPWORDS = frozenset({
    "fc", "cf", "afc", "sc", "ac", "as", "ssc", "club", "de", "sk", "fk", "jk", "bk",
    "if", "cp", "sv", "vfb", "vfl", "tsg", "rc", "ss", "us", "acf", "calcio", "sad",
    "kv", "nk", "gnk", "rcd", "ud", "cd", "sl", "osc", "hsc", "losc", "1",
})


def tsdb_team_key(name: str) -> str:
    """Comparable form of a club name: accents folded, punctuation and generic
    words dropped."""
    tokens = re.split(r"[^a-z0-9]+", _normalize(name or ""))
    return " ".join(t for t in tokens if t and t not in _TEAM_KEY_STOPWORDS)


def load_known_fd_teams(repo_root=None) -> list[dict]:
    """Every club team in our football-data data: the league fixture files and the
    football-data club tournaments' files (Champions League, with archives). The
    newest file wins for a team's name and crest."""
    root = pathlib.Path(repo_root) if repo_root else HIGHLIGHTS_DIR.parent
    files = sorted((root / "fixtures").glob("*/*.json"))
    for name, cfg in COMPETITION_CONFIG.items():
        if (cfg.get("provider") == PROVIDER_FOOTBALLDATA
                and JOB_TOURNAMENT_GROUPS in cfg.get("jobs", ())
                and name not in SUMMER_TOURNAMENT_COMPS):
            files += sorted((root / "tournament-groups" / cfg["slug"]).glob("*.json"))
            files.append(root / "tournament-groups" / f"{cfg['slug']}.json")
    teams: dict = {}
    for path in files:
        data = load_json_file(path)
        if not isinstance(data, dict):
            continue
        for m in (data.get("fixtures") or []) + (data.get("matches") or []) + \
                (data.get("groupMatches") or []):
            for side in ("homeTeam", "awayTeam"):
                t = m.get(side) or {}
                tid = t.get("id")
                if isinstance(tid, int) and 0 < tid < TSDB_ID_OFFSET and t.get("name"):
                    teams[tid] = {**teams.get(tid, {}), **{k: v for k, v in t.items() if v}}
    return list(teams.values())


class TsdbTeamIndex:
    """Matches TheSportsDB club names to football-data teams.

    A name matches when its tsdb_team_key equals the key of exactly one known FD
    team's name, short name or title alias (TEAM_TITLE_ALIASES); an ambiguous or
    unknown name stays a TheSportsDB team (namespaced id, its own badge), named by
    its title-alias canonical form when there is exactly one."""

    def __init__(self, fd_teams):
        self._fd: dict[str, dict] = {}
        for t in fd_teams:
            names = {t.get("name"), t.get("shortName"), *TEAM_TITLE_ALIASES.get(t.get("name") or "", [])}
            for n in names:
                k = tsdb_team_key(n or "")
                if k:
                    self._fd.setdefault(k, {})[t["id"]] = t
        self._canonical: dict[str, set] = {}
        for canon, aliases in TEAM_TITLE_ALIASES.items():
            for n in (canon, *aliases):
                k = tsdb_team_key(n)
                if k:
                    self._canonical.setdefault(k, set()).add(canon)

    def fd_team(self, name: str) -> dict | None:
        found = self._fd.get(tsdb_team_key(name))
        return next(iter(found.values())) if found and len(found) == 1 else None

    def canonical_name(self, name: str) -> str | None:
        found = self._canonical.get(tsdb_team_key(name))
        return next(iter(found)) if found and len(found) == 1 else None


def finished_fixtures_by_stem(matches: list, comp_name: str) -> dict:
    """{file_stem: [fixture]} for the FINISHED matches of a football-data-shaped
    /matches list (the highlights path, shared by every provider whose matches
    use that shape)."""
    # Only FINISHED matches contribute to highlights (SCHEDULED/IN_PLAY
    # matches have no highlights yet; POSTPONED/CANCELLED never will).
    # Pre-scan and main loop both skip non-FINISHED to keep is_gameweek_complete
    # working correctly: if SCHEDULED fixtures were included, a gameweek would
    # never be complete (SCHEDULED matches never get a video) → every run would
    # re-search YouTube for it.
    #
    # FD sometimes returns the same match twice for stage-aware competitions
    # (UCL/UEL): once with the correct knockout stage and once with
    # LEAGUE_STAGE + leg-number as matchday.  Pre-scan to identify match IDs
    # that have a proper knockout stem so we can skip the spurious league-stage
    # routing for those IDs.
    knockout_ids: set[int] = set()
    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        stage    = m.get("stage", "")
        matchday = m.get("matchday")
        if stage not in ("LEAGUE_STAGE", "GROUP_STAGE"):
            if stage_to_file_stem(stage, matchday, comp_name) is not None:
                knockout_ids.add(m["id"])

    by_stem: dict = {}
    for m in matches:
        if m.get("status") != "FINISHED":   # highlights only for finished matches
            continue
        matchday = m.get("matchday")
        stage    = m.get("stage", "")
        utc_str  = m.get("utcDate", "")
        if not utc_str:
            continue

        # Skip league-stage routing when a proper knockout stem exists for this ID.
        if stage in ("LEAGUE_STAGE", "GROUP_STAGE") and m["id"] in knockout_ids:
            log.debug(
                f"{comp_name}: skipping league-stage duplicate for match {m['id']}"
            )
            continue

        stem = stage_to_file_stem(stage, matchday, comp_name)
        if stem is None:
            continue

        home = m.get("homeTeam", {})
        away = m.get("awayTeam", {})
        by_stem.setdefault(stem, []).append({
            "match_id":   m["id"],
            "home_team":  home.get("name", ""),
            "home_short": home.get("shortName") or home.get("name", ""),
            "home_tla":   home.get("tla", ""),
            "away_team":  away.get("name", ""),
            "away_short": away.get("shortName") or away.get("name", ""),
            "away_tla":   away.get("tla", ""),
            "date":       utc_str[:10],
            # Full kickoff time: the matcher refuses clips added to a
            # playlist before it (see search_playlist).
            "kickoff":    utc_str,
            "matchday":   matchday,
            "stage":      stage,
            # Final result, for the highlights scoreline guard
            # (acceptable_scores). Not persisted to highlights files.
            "status":     m.get("status"),
            "score":      m.get("score") or {},
        })

    total = sum(len(v) for v in by_stem.values())
    log.info(
        f"{comp_name}: {total} finished fixture(s) across {len(by_stem)} file stem(s)"
    )
    return by_stem


def compute_league_table(matches: list, stage: str = "LEAGUE_STAGE") -> list[dict]:
    """football-data-shaped standings rows for one league phase, from its results.

    Every team with a [stage] fixture is listed (0 played before its first game).
    Order follows UEFA's league-phase criteria as far as the results allow:
    points, goal difference, goals scored, away goals scored, wins, away wins;
    then name, so the order is stable (UEFA's later criteria - opponents' results,
    disciplinary points, coefficient - aren't in the data)."""
    rows: dict = {}

    def row(team):
        r = rows.get(team["id"])
        if r is None:
            r = rows[team["id"]] = {
                "team": {k: team.get(k) for k in ("id", "name", "shortName", "tla", "crest")},
                "playedGames": 0, "form": None, "won": 0, "draw": 0, "lost": 0,
                "points": 0, "goalsFor": 0, "goalsAgainst": 0, "goalDifference": 0,
                "_awayGoals": 0, "_awayWins": 0,
            }
        return r

    for m in matches:
        if m.get("stage") != stage:
            continue
        home, away = m.get("homeTeam") or {}, m.get("awayTeam") or {}
        if home.get("id") is None or away.get("id") is None:
            continue
        h, a = row(home), row(away)
        ft = (m.get("score") or {}).get("fullTime") or {}
        hg, ag = ft.get("home"), ft.get("away")
        if m.get("status") != "FINISHED" or hg is None or ag is None:
            continue
        for r, gf, ga in ((h, hg, ag), (a, ag, hg)):
            r["playedGames"] += 1
            r["goalsFor"] += gf
            r["goalsAgainst"] += ga
            r["goalDifference"] = r["goalsFor"] - r["goalsAgainst"]
            if gf > ga:
                r["won"] += 1
                r["points"] += 3
            elif gf == ga:
                r["draw"] += 1
                r["points"] += 1
            else:
                r["lost"] += 1
        a["_awayGoals"] += ag
        if ag > hg:
            a["_awayWins"] += 1

    ordered = sorted(rows.values(), key=lambda r: (
        -r["points"], -r["goalDifference"], -r["goalsFor"], -r["_awayGoals"],
        -r["won"], -r["_awayWins"], (r["team"].get("name") or "").casefold()))
    out = []
    for i, r in enumerate(ordered, 1):
        r = {k: v for k, v in r.items() if not k.startswith("_")}
        out.append({"position": i, **r})
    return out


class TheSportsDBProvider(FixtureProvider):
    """
    TheSportsDB v1 for one or more competitions (COMPETITION_CONFIG entries with
    provider "thesportsdb"). Competition settings:

      tsdb_league_id        TheSportsDB league id (required; Europa League 4481)
      tsdb_league_rounds    league-phase rounds 1..N (default 8)
      tsdb_knockout_rounds  {round code: football-data stage} (default
                            TSDB_DEFAULT_KNOCKOUT_ROUNDS)
      tsdb_season_start     "MM-DD": events before it in the season's start year
                            are qualifiers and dropped (default "09-01")

    Matches are rebuilt in football-data's /matches shape: stage LEAGUE_STAGE +
    matchday for the league phase, knockout stages with matchday = leg (1/2, by
    kickoff order within the tie), FINAL with none. One instance per run caches
    the converted season, so the fixtures, standings and tournament calls share
    the same requests.
    """

    CAPABILITIES = frozenset({CAP_FIXTURES, CAP_STANDINGS, CAP_TOURNAMENT_GROUPS})

    def __init__(self, api_key: str = TSDB_FREE_KEY, base_url: str = TSDB_BASE,
                 cache_path=None, team_index: TsdbTeamIndex | None = None,
                 now=None) -> None:
        self._key = api_key or TSDB_FREE_KEY
        self._base_url = base_url
        self._cache_path = pathlib.Path(cache_path or TSDB_ROUNDS_CACHE)
        self._team_index = team_index
        self._now = now                      # callable -> aware datetime (tests)
        self._seasons: dict[tuple, list] = {}
        self._requests = 0
        self._rate_limited = False           # a retried 429 failed: stop for this run

    @classmethod
    def from_config(cls, config: dict) -> "TheSportsDBProvider":
        key = config.get("tsdb_key") or os.environ.get("THESPORTSDB_API_KEY") or TSDB_FREE_KEY
        return cls(key, base_url=config.get("tsdb_base_url", TSDB_BASE))

    # ── Uniform per-competition entry points ─────────────────────────────────

    def competition_fixtures(self, comp_name: str, comp_cfg: dict, season: int) -> dict:
        matches = self.season_matches(comp_name, comp_cfg, season)
        return finished_fixtures_by_stem(matches, comp_name) if matches else {}

    def get_standings(self, comp_name: str, comp_cfg: dict, season=None) -> dict:
        season = season or season_for_competition(comp_name)
        matches = self._require_matches(comp_name, comp_cfg, season)
        return self._standings_payload(comp_name, season, matches)

    def get_tournament_groups(self, comp_name: str, comp_cfg: dict, season=None):
        season = season or season_for_competition(comp_name)
        matches = self._require_matches(comp_name, comp_cfg, season)
        return (self._standings_payload(comp_name, season, matches),
                {"filters": {"season": str(season)}, "matches": matches})

    # ── Matches ───────────────────────────────────────────────────────────────

    def season_matches(self, comp_name: str, comp_cfg: dict, season: int) -> list[dict]:
        """The competition's season as football-data-shaped match dicts, sorted by
        kickoff; [] when TheSportsDB has nothing (nothing cached, requests failed)."""
        league_id = comp_cfg["tsdb_league_id"]
        key = (league_id, season)
        if key in self._seasons:
            return self._seasons[key]
        league_rounds = int(comp_cfg.get("tsdb_league_rounds", TSDB_DEFAULT_LEAGUE_ROUNDS))
        knockout = {int(k): v for k, v in
                    (comp_cfg.get("tsdb_knockout_rounds") or TSDB_DEFAULT_KNOCKOUT_ROUNDS).items()}
        codes = list(range(1, league_rounds + 1)) + list(knockout)
        cutoff = f"{season}-{comp_cfg.get('tsdb_season_start', TSDB_DEFAULT_SEASON_START)}"
        index = self._teams()
        matches, seen, mapped, unmapped = [], set(), set(), set()
        for code, events in self._season_rounds(league_id, season, codes, cutoff).items():
            stage = "LEAGUE_STAGE" if code <= league_rounds else knockout[code]
            for e in events:
                kickoff = tsdb_kickoff(e)
                event_id = _tsdb_int(e.get("idEvent"))
                if not kickoff or event_id is None or kickoff[:10] < cutoff:
                    continue                 # undated, or a qualifier
                if event_id in seen:
                    continue
                seen.add(event_id)
                status = tsdb_status(e)
                home = self._team(index, e, "Home", mapped, unmapped)
                away = self._team(index, e, "Away", mapped, unmapped)
                matches.append({
                    "id":       TSDB_ID_OFFSET + event_id,
                    "utcDate":  kickoff,
                    "status":   status,
                    "matchday": code if stage == "LEAGUE_STAGE" else None,
                    "stage":    stage,
                    "group":    None,
                    "lastUpdated": None,
                    "homeTeam": home,
                    "awayTeam": away,
                    "score":    tsdb_score(e, status),
                    "tsdbEventId": str(event_id),
                })
        _number_legs(matches)
        matches.sort(key=lambda m: (m["utcDate"], m["id"]))
        if matches:
            log.info(f"{comp_name}: {len(matches)} TheSportsDB match(es) for season "
                     f"{season}; teams matched to football-data: {len(mapped)}, "
                     f"TheSportsDB-only: {len(unmapped)}")
        self._seasons[key] = matches
        return matches

    def _require_matches(self, comp_name, comp_cfg, season) -> list[dict]:
        matches = self.season_matches(comp_name, comp_cfg, season)
        if not matches:
            # Raised so the job keeps its previous file instead of writing an empty one.
            raise RuntimeError(f"no TheSportsDB data for {comp_name} {tsdb_season(season)}")
        return matches

    def _teams(self) -> TsdbTeamIndex:
        if self._team_index is None:
            self._team_index = TsdbTeamIndex(load_known_fd_teams())
        return self._team_index

    @staticmethod
    def _team(index, event, side, mapped, unmapped) -> dict:
        name = (event.get(f"str{side}Team") or "").strip()
        fd = index.fd_team(name)
        if fd is not None:
            mapped.add(fd["id"])
            return {
                "id":        fd["id"],
                "name":      fd.get("name", ""),
                "shortName": fd.get("shortName") or fd.get("name", ""),
                "tla":       fd.get("tla", ""),
                "crest":     override_crest(fd["id"], fd.get("crest", "")),
            }
        tsdb_id = _tsdb_int(event.get(f"id{side}Team"))
        unmapped.add(tsdb_id)
        return {
            "id":        TSDB_ID_OFFSET + tsdb_id if tsdb_id is not None else None,
            "name":      index.canonical_name(name) or name,
            "shortName": name,
            "tla":       "",
            "crest":     event.get(f"str{side}TeamBadge") or "",
        }

    def _standings_payload(self, comp_name, season, matches) -> dict:
        return {
            "competition": {"name": comp_name},
            "filters":     {"season": str(season)},
            "standings":   [{"stage": "LEAGUE_STAGE", "type": "TOTAL",
                             "group": "League phase",
                             "table": compute_league_table(matches)}],
        }

    # ── Rounds: on-disk cache + requests ──────────────────────────────────────

    def _season_rounds(self, league_id, season, codes, cutoff) -> dict[int, list]:
        """{round code: [raw event]} with every due round re-requested
        (tsdb_round_due) and the cache file rewritten when anything changed. A
        failed request keeps that round's cached events. Qualifiers (kickoff
        before [cutoff], "YYYY-MM-DD") and empty fields are not cached."""
        now = self._now() if self._now else datetime.now(timezone.utc)
        cache = load_json_file(self._cache_path) or {}
        rounds = cache.setdefault(str(league_id), {}).setdefault(str(season), {})
        changed, deferred = False, 0
        for code in codes:
            if not tsdb_round_due(rounds.get(str(code)), now):
                continue
            if self._rate_limited or self._requests >= TSDB_MAX_REQUESTS_PER_RUN:
                deferred += 1
                continue
            events = self._get_round(league_id, code, season)
            if events is None:
                continue
            rounds[str(code)] = {
                "last_updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "events": [
                    {k: e[k] for k in _TSDB_EVENT_FIELDS if e.get(k) not in (None, "")}
                    for e in events
                    if str(e.get("idLeague") or league_id) == str(league_id)
                    and (tsdb_kickoff(e) or "9")[:10] >= cutoff],
            }
            changed = True
        if deferred:
            why = "rate-limited" if self._rate_limited else                 f"{TSDB_MAX_REQUESTS_PER_RUN}-request cap reached"
            log.info(f"TheSportsDB: {why} - {deferred} due round(s) of league "
                     f"{league_id} left for the next run")
        if changed:
            write_json_atomic(self._cache_path, cache)
        return {code: (rounds.get(str(code)) or {}).get("events") or [] for code in codes}

    def _get_round(self, league_id, code, season) -> list | None:
        """One eventsround.php request; [] for an empty round, None on failure
        (logged without the URL, which holds the key)."""
        url = (f"{self._base_url}/{self._key}/eventsround.php"
               f"?id={league_id}&r={code}&s={tsdb_season(season)}")
        for attempt in (1, 2):
            if self._requests:
                time.sleep(TSDB_SLEEP_SECONDS)
            self._requests += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "refoot-pipeline"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = json.loads(resp.read() or b"null")
                return list((body or {}).get("events") or [])
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 1:
                    print(f"TheSportsDB rate-limited (429) - retrying in "
                          f"{TSDB_RATE_LIMIT_BACKOFF_SECONDS} s", file=sys.stderr)
                    time.sleep(TSDB_RATE_LIMIT_BACKOFF_SECONDS)
                    continue
                if e.code == 429:
                    self._rate_limited = True
                log.warning(f"TheSportsDB HTTP {e.code} for league {league_id} round "
                            f"{code} - keeping the cached round")
                return None
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
                log.warning(f"TheSportsDB request failed for league {league_id} round "
                            f"{code} ({type(e).__name__}) - keeping the cached round")
                return None
        return None


def _number_legs(matches: list) -> None:
    """Set matchday = leg (1, 2) on two-legged knockout matches: the ties' games
    in kickoff order. Single-leg stages (final) keep matchday None, and a tie
    with more than two games (a data error) is left without legs."""
    ties: dict = {}
    for m in matches:
        if m["stage"] == "LEAGUE_STAGE" or m["stage"] in _SINGLE_LEG_STAGES:
            continue
        teams = frozenset((m["homeTeam"].get("id"), m["awayTeam"].get("id")))
        ties.setdefault((m["stage"], teams), []).append(m)
    for games in ties.values():
        if len(games) > 2:
            log.warning(f"TheSportsDB: {len(games)} games in one {games[0]['stage']} "
                        f"tie - legs not numbered")
            continue
        for leg, m in enumerate(sorted(games, key=lambda g: g["utcDate"]), 1):
            m["matchday"] = leg


# ── Provider registry ─────────────────────────────────────────────────────────

# COMPETITION_CONFIG "provider" value -> provider class. A new data source is a
# FixtureProvider subclass registered here; the job scripts need no change.
PROVIDERS: dict[str, type] = {
    PROVIDER_FOOTBALLDATA: FootballDataProvider,
    PROVIDER_APISPORTS:    ApiSportsProvider,
    PROVIDER_THESPORTSDB:  TheSportsDBProvider,
}


class UnknownProviderError(ValueError):
    """A competition names a provider that is not in PROVIDERS."""


def provider_class(name: str, competition: str = "?") -> type:
    """The registered class for [name]; UnknownProviderError naming the
    competition and the value otherwise."""
    try:
        return PROVIDERS[name]
    except (KeyError, TypeError):
        raise UnknownProviderError(
            f"Competition {competition!r} names unknown provider {name!r} "
            f"(known providers: {', '.join(sorted(PROVIDERS))})"
        ) from None


def get_provider(name: str, config: dict, *, competition: str = "?",
                 instances: dict | None = None) -> FixtureProvider:
    """The provider instance for a COMPETITION_CONFIG "provider" value.

    [config] is the run config handed to the provider's from_config (e.g.
    {"fd_key": ...}). Pass the same [instances] dict for a whole run to share one
    instance per provider (FootballDataProvider caches its raw responses).
    """
    cls = provider_class(name, competition)
    if instances is not None and name in instances:
        return instances[name]
    provider = cls.from_config(config)
    if instances is not None:
        instances[name] = provider
    return provider


def iter_job_providers(job: str, capability: str, config: dict, *,
                       report=None, instances: dict | None = None):
    """Yield (comp_name, comp_cfg, provider) for every enabled competition in
    [job] (COMPETITION_CONFIG order) whose provider offers [capability].

    Skipped with one [report] line (default: log.info), never an error:
      - a SCHEDULED_JOBS_ONLY provider (API-Sports) - checked before the provider
        is built, so the generic jobs never construct or call it;
      - a provider without [capability].
    An unknown provider value raises UnknownProviderError.
    """
    report = report or log.info
    if instances is None:
        instances = {}
    for comp_name, comp_cfg in competitions_for_job(job):
        name = comp_cfg.get("provider")
        cls = provider_class(name, comp_name)
        if cls.SCHEDULED_JOBS_ONLY:
            report(f"{comp_name}: provider {name!r} is only called from its own "
                   f"scheduled jobs - skipped by {job}")
            continue
        if not cls.supports(capability):
            report(f"{comp_name}: provider {name!r} has no {capability!r} "
                   f"capability - skipped")
            continue
        yield comp_name, comp_cfg, get_provider(
            name, config, competition=comp_name, instances=instances)


# ── ApisportsQuotaTracker ─────────────────────────────────────────────────────


class ApisportsQuotaTracker:
    """
    Persists API-Sports daily call count to highlights/apisports-quota-tracker.json.

    Kept strictly separate from highlights/quota-tracker.json (YouTube quota) --
    the two budgets are independent and must never be merged.
    """

    def __init__(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data  = load_json_file(APISPORTS_QUOTA_PATH) or {}
        if data.get("date") != today:
            self.date       = today
            self.calls_used = 0
        else:
            self.date       = data["date"]
            self.calls_used = int(data.get("calls_used", 0))
        self._save()

    def increment(self) -> None:
        """Increment call count, persist to disk, and raise RuntimeError if cap hit."""
        self.calls_used += 1
        self._save()
        if self.calls_used > APISPORTS_DAILY_CAP:
            raise RuntimeError(
                f"API-Sports daily cap reached: {self.calls_used}/{APISPORTS_DAILY_CAP}"
            )

    @property
    def remaining(self) -> int:
        return max(0, APISPORTS_DAILY_CAP - self.calls_used)

    def _save(self) -> None:
        write_json_atomic(APISPORTS_QUOTA_PATH, {
            "date":         self.date,
            "calls_used":   self.calls_used,
            "last_updated": utc_now_iso(),
        })
