#!/usr/bin/env python3
"""Decide whether the fetch-highlights run should commit its staged changes.

Every ~5-minute run rewrites its JSON artifacts with fresh run timestamps
(``generated_at``, ``last_run``, ``last_updated``, ``_updated``), so the
working tree ALWAYS differs from HEAD even when no score, fixture or video
changed. Committing that produced ~288 timestamp-only commits a day, and each
push also triggered a GitHub Pages rebuild - most of the repo's Actions usage.

Rules (run after ``git add``, against the index):
  * any staged change other than those timestamp keys -> commit (real data);
  * timestamp-only, but the data's timestamps at HEAD are older than
    ``--heartbeat-hours`` -> commit anyway (heartbeat). The app rejects stale
    caches - league fixtures after 6 h, standings after 2 days, tournaments
    after 7 days - so a quiet spell must still refresh them. 3 h keeps the
    6 h fixtures TTL with a missed tick to spare;
  * otherwise -> skip.

Prints ``commit`` or ``skip`` (plus the reason on stderr). Exit code is always
0 so the workflow branches on the printed word.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

# Run-time stamps rewritten on every run; their change alone is not new data.
VOLATILE_KEYS = {"generated_at", "last_run", "last_updated", "_updated"}


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          encoding="utf-8", check=False)


def strip_volatile(obj):
    """Copy of ``obj`` without VOLATILE_KEYS at any depth."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj


def volatile_timestamps(obj):
    """All parseable VOLATILE_KEYS timestamps in ``obj`` (any depth), as UTC."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in VOLATILE_KEYS and isinstance(v, str):
                try:
                    t = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    out.append(t if t.tzinfo else t.replace(tzinfo=timezone.utc))
                except ValueError:
                    pass
            else:
                out.extend(volatile_timestamps(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(volatile_timestamps(v))
    return out


def decide(changes, now, heartbeat_hours):
    """Pure decision. ``changes`` is a list of (path, head_text, staged_text);
    either text is None when the file is absent on that side.
    Returns (verdict, reason)."""
    if not changes:
        return "skip", "nothing staged"
    head_stamps = []
    for path, head, staged in changes:
        if head is None or staged is None:
            return "commit", f"{path}: added or deleted"
        if not path.endswith(".json"):
            return "commit", f"{path}: non-JSON change"
        try:
            h, s = json.loads(head), json.loads(staged)
        except ValueError:
            return "commit", f"{path}: unparseable JSON"
        if strip_volatile(h) != strip_volatile(s):
            return "commit", f"{path}: data changed"
        head_stamps.extend(volatile_timestamps(h))
    if not head_stamps:
        return "commit", "timestamp-only, but no readable timestamp at HEAD"
    age_h = (now - min(head_stamps)).total_seconds() / 3600
    if age_h >= heartbeat_hours:
        return "commit", f"heartbeat: oldest data timestamp is {age_h:.1f} h old"
    return "skip", f"timestamp-only change (oldest data {age_h:.1f} h old)"


def staged_changes():
    names = _git("diff", "--cached", "--name-only").stdout.split()
    changes = []
    for path in names:
        head = _git("show", f"HEAD:{path}")
        staged = _git("show", f":{path}")
        changes.append((path,
                        head.stdout if head.returncode == 0 else None,
                        staged.stdout if staged.returncode == 0 else None))
    return changes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--heartbeat-hours", type=float, default=3.0)
    args = ap.parse_args(argv)
    verdict, reason = decide(staged_changes(), datetime.now(timezone.utc),
                             args.heartbeat_hours)
    print(reason, file=sys.stderr)
    print(verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
