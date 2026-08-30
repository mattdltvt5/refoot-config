#!/usr/bin/env python3
"""
scripts/seed_results_ledger.py

One-time / re-runnable seed of the durable results ledger from git history.

Reconstructs results/{slug}/{season}.json for each domestic league by folding
every recent revision of fixtures/{slug}/{season}.json through
update_results_ledger(), oldest → newest.  This recovers FINISHED scores that
upstream football-data.org may currently be serving as unplayed (TIMED/null):
the good results were committed to the cache while they were healthy, and this
script distills them back into an FD-independent ledger.

Behaviour (mirrors the live pipeline's ledger rules exactly):
  - the latest FINISHED score wins (score corrections are captured);
  - a later terminal status (POSTPONED/CANCELLED/SUSPENDED/AWARDED) clears the
    entry (a genuine postponement is not force-restored to FINISHED);
  - a later TIMED/null does NOT downgrade an already-recorded FINISHED result.

Idempotent: merges into any existing ledger and never downgrades a result.
Read-only against git history; writes only results/{slug}/{season}.json.

Usage:
    python scripts/seed_results_ledger.py [--depth N] [--dry-run]
"""
import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlights_common import (  # noqa: E402
    REPO_ROOT,
    RESULTS_DIR,
    COMPETITION_SLUG_MAP,
    DOMESTIC_LEAGUE_COMPS,
    season_for_competition,
    load_json_file,
    write_json_atomic,
    utc_now_iso,
)
from fixture_providers import update_results_ledger  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_results_ledger")

DEFAULT_DEPTH = 400   # ~5-min cadence → ~33h of history; comfortably spans the last good period


def _revisions(rel_path: str, depth: int) -> list[str]:
    """Return commit SHAs that touched rel_path, oldest → newest (capped at depth)."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", f"-{depth}", "--format=%H", "--", rel_path],
        capture_output=True, text=True,
    )
    shas = [s for s in out.stdout.splitlines() if s.strip()]
    return list(reversed(shas))   # oldest first


def _fixtures_at(sha: str, rel_path: str) -> list[dict]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", f"{sha}:{rel_path}"],
        capture_output=True, text=True,
    )
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        return (json.loads(out.stdout) or {}).get("fixtures", []) or []
    except json.JSONDecodeError:
        return []


def seed_one(comp_name: str, depth: int, dry_run: bool) -> tuple[int, int]:
    slug     = COMPETITION_SLUG_MAP[comp_name]
    season   = season_for_competition(comp_name)
    rel_path = f"fixtures/{slug}/{season}.json"
    led_path = RESULTS_DIR / slug / f"{season}.json"

    ledger = (load_json_file(led_path) or {}).get("results", {}) or {}
    before = len(ledger)

    revs = _revisions(rel_path, depth)
    for sha in revs:
        fixtures = _fixtures_at(sha, rel_path)
        if fixtures:
            ledger = update_results_ledger(ledger, fixtures)

    after = len(ledger)
    log.info(
        f"{comp_name:16} {slug}/{season}: scanned {len(revs)} revision(s) → "
        f"{after} ledgered result(s) (+{after - before})"
    )
    if not dry_run:
        led_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(led_path, {
            "competition":  comp_name,
            "season":       season,
            "generated_at": utc_now_iso(),
            "results":      ledger,
        })
    return before, after


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH,
                    help=f"max git revisions to scan per league (default {DEFAULT_DEPTH})")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = ap.parse_args()

    total = 0
    for comp_name in sorted(DOMESTIC_LEAGUE_COMPS):
        _, after = seed_one(comp_name, args.depth, args.dry_run)
        total += after
    log.info(f"{'(dry-run) ' if args.dry_run else ''}Done — {total} total ledgered result(s) across "
             f"{len(DOMESTIC_LEAGUE_COMPS)} leagues.")


if __name__ == "__main__":
    main()
