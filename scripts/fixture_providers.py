#!/usr/bin/env python3
"""
scripts/fixture_providers.py

Pluggable fixture provider layer.

FixtureProvider         -- minimal base class; subclasses implement get_fixtures()
FootballDataProvider    -- football-data.org (all existing FD competitions)
ApiSportsProvider       -- API-Sports free tier (Copa America 2024; EL parked)
ApisportsQuotaTracker   -- daily call-count tracker (highlights/apisports-quota-tracker.json)

APISPORTS_COMPETITIONS  -- config-driven registry; adding a competition is a one-entry
                           config change with no code modification needed.

Free-tier constraint:
    API-Sports free plan covers seasons 2022-2024 only.  Seasons 2025+ return a paywall
    error.  The free tier is a historical backfill source only -- it cannot cover the
    current season.  Live/current-season coverage requires a paid plan.
"""

import logging
import os
import time
from datetime import datetime, timezone

import requests

from highlights_common import (
    FD_BASE,
    FD_SLEEP_SECONDS,
    HIGHLIGHTS_DIR,
    fd_get,
    load_json_file,
    season_for_competition,
    stage_to_file_stem,
    utc_now_iso,
    write_json_atomic,
)

log = logging.getLogger(__name__)

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


class FixtureProvider:
    def get_fixtures(self, *args, **kwargs) -> dict:
        raise NotImplementedError


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

    def __init__(self, fd_key: str) -> None:
        self._fd_key    = fd_key
        self._raw_cache: dict[tuple, list] = {}  # (code, season) → raw match list

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
                f"{FD_BASE}/competitions/{code}/matches",
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
                "matchday":   matchday,
                "stage":      stage,
            })

        total = sum(len(v) for v in by_stem.values())
        log.info(
            f"{comp_name}: {total} finished fixture(s) across {len(by_stem)} file stem(s)"
        )
        return by_stem

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
                    "crest":     home.get("crest", ""),
                },
                "awayTeam": {
                    "id":        away.get("id"),
                    "name":      away.get("name", ""),
                    "shortName": away.get("shortName") or away.get("name", ""),
                    "tla":       away.get("tla", ""),
                    "crest":     away.get("crest", ""),
                },
                "score": {
                    "fullTime": {
                        "home": ft.get("home"),   # None when unplayed
                        "away": ft.get("away"),
                    }
                },
                "status":  status,
                "utcDate": utc_str,
            })
        result.sort(key=lambda x: (x["matchday"], x.get("utcDate", "")))
        finished = sum(1 for r in result if r["status"] == "FINISHED")
        log.info(
            f"{comp_name}: {len(result)} fixture(s) for season {season} artifact "
            f"({finished} FINISHED, {len(result) - finished} non-finished)"
        )
        return result


# ── ApiSportsProvider ─────────────────────────────────────────────────────────


class ApiSportsProvider(FixtureProvider):
    """
    Fetches fixtures from API-Sports (free tier, historical backfill only).

    Security: never hardcode APISPORTS_API_KEY -- env var only, sourced from a
    GitHub Actions secret.  Do not use for live / current-season data; free
    tier covers 2022-2024 only.
    """

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
