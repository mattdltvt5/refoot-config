#!/usr/bin/env python3
"""
verify_playlist_owners.py

Resolve each PL-prefixed playlist ID in sources.json against the YouTube API
and confirm that the owning channel matches the broadcaster label the ID is
filed under in sources.json.

Exit codes:
  0 — all IDs verified and owners match their labels
  1 — one or more mismatches, unresolvable IDs, or missing API key

Owner-verification is gated to new/changed IDs only: IDs already recorded in
highlights/playlist-owners.json are not re-fetched (quota conserved).  The
label-vs-owner comparison runs on every invocation for all IDs, cached or not,
so a label rename in sources.json is caught without a re-fetch.

Each new-ID fetch costs 1 quota unit (playlists.list?part=snippet) counted
against highlights/quota-tracker.json alongside all other YouTube API calls.
"""

import os
import sys
from pathlib import Path

# Allow running from repo root or from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlights_common import (
    BACKFILL_CAP,
    QuotaTracker,
    log,
    verify_playlist_owners,
)


def main() -> None:
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    quota = QuotaTracker()

    errors = verify_playlist_owners(api_key, quota=quota, quota_cap=BACKFILL_CAP)

    if errors:
        log.error("Playlist owner verification FAILED — %d issue(s):", len(errors))
        for e in errors:
            log.error("  ✗ %s", e)
        sys.exit(1)

    log.info(
        "Playlist owner verification passed — all owners match their broadcaster labels."
    )


if __name__ == "__main__":
    main()
