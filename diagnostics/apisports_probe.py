#!/usr/bin/env python3
"""
diagnostics/apisports_probe.py

Read-only diagnostic: confirms whether Copa America and UEFA Europa League
target seasons are available on the API-Sports free tier and captures their
real round-string formats for later normalization.

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

DIAGNOSTICS_DIR = Path(__file__).resolve().parent
REPORT_PATH     = DIAGNOSTICS_DIR / "apisports_probe_report.md"

# Search targets.  canon_includes / canon_excludes drive the heuristic that
# auto-selects the canonical league entry for the fixtures probe.  All matches
# are still printed so the user can verify the selection.
TARGETS = [
    {
        "label":          "Copa America",
        "search":         "copa america",
        "canon_includes": ["copa america"],
        "canon_excludes": [],
    },
    {
        "label":          "UEFA Europa League",
        "search":         "europa league",
        # Exclude Conference League, qualification rounds, defunct variants
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
# HTTP helper
# ──────────────────────────────────────────────────────────────────────────────

def _get(path: str, params: Optional[Dict] = None) -> dict:
    """
    GET BASE_URL/path with x-apisports-key auth.

    - Enforces INTER_REQUEST_SLEEP between consecutive calls.
    - Logs x-ratelimit-requests-remaining (daily) and X-RateLimit-Remaining
      (per-minute) from every response header.
    - Raises RuntimeError on non-200 HTTP status.
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
    if errors:   # non-empty dict or non-empty list means an API-level error
        raise RuntimeError(f"API-Sports returned errors: {errors}")

    return body


# ──────────────────────────────────────────────────────────────────────────────
# League-selection heuristic
# ──────────────────────────────────────────────────────────────────────────────

def _pick_canonical(
    leagues:  List[dict],
    includes: List[str],
    excludes: List[str],
) -> Optional[dict]:
    """
    From a /leagues?search= result set pick the most likely canonical entry:
      1. league.name (lower) contains every string in `includes`.
      2. league.name (lower) contains none of the strings in `excludes`.
      3. Among survivors pick the entry whose most-recent season year is newest.

    Returns None if nothing passes the filter.
    """
    candidates = []
    for entry in leagues:
        name_lower = entry.get("league", {}).get("name", "").lower()
        if not all(t in name_lower for t in includes):
            continue
        if any(t in name_lower for t in excludes):
            continue
        seasons = entry.get("seasons", [])
        if not seasons:
            continue
        max_year = max(s.get("year", 0) for s in seasons)
        candidates.append((max_year, entry))

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
    _out("## 3. Fixtures probe (most-recent season per canonical league)")
    _out()
    _out(
        "> **Review required for Europa League:** `/leagues?search=europa+league`"
        " can return Conference League, qualification rounds, and defunct cups."
        " The script auto-selects the most likely canonical entry (reason shown"
        " below) — verify against section 2 before treating round strings as final."
    )
    _out()
    _out(
        "> **Copa America season-year note:** API-Sports uses the calendar year"
        " of the tournament (e.g. 2024 = Copa America 2024, June–July 2024),"
        " not a split-year notation like European leagues.  Confirm start/end"
        " dates below against the competition calendar."
    )
    _out()

    probe_results: List[dict] = []

    for target in TARGETS:
        label    = target["label"]
        includes = target["canon_includes"]
        excludes = target["canon_excludes"]
        leagues  = all_leagues.get(label, [])

        _out(f"### {label}")
        _out()

        canonical = _pick_canonical(leagues, includes, excludes)

        if canonical is None and leagues:
            _out(
                f"*Heuristic found no match after applying excludes "
                f"({', '.join(excludes) or 'none'}) — falling back to first "
                f"result with seasons.*"
            )
            for entry in leagues:
                if entry.get("seasons"):
                    canonical = entry
                    break

        if canonical is None:
            _out(f"**SKIPPED** — no usable league entry found for {label!r}.")
            probe_results.append({"label": label, "status": "SKIPPED — no usable league"})
            _out()
            continue

        lg      = canonical["league"]
        seasons = canonical.get("seasons", [])
        most_recent = max(seasons, key=lambda s: s.get("year", 0))

        lg_id   = lg["id"]
        lg_name = lg["name"]
        season  = most_recent["year"]
        s_start = most_recent.get("start", "?")
        s_end   = most_recent.get("end",   "?")

        excl_note = ""
        if excludes:
            excl_note = "  *(excludes: " + ", ".join(f"`{e}`" for e in excludes) + ")*"

        _out(f"**Auto-selected:** id={lg_id}  {lg_name!r}{excl_note}")
        _out(f"**Most-recent season:** {season}  ({s_start} → {s_end})")
        _out()

        fix_body = _get("/fixtures", {"league": lg_id, "season": season})
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
