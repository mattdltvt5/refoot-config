#!/usr/bin/env python3
"""
diagnostics/apisports_probe.py

Read-only diagnostic: confirms whether Copa America and UEFA Europa League
target seasons are available on the API-Sports free tier (2022–2024 only) and
captures the exact `league.round` strings the API returns.

Usage
-----
    APISPORTS_API_KEY=<key> python diagnostics/apisports_probe.py

Constraints
-----------
- Manual-only. Never wired into any GitHub Actions workflow.
- Always commit with [skip ci] — this file must never trigger a fetch run.
- Does NOT import or call any existing pipeline module.
- Throttles to 1 call per 7 s (~8/min, safely under the 10/min free-tier cap).
- 5 total API calls: /status, 2x /leagues?search=, 2x /fixtures.
- Aborts early if daily quota is critically low (< 10 calls remaining).
- Writes a Markdown report to diagnostics/apisports_probe_report.md.
"""

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

BASE_URL            = "https://v3.football.api-sports.io"
INTER_REQUEST_SLEEP = 7      # seconds — keeps bursts safely under 10 req/min
MIN_DAILY_REMAINING = 10     # abort threshold

# API-Sports free plan covers seasons 2022–2024 only.
# Querying 2025 or 2026 returns a paywall error even on valid leagues.
FREE_TIER_SEASON_MAX = 2024

DIAGNOSTICS_DIR = Path(__file__).resolve().parent
REPORT_PATH     = DIAGNOSTICS_DIR / "apisports_probe_report.md"

# Search targets.
# canonical_id: known league ID — used as primary path; name-filter is fallback only.
# canon_includes / canon_excludes: name-filter heuristic (backup path).
TARGETS = [
    {
        "label":          "Copa America",
        "search":         "copa america",
        "canonical_id":   9,   # men's senior tournament (NOT id 926 Copa America Femenina)
        "canon_includes": ["copa america"],
        "canon_excludes": [],
    },
    {
        "label":          "UEFA Europa League",
        "search":         "europa league",
        "canonical_id":   3,   # main UEL (NOT Conference League, qualification rounds)
        "canon_includes": ["europa league"],
        "canon_excludes": ["conference", "qualification", "qualifying", "play-off",
                           "championship", "reserve", "youth"],
    },
]

# ──────────────────────────────────────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────────────────────────────────────

_api_key:      str       = ""
_calls_made:   int       = 0
_last_call_ts: float     = 0.0
_lines:        List[str] = []   # buffered for the report file


# ──────────────────────────────────────────────────────────────────────────────
# Output helper — prints to stdout AND buffers for the report
# ──────────────────────────────────────────────────────────────────────────────

def _out(line: str = "") -> None:
    print(line, flush=True)
    _lines.append(line)


# ──────────────────────────────────────────────────────────────────────────────
# Custom exception for free-tier paywall
# ──────────────────────────────────────────────────────────────────────────────

class SeasonLockedError(Exception):
    """Raised when API-Sports returns errors.plan (free-tier season paywall)."""
    pass


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helper
# ──────────────────────────────────────────────────────────────────────────────

