# refoot-config

Remote channel configuration for the **ReFoot Highlights** Android app.

## Files

| File | Purpose |
|---|---|
| `sources.json` | YouTube channel IDs for competition and club channels (read by the app at runtime) |
| `admin.html` | Browser-based admin panel for managing `sources.json` without editing raw JSON |

## Admin Panel

Open the admin panel at:
**https://mattdltvt5.github.io/refoot-config/admin.html**

Requirements:
- GitHub Personal Access Token with **Contents: Read & Write** on this repo
- football-data.org API key (free tier)

## sources.json schema

```json
{
  "_updated": "YYYY-MM-DD",
  "competitions": {
    "Competition Name": "UCxxxxxxxx"
  },
  "teams": {
    "Exact Team Name": "UCxxxxxxxx"
  }
}
```

- Competition names must exactly match those used in the Android app (`competitionName` strings)
- Team names must exactly match football-data.org `team.name` values
- Empty string `""` means "not configured" — the app falls through to the next tier
