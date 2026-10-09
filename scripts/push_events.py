#!/usr/bin/env python3
"""Detect push-notification events for favourite teams (LOG-ONLY for now).

Runs at the end of every ~5-minute fetch-highlights run, after all artifacts
have been rewritten and before the commit. It compares the match state the
pipeline just built (``build_home_index.build_index()`` in memory - leagues
plus UCL / World Cup / Euro) with the state committed at ``HEAD``
(``home-index/{YYYY-MM}.json``) and turns the differences into events:

  * ``goal``       - a match's total score went up (live, or just finished).
  * ``disallowed`` - a match's total score went DOWN (VAR / data correction).
  * ``final``      - FINISHED with the same score for FINAL_SETTLE (15 min).
                     Final scores are sometimes corrected 5-15 minutes after
                     the whistle (e.g. 2-0 -> 1-0): replaying 19-20 Sep 2026,
                     3 of 43 results changed within ~10 min of full time. The
                     first time a final score is seen is recorded in the ledger
                     (an internal ``{id}:ft:{h}-{a}`` mark); a correction
                     restarts the wait.
  * ``highlights`` - a highlight video was attached to the match.

Every event has a stable ``key``; keys already in ``push-events/ledger.json``
are never emitted again (a failed push, a lagging artifact or a score flicker
can't produce duplicates). Only recent matches are considered, so a backfill
of old data can't flood users.

``--mode log`` (the default and, for now, the only mode): print the events and
append them to ``push-events/events-{YYYY-MM}.jsonl`` with timing data, so a
real matchday can be measured before anything is sent. Nothing is sent.

Never fails the pipeline: any error is logged and the script exits 0.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS_DIR = ROOT / "push-events"
LEDGER_PATH = EVENTS_DIR / "ledger.json"

LIVE = frozenset({"IN_PLAY", "PAUSED"})
FINISHED = "FINISHED"

# Only matches that kicked off within these windows produce events.
GOAL_WINDOW = timedelta(hours=6)        # goals / corrections / final result
HIGHLIGHTS_WINDOW = timedelta(hours=72)  # clips can arrive many hours later
# How long a final score must stay unchanged before the result is announced.
FINAL_SETTLE = timedelta(minutes=15)
# Ledger keys older than this are pruned (well past every window).
LEDGER_TTL = timedelta(days=4)

log = logging.getLogger("push_events")


# ── Pure core ───────────────────────────────────────────────────────────────────


def flatten(months: dict) -> dict[int, dict]:
    """{match_id: match (+ 'competition')} from a home-index month map.

    Accepts both the in-memory ``build_index()`` shape
    ``{ym: {date: [groups]}}`` and the file shape ``{ym: {'dates': {...}}}``.
    """
    out: dict[int, dict] = {}
    for month in months.values():
        dates = month.get("dates", month) if isinstance(month, dict) else {}
        for groups in dates.values():
            if not isinstance(groups, list):
                continue
            for g in groups:
                for m in g.get("matches", []):
                    mid = m.get("match_id")
                    if mid is not None:
                        out[mid] = {**m, "competition": g.get("competition")}
    return out


def _parse(utc: str | None) -> datetime | None:
    if not utc:
        return None
    try:
        return datetime.fromisoformat(utc.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _total(m: dict | None) -> int | None:
    if not m:
        return None
    h, a = m.get("homeScore"), m.get("awayScore")
    if h is None or a is None:
        return None
    return h + a


def _event(kind: str, key: str, m: dict, now: datetime) -> dict:
    ko = _parse(m.get("utcDate"))
    ev = {
        "key": key,
        "type": kind,
        "match_id": m.get("match_id"),
        "competition": m.get("competition"),
        "home": {"id": (m.get("homeTeam") or {}).get("id"),
                 "name": (m.get("homeTeam") or {}).get("name")},
        "away": {"id": (m.get("awayTeam") or {}).get("id"),
                 "name": (m.get("awayTeam") or {}).get("name")},
        "score": [m.get("homeScore"), m.get("awayScore")],
        "status": m.get("status"),
        "kickoff": m.get("utcDate"),
        "detected_at": _iso(now),
        # Minutes since kickoff - for measuring latency on a real matchday.
        "minutes_after_kickoff": round((now - ko).total_seconds() / 60) if ko else None,
    }
    if kind == "highlights":
        ev["video_id"] = m.get("videoId")
    return ev


def detect(prev: dict[int, dict], curr: dict[int, dict], now: datetime,
           ledger: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
    """Events between two flattened states, plus internal ledger marks (when a
    final score was first seen). Pure: ``ledger`` is only read."""
    events: list[dict] = []
    marks: dict[str, str] = {}
    for mid, m in curr.items():
        ko = _parse(m.get("utcDate"))
        if ko is None or ko > now:
            continue
        age = now - ko
        p = prev.get(mid)
        status = m.get("status")
        h, a = m.get("homeScore"), m.get("awayScore")

        def emit(kind: str, key: str) -> None:
            if key not in ledger:
                events.append(_event(kind, key, m, now))

        if age <= GOAL_WINDOW:
            t_now, t_prev = _total(m), _total(p)
            if t_now is not None and (status in LIVE or status == FINISHED):
                if t_now > (t_prev or 0):
                    emit("goal", f"{mid}:goal:{h}-{a}")
                elif t_prev is not None and t_now < t_prev:
                    emit("disallowed", f"{mid}:disallowed:{h}-{a}")
            # Final: once this final score has been seen unchanged for
            # FINAL_SETTLE (a correction gives a new mark and restarts the wait).
            if status == FINISHED and h is not None and a is not None:
                mark = f"{mid}:ft:{h}-{a}"
                seen = _parse(ledger.get(mark))
                if seen is None:
                    marks[mark] = _iso(now)
                elif now - seen >= FINAL_SETTLE:
                    emit("final", f"{mid}:final")

        if age <= HIGHLIGHTS_WINDOW and m.get("videoId") and not (p or {}).get("videoId"):
            emit("highlights", f"{mid}:highlights")
    events.sort(key=lambda e: (e["kickoff"] or "", e["match_id"] or 0, e["type"]))
    return events, marks


def update_ledger(ledger: dict[str, str], events: list[dict], now: datetime,
                  marks: dict[str, str] | None = None) -> dict[str, str]:
    """Add the events' keys and marks; drop keys older than LEDGER_TTL."""
    out = {k: v for k, v in ledger.items()
           if (_parse(v) or now) > now - LEDGER_TTL}
    out.update(marks or {})
    for e in events:
        out[e["key"]] = e["detected_at"]
    return dict(sorted(out.items()))


