# refoot-config

Remote channel configuration for the **ReFoot Highlights** Android app.

## Files

| File | Purpose |
|---|---|
| `sources.json` | YouTube channel/playlist IDs for all tiers (read by the app at runtime) |
| `admin.html` | Browser-based admin panel for managing `sources.json` |
| `uicons/` | Flaticon UIcons Bold Rounded webfont (used by the admin panel) |

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

- **Competition titles** truncate with `…` (e.g. `PREMIER LEA…`) — the flag, team-count badge, Official Channel pill, and play button stay on the same row and are never clipped
- **Team names** truncate with `…` (e.g. `Club Atlético de Ma…`) — the Channel pill, Playlist pill, and play icon always remain visible
- Hovering (desktop) or long-pressing (mobile) a truncated name shows the full text via the native browser tooltip (`title` attribute)