def _get(path: str, params: Optional[Dict] = None) -> dict:
    """
    GET BASE_URL/path with x-apisports-key auth.

    - Enforces INTER_REQUEST_SLEEP between consecutive calls.
    - Logs x-ratelimit-requests-remaining (daily) and X-RateLimit-Remaining
      (per-minute) from every response header.
    - Raises SeasonLockedError when errors.plan contains "does not have access
      to this season" — expected free-tier paywall, not a bug.
    - Raises RuntimeError on non-200 HTTP status or other API-level errors.
    - A 200 with an empty `response` array is NOT an error; callers handle it.
    """
    global _calls_made, _last_call_ts

    if _last_call_ts:
        elapsed = time.monotonic() - _last_call_ts
        if elapsed < INTER_REQUEST_SLEEP:
            wait = INTER_REQUEST_SLEEP - elapsed
            print(f"  [throttle {wait:.1f}s]", flush=True)
            time.sleep(wait)

    url  = f"{BASE_URL}/{path.lstrip('/')}"
    resp = requests.get(
        url,
        headers={"x-apisports-key": _api_key},
        params=params or {},
        timeout=15,
    )
    _calls_made   += 1
    _last_call_ts  = time.monotonic()

    # requests.headers is a CaseInsensitiveDict so both casings work; keep
    # explicit fallback names for clarity and future-proofing.
    daily_rem  = (resp.headers.get("x-ratelimit-requests-remaining")
                  or resp.headers.get("X-RateLimit-Requests-Remaining")
                  or "?")
    minute_rem = (resp.headers.get("X-RateLimit-Remaining")
                  or resp.headers.get("x-ratelimit-remaining")
                  or "?")
    print(
        f"  [quota] call #{_calls_made}  "
        f"daily_remaining={daily_rem}  per_minute_remaining={minute_rem}",
        flush=True,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from {url}: {resp.text[:300]}")

    body   = resp.json()
    errors = body.get("errors")
    if errors:
        err_str = str(errors).lower()
        if "does not have access to this season" in err_str or "free plans" in err_str:
            raise SeasonLockedError(str(errors))
        raise RuntimeError(f"API-Sports returned errors: {errors}")

    return body


# ──────────────────────────────────────────────────────────────────────────────
# Season helpers
# ──────────────────────────────────────────────────────────────────────────────

def _best_free_season(seasons: List[dict]) -> Optional[dict]:
    """
    Return the newest season with year <= FREE_TIER_SEASON_MAX and
    fixtures.events == True.  Returns None if no eligible season exists.
    """
    eligible = [
        s for s in seasons
        if s.get("year", 9999) <= FREE_TIER_SEASON_MAX
        and s.get("coverage", {}).get("fixtures", {}).get("events") is True
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda s: s.get("year", 0))


# ──────────────────────────────────────────────────────────────────────────────
# League-selection heuristic (fallback path — canonical_id is preferred)
# ──────────────────────────────────────────────────────────────────────────────

# Variants to exclude during name-based selection.
_GENDER_AGE_EXCLUDES = {"femenina", "women", "feminine", "u21", "u20", "u19"}

def _pick_canonical(
    leagues:  List[dict],
    includes: List[str],
    excludes: List[str],
) -> Optional[dict]:
    """
    From a /leagues?search= result set pick the most likely canonical entry:
      1. league.name (lower) contains every string in `includes`.
      2. league.name (lower) contains none of the strings in `excludes` or
         _GENDER_AGE_EXCLUDES (women's/youth variants).
      3. Among survivors pick the entry whose most-recent free-tier season
         (year <= FREE_TIER_SEASON_MAX, fixtures.events == True) is newest.

    Returns None if nothing passes the filter.
    """
    all_excludes = set(excludes) | _GENDER_AGE_EXCLUDES
    candidates = []
    for entry in leagues:
        name_lower = entry.get("league", {}).get("name", "").lower()
        if not all(t in name_lower for t in includes):
            continue
        if any(t in name_lower for t in all_excludes):
            continue
        best = _best_free_season(entry.get("seasons", []))
        if best is None:
            continue
        candidates.append((best["year"], entry))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ──────────────────────────────────────────────────────────────────────────────
# Report writer
# ──────────────────────────────────────────────────────────────────────────────

def _write_report() -> None:
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_lines))
        fh.write("\n")
    print(f"\nReport written → {REPORT_PATH}", flush=True)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global _api_key

    # ── 0. Key ────────────────────────────────────────────────────────────────
    _api_key = os.environ.get("APISPORTS_API_KEY", "").strip()
    if not _api_key:
        sys.exit(
            "ERROR: APISPORTS_API_KEY environment variable is not set.\n"
            "Usage: APISPORTS_API_KEY=<key> python diagnostics/apisports_probe.py"
        )

    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M UTC")

    _out(f"# API-Sports Probe Report — {run_ts}")
    _out()
    _out("**Read-only diagnostic** — no pipeline file, config, or quota tracker is modified.")
    _out()
    _out(f"> **Free-tier constraint:** API-Sports free plan covers seasons"
         f" 2022–{FREE_TIER_SEASON_MAX} only.  Seasons"
         f" {FREE_TIER_SEASON_MAX + 1}+ return a paywall error and are never queried.")
    _out()

    # ── 1. /status ────────────────────────────────────────────────────────────
    _out("---")
    _out("## 1. Quota check (`/status`)")
    _out()

    status_body = _get("/status")
    resp_block  = status_body.get("response", {})
    if isinstance(resp_block, list):
        resp_block = resp_block[0] if resp_block else {}
    req_block = resp_block.get("requests", {})

    current   = req_block.get("current",   "?")
    limit_day = req_block.get("limit_day", "?")

    _out(f"- `requests.current`   : **{current}**")
    _out(f"- `requests.limit_day` : **{limit_day}**")

    try:
        estimated_remaining = int(limit_day) - int(current)
    except (TypeError, ValueError):
        estimated_remaining = 999   # unknown — proceed and let caller judge

    _out(f"- Estimated remaining  : **{estimated_remaining}**")
    _out()

    if estimated_remaining < MIN_DAILY_REMAINING:
        _out(
            f"> **ABORTED** — only {estimated_remaining} daily calls estimated remaining "
            f"(threshold: {MIN_DAILY_REMAINING}).  Re-run after 00:00 UTC reset."
        )
        _write_report()
        sys.exit(1)

    # ── 2. League discovery ───────────────────────────────────────────────────
    _out("---")
    _out("## 2. League discovery")
    _out()

    all_leagues: Dict[str, List[dict]] = {}

    for idx, target in enumerate(TARGETS, start=1):
        label  = target["label"]
        search = target["search"]

        _out(f"### 2.{idx} `{search}` → {label}")
        _out()

        body    = _get("/leagues", {"search": search})
        leagues = body.get("response", [])
        _out(f"**{len(leagues)} result(s) returned**")
        _out()

        for entry in leagues:
            lg      = entry.get("league", {})
            ctry    = entry.get("country", {})
            seasons = entry.get("seasons", [])

            _out(
                f"#### id={lg.get('id')}  {lg.get('name')!r}"
                f"  (type={lg.get('type')!r}, country={ctry.get('name')!r})"
            )
            _out()

            if not seasons:
                _out("*(no seasons listed)*")
                _out()
                continue

            _out(f"Seasons ({len(seasons)} total):")
            _out()
            _out("| year | start | end | current | fixtures.events |")
            _out("|------|-------|-----|---------|-----------------|")
            for s in sorted(seasons, key=lambda x: x.get("year", 0), reverse=True):
                cov = s.get("coverage", {}).get("fixtures", {})
                _out(
                    f"| {s.get('year')} "
                    f"| {s.get('start', '?')} "
                    f"| {s.get('end', '?')} "
                    f"| {s.get('current')} "
                    f"| {cov.get('events')} |"
                )
            _out()

        all_leagues[label] = leagues

    # ── 3. Fixtures probe ─────────────────────────────────────────────────────
    _out("---")
    _out("## 3. Fixtures probe (newest free-tier season per canonical league)")
    _out()
    _out(
        f"> **Free-tier window: 2022–{FREE_TIER_SEASON_MAX}.**"
        f"  Only seasons within this range are queried.  Current seasons"
        f" ({FREE_TIER_SEASON_MAX + 1}+) require a paid plan."
    )
    _out()
    _out(
        "> **Canonical IDs are pinned** (id=9 for Copa America, id=3 for Europa League)."
        "  Name-filter heuristic is used only if the pinned ID is absent from search results."
    )
    _out()

    probe_results: List[dict] = []

    for target in TARGETS:
        label        = target["label"]
        includes     = target["canon_includes"]
        excludes     = target["canon_excludes"]
        canonical_id = target.get("canonical_id")
        leagues      = all_leagues.get(label, [])

        _out(f"### {label}")
        _out()

        # Primary path: look up by canonical_id in search results
        canonical = None
        if canonical_id is not None:
            for entry in leagues:
                if entry.get("league", {}).get("id") == canonical_id:
                    canonical = entry
                    break
            if canonical is None:
                _out(
                    f"*Pinned id={canonical_id} not found in `/leagues?search={target['search']}`"
                    f" results — falling back to name-filter heuristic.*"
                )

        # Fallback: name-based selection (excludes women's/youth variants)
        if canonical is None:
            canonical = _pick_canonical(leagues, includes, excludes)

        if canonical is None and leagues:
            _out(
                f"*Name-filter found no match after applying excludes"
                f" ({', '.join(excludes) or 'none'} + gender/age variants)"
                f" — falling back to first result with eligible seasons.*"
            )
            for entry in leagues:
                if _best_free_season(entry.get("seasons", [])) is not None:
                    canonical = entry
                    break

        if canonical is None:
            _out(f"**SKIPPED** — no usable league entry found for {label!r}.")
            probe_results.append({"label": label, "status": "SKIPPED — no usable league"})
            _out()
            continue

        lg      = canonical["league"]
        seasons = canonical.get("seasons", [])

        lg_id   = lg["id"]
        lg_name = lg["name"]

        best_season = _best_free_season(seasons)
        if best_season is None:
            _out(
                f"**SKIPPED** — no season with year ≤ {FREE_TIER_SEASON_MAX}"
                f" and fixtures.events=True found for {lg_name!r} (id={lg_id})."
            )
            probe_results.append({
                "label":  label,
                "status": f"SKIPPED — no free-tier eligible season (≤{FREE_TIER_SEASON_MAX})",
            })
            _out()
            continue

        season  = best_season["year"]
        s_start = best_season.get("start", "?")
        s_end   = best_season.get("end",   "?")

        excl_note = ""
        if excludes:
            excl_note = "  *(excludes: " + ", ".join(f"`{e}`" for e in excludes) + " + gender/age variants)*"

        _out(f"**Selected:** id={lg_id}  {lg_name!r}{excl_note}")
        _out(f"**Target season:** {season}  ({s_start} → {s_end})  *(newest ≤ {FREE_TIER_SEASON_MAX} with coverage)*")
        _out()

        try:
            fix_body = _get("/fixtures", {"league": lg_id, "season": season})
        except SeasonLockedError as exc:
            _out(f"> **SEASON LOCKED (free tier)** — season {season} is paywalled.")
            _out(f"> API error: `{exc}`")
            _out(f"> This should not happen when season ≤ {FREE_TIER_SEASON_MAX} — check coverage flags in section 2.")
            probe_results.append({
                "label":         label,
                "league_id":     lg_id,
                "league_name":   lg_name,
                "season":        season,
                "season_span":   f"{s_start} → {s_end}",
                "status":        f"SEASON LOCKED (free tier) — season {season}",
                "round_strings": [],
                "fixture_count": 0,
            })
            _out()
            continue

        fix_list = fix_body.get("response", [])
        total    = len(fix_list)

        if total == 0:
            _out(
                f"> **NOT COVERED on free tier** — HTTP 200 but 0 fixtures returned"
                f" for league id={lg_id}, season={season}."
            )
            _out(
                f"> Try an earlier season year if coverage is expected"
                f" (check section 2 for available years)."
            )
            probe_results.append({
                "label":         label,
                "league_id":     lg_id,
                "league_name":   lg_name,
                "season":        season,
                "season_span":   f"{s_start} → {s_end}",
                "status":        f"NOT COVERED (0 fixtures for season {season})",
                "round_strings": [],
                "fixture_count": 0,
            })
            _out()
            continue

        # Collect distinct league.round strings preserving insertion order
        seen: Dict[str, None] = {}
        for fix in fix_list:
            rnd = fix.get("league", {}).get("round", "")
            if rnd:
                seen[rnd] = None
        rounds = list(seen.keys())

        _out(f"**Fixture count:** {total}")
        _out(f"**Distinct `league.round` strings** ({len(rounds)}) — in order of first appearance:")
        _out()
        for r in rounds:
            _out(f"- `{r}`")
        _out()

        probe_results.append({
            "label":         label,
            "league_id":     lg_id,
            "league_name":   lg_name,
            "season":        season,
            "season_span":   f"{s_start} → {s_end}",
            "status":        "COVERED",
            "round_strings": rounds,
            "fixture_count": total,
        })

    # ── 4. Summary ────────────────────────────────────────────────────────────
    _out("---")
    _out("## 4. Summary")
    _out()
    _out(f"**Total API-Sports calls this run: {_calls_made}**")
    _out()
    _out(
        f"**Free-tier coverage window: 2022–{FREE_TIER_SEASON_MAX}.**"
        f"  Seasons {FREE_TIER_SEASON_MAX + 1}+ require a paid plan."
        f"  The free tier is a historical backfill source only — it cannot cover the current season."
    )
    _out()
    _out("| Competition | League ID | Season | Span | Status | Fixture count |")
    _out("|-------------|-----------|--------|------|--------|---------------|")
    for p in probe_results:
        _out(
            f"| {p['label']} "
            f"| {p.get('league_id', '—')} "
            f"| {p.get('season', '—')} "
            f"| {p.get('season_span', '—')} "
            f"| {p.get('status', '?')} "
            f"| {p.get('fixture_count', '—')} |"
        )
    _out()
    _out("### Round strings by competition")
    _out()
    for p in probe_results:
        _out(f"**{p['label']}** (league id={p.get('league_id', '?')}, season {p.get('season', '?')}):")
        if p.get("round_strings"):
            for r in p["round_strings"]:
                _out(f"- `{r}`")
        else:
            _out("- *(no round data)*")
        _out()

    _write_report()


if __name__ == "__main__":
    main()