# ── I/O ───────────────────────────────────────────────────────────────────────


def _git_show(path: str) -> str | None:
    r = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    return r.stdout if r.returncode == 0 else None


def months_around(now: datetime) -> list[str]:
    """The index months a recent-match window can touch (previous + current)."""
    first = now.replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    return [prev.strftime("%Y-%m"), now.strftime("%Y-%m")]


def load_head_state(months: list[str]) -> dict[int, dict]:
    out: dict[str, dict] = {}
    for ym in months:
        text = _git_show(f"home-index/{ym}.json")
        if text:
            try:
                out[ym] = json.loads(text)
            except ValueError:
                log.warning("HEAD home-index/%s.json unreadable", ym)
    return flatten(out)


def load_current_state(months: list[str]) -> dict[int, dict]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_home_index  # noqa: E402  (pipeline module)

    built, _ = build_home_index.build_index()
    return flatten({ym: built[ym] for ym in months if ym in built})


def load_ledger() -> dict[str, str]:
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_outputs(events: list[dict], ledger: dict[str, str], now: datetime) -> None:
    EVENTS_DIR.mkdir(exist_ok=True)
    if events:
        path = EVENTS_DIR / f"events-{now.strftime('%Y-%m')}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    new = json.dumps(ledger, indent=1, ensure_ascii=False) + "\n"
    old = LEDGER_PATH.read_text(encoding="utf-8") if LEDGER_PATH.exists() else None
    if new != old:
        LEDGER_PATH.write_text(new, encoding="utf-8")


def run(mode: str, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    months = months_around(now)
    prev = load_head_state(months)
    curr = load_current_state(months)
    ledger = load_ledger()
    events, marks = detect(prev, curr, now, ledger)
    for e in events:
        log.info("[%s] %s %s %s-%s %s (%s, +%s min)", mode, e["type"].upper(),
                 e["home"]["name"], *e["score"], e["away"]["name"],
                 e["competition"], e["minutes_after_kickoff"])
    log.info("%d push event(s) detected (%s mode - nothing sent)", len(events), mode)
    write_outputs(events, update_ledger(ledger, events, now, marks), now)
    return events


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["log"], default="log",
                    help="log: record events only (sending comes in a later PR)")
    args = ap.parse_args(argv)
    try:
        run(args.mode)
    except Exception:  # never break the data pipeline
        log.exception("push event detection failed (ignored)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
