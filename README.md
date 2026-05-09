# refoot-config

Remote channel configuration for the **ReFoot Highlights** Android app.

## Files

| File | Purpose |
|---|---|
| `sources.json` | YouTube channel/playlist IDs for all tiers (read by the app at runtime) |
| `admin.html` | Browser-based admin panel for managing `sources.json` |
| `uicons/` | Flaticon UIcons Bold Rounded webfont (used by the admin panel) |
| `highlights/` | Pre-built video metadata written by the fetch-highlights Action |
| `scripts/highlights_common.py` | Shared utilities imported by both highlight scripts |
| `scripts/fetch_highlights.py` | Incremental update script (runs every 4 hours) |
| `scripts/backfill_highlights.py` | Full-season backfill script (manual trigger only) |
| `.github/workflows/fetch-highlights.yml` | GitHub Action that runs the incremental script every 4 hours |
| `.github/workflows/backfill-highlights.yml` | GitHub Action for the manual backfill (workflow_dispatch only) |

## Admin Panel

Open the admin panel at:
**https://mattdltvt5.github.io/refoot-config/admin.html**

Requirements:
- GitHub Personal Access Token with **Contents: Read & Write** on this repo

The admin panel uses the [UIcons Bold Rounded](https://www.flaticon.com/uicons) icon set, hosted locally in `uicons/` so it works without an internet connection to Flaticon.

## sources.json schema

```json
{
  "_updated": "YYYY-MM-DD",
  "competitions": {
    "Competition Name": "UCxxxxxxxx"
  },
  "teams": {
    "Exact Team Name": "UCxxxxxxxx"
  },
  "playlists": {
    "Competition Name": {
      "Broadcaster Name": ["PLxxxxxxxx", "PLyyyyyyyy"]
    }
  },
  "teamPlaylists": {
    "Competition Name": {
      "Exact Team Name": "PLxxxxxxxx"
    }
  },
  "teamLists": {
    "Competition Name": ["Team A", "Team B"]
  }
}
```

- Competition names must exactly match those used in the Android app (`competitionName` strings)
- Team names must exactly match football-data.org `team.name` values
- Empty string `""` means "not configured" — the app falls through to the next tier
- `playlists` and `teamPlaylists` are Tier 4 fallbacks; `competitions` and `teams` are Tier 2 / Tier 1
- `playlists` supports multiple playlist IDs per broadcaster (e.g. one per game week); the app queries them all
- `teamLists` is auto-populated weekly by the `sync-teams.yml` GitHub Action (football-data.org)
- `teams` channel IDs are auto-populated weekly by the `sync-channels.yml` GitHub Action (Wikidata)

## Highlights Cache

The highlights pipeline has **two operating modes** that share a single YouTube 10,000 unit/day quota:

| Mode | Script | Trigger | Budget |
|---|---|---|---|
| **Incremental** | `fetch_highlights.py` | Every 4 hours (scheduled) | 8,000 units/day |
| **Backfill** | `backfill_highlights.py` | Manual only (`workflow_dispatch`) | 9,500 units/day |

Both scripts write to the same `highlights/` files and track consumption in `highlights/quota-tracker.json`.

### First-time setup

Before the incremental job can cover the full season, trigger the backfill once manually from the **Actions** tab → **Backfill highlights cache** → **Run workflow**. It will fetch all finished fixtures for the current season. If the daily cap is hit, re-trigger the next day — it resumes exactly where it stopped.

The incremental job runs automatically every 4 hours and keeps new results flowing once the backfill is complete.

### Directory structure

```
highlights/
  premier-league/
    gameweek-36.json
    gameweek-37.json
  laliga/
    gameweek-36.json
  serie-a/
  bundesliga/
  ligue-1/
  ucl/
    matchday-8.json   ← UCL and UEL use "matchday" not "gameweek"
  uel/
    matchday-12.json
```

### Gameweek file schema

```json
{
  "competition": "Premier League",
  "gameweek": 36,
  "generated_at": "2026-05-07T03:00:00Z",
  "matches": [
    {
      "match_id": 12345,
      "home_team": "Arsenal FC",
      "away_team": "Chelsea FC",
      "date": "2026-05-04",
      "videos": [
        {
          "video_id": "dQw4w9WgXcQ",
          "title": "Arsenal vs Chelsea | Highlights",
          "published_at": "2026-05-04",
          "tier_used": 2
        }
      ]
    }
  ]
}
```

- `video_id` only — never a full URL; reconstruct as `https://www.youtube.com/watch?v={video_id}`
- `tier_used` indicates which tier produced the video (1, 2, or 4)
- `generated_at` is always UTC; updated on every write

### Tier priority order

The script tries sources in this order and stops at the **first tier that yields at least one accepted video** for the fixture:

| Priority | Tier | Source |
|---|---|---|
| 1st | 1c | `teamPlaylists[competition][home_team]` — competition-scoped team playlist |
| 2nd | 1d | `teamPlaylists[competition][away_team]` — competition-scoped team playlist |
| 3rd | 2  | `competitions[competition]` channel → uploads playlist (`UC→UU`) |
| 4th | 4  | `playlists[competition]` — all broadcaster playlist IDs (flattened) |
| 5th | 1a | `teams[home_team]` channel → uploads playlist (requires competition keyword in title) |
| 6th | 1b | `teams[away_team]` channel → uploads playlist (requires competition keyword in title) |

A video is accepted when **all** of the following are true:
- `publishedAt` is within 5 days of the fixture date
- If the source is a club channel uploads playlist (Tiers 1a/1b): the title contains a competition keyword
- The title contains the home team's `shortName` **or** the away team's `shortName` (case-insensitive substring)

### Smart-skip logic

Before making any YouTube API calls for a gameweek, the script loads the existing JSON file. If every fixture from the current run already has at least one video in its `videos` array, the entire gameweek is skipped with:

```
INFO: gameweek {N} for {competition} is complete, skipping
```

This makes repeated runs idempotent — no YouTube quota is consumed for complete gameweeks.

### Missing match alerts

After processing all fixtures in a gameweek, any match with zero videos produces a visible warning in the Action log:

```
WARNING: No highlights found — {competition} GW{N}: {home} vs {away} ({date})
```

### Merge behaviour

Files are **never overwritten** — new runs always merge:
- If a `match_id` is new: the full match object is appended
- If a `match_id` already exists: only new `video_id`s are appended, no duplicates
- If nothing changed: no file is written and no git commit is made

Writes are atomic (temp file + rename) to avoid corrupted JSON if the Action is interrupted.

### Quota budget

Only `playlistItems.list` is used (**1 unit per page**). `search.list` (100 units/call) is never called.

| Scenario | Units |
|---|---|
| Typical incremental run (7 competitions × 10 matches × 2 pages average) | ~140 units |
| Worst case per run (all tiers tried, 10 pages each) | ~700 units |
| Complete gameweeks skipped | 0 units |

YouTube Data API free quota: **10,000 units/day**.

#### Budget split

| Mode | Hard cap | Headroom |
|---|---|---|
| Incremental | 8,000 units | Leaves 2,000 units for a same-day backfill run |
| Backfill | 9,500 units | 500-unit emergency buffer |

When either cap is hit the script saves state, writes any in-progress files, and exits 0. No data is lost.

#### Runtime state files

| File | Purpose |
|---|---|
| `highlights/quota-tracker.json` | Tracks units consumed today; resets automatically at UTC midnight |
| `highlights/backfill-progress.json` | Checkpoints backfill position (competition + gameweek); resets when the season changes |
| `highlights/backfill.lock` | Created by backfill at startup, deleted in its `finally` block; signals incremental to defer |

If the backfill Action is cancelled mid-run the lock file may be left on disk. The incremental script treats any lock older than 3 hours as stale, removes it, and proceeds normally.

### Required secrets

| Secret | Used by |
|---|---|
| `FOOTBALL_DATA_API_KEY` | Fixture fetch from football-data.org |
| `YOUTUBE_API_KEY` | YouTube `playlistItems.list` calls |

## Admin Panel — coverage counters

The three stat cards at the top show **X/Y ratios**:

| Card | Numerator (X) | Denominator (Y) |
|---|---|---|
| **Own Channel** | Competitions with an official channel ID filled | Total competitions |
| **Fallback Only** | Broadcaster rows with at least one playlist filled | Total broadcaster rows |
| **No Coverage** | Competitions with no official channel AND no broadcaster playlists | Total competitions |

Counters update live as you edit fields or add/remove broadcaster rows.

## Mobile behaviour

On narrow screens (≤ ~380 px):

- **Competition header** hides the name label entirely and replaces the badge chip with a plain integer (e.g. `20`) — the flag, count, Official Channel pill, and play button fit cleanly in one row
- **Competition titles** truncate with `…` on desktop/tablet when space is tight — the flag, team-count badge, Official Channel pill, and play button stay on the same row and are never clipped
- **Team names** truncate with `…` (e.g. `Club Atlético de Ma…`) — the Channel pill, Playlist pill, and play icon always remain visible
- **Team row chips** swap "Channel" / "Playlist" text labels for compact icons (`fi-br-channel` / `fi-br-list`) on mobile to free up horizontal space
- **Team semaphore dot**: 🟢 green = channel *and* playlist filled; 🟡 orange = exactly one filled; 🔴 red = neither filled. Updates live on every keystroke
- **Accordion competition list**: only one competition can be expanded at a time — opening a new one automatically collapses the previously open one
- Hovering (desktop) or long-pressing (mobile) a truncated name shows the full text via the native browser tooltip (`title` attribute)
