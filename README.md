# refoot-config

Remote channel configuration for the **ReFoot Highlights** Android app.

## Files

| File | Purpose |
|---|---|
| `sources.json` | YouTube channel/playlist IDs for all tiers (read by the app at runtime) |
| `admin.html` | Browser-based admin panel for managing `sources.json` |
| `uicons/` | Flaticon UIcons Bold Rounded webfont (used by the admin panel) |
| `highlights/` | Pre-built video metadata written by the fetch-highlights Action |
| `scripts/highlights_common.py` | Shared utilities, title filter constants, and `is_highlight_title()` |
| `scripts/fetch_highlights.py` | Incremental update script (runs every 4 hours) |
| `scripts/backfill_highlights.py` | Full-season backfill script (manual trigger only) |
| `scripts/clean_highlights.py` | One-time cleanup script — re-evaluates existing JSON files and removes false positives |
| `scripts/clean_wrong_fixture_videos.py` | Retroactive cleanup — removes videos stored for the wrong fixture (both-teams rule) |
| `.github/workflows/fetch-highlights.yml` | GitHub Action that runs the incremental script every 4 hours |
| `.github/workflows/backfill-highlights.yml` | GitHub Action for the manual backfill (workflow_dispatch only) |
| `.github/workflows/clean-highlights.yml` | GitHub Action for the manual false-positive cleanup (workflow_dispatch only) |
| `.github/workflows/clean-wrong-fixture-videos.yml` | GitHub Action for the retroactive both-teams cleanup (workflow_dispatch only) |

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
- The title passes the multilingual highlight title filter (see [Title filter](#title-filter) below)

### Title filter

All tiers pass through `is_highlight_title()` in `highlights_common.py`. The function checks a **blocklist first** — if any term matches, the video is rejected immediately, regardless of allowlist matches. Only videos that survive the blocklist and contain at least one allowlist term are accepted.

#### Evaluation order

1. **Blocklist checked first — always wins.** A blocked video can never be saved by an allowlist match.
2. **Allowlist checked second.** At least one term must match for the video to be accepted.

#### Languages covered

English, Spanish, French, German, Italian, Portuguese, Arabic, Dutch, Turkish, Japanese, Korean.

#### Blocklist (reject if any term matches)

| Language | Terms |
|---|---|
| English | `press conference`, `presser`, `interview`, `reaction`, `training`, `preview`, `analysis`, `tactical`, `watch along`, `live stream`, … |
| Spanish | `rueda de prensa`, `entrevista`, `previo`, `análisis`, `reacción` |
| French | `conférence de presse`, `avant-match`, `après-match`, `analyse`, `réaction`, `entraînement` |
| German | `pressekonferenz`, `vorschau`, `reaktion`, `training einheit` |
| Italian | `conferenza stampa`, `intervista`, `anteprima`, `analisi`, `reazione`, `allenamento` |
| Portuguese | `coletiva de imprensa`, `pré-jogo`, `pós-jogo`, `análise`, `treino` |
| Arabic | `مؤتمر صحفي`, `مقابلة`, `تحليل`, `تدريب`, `معاينة` |
| Dutch | `persconferentie`, `vooruitblik` |
| Turkish | `basın toplantısı`, `röportaj`, `önizleme`, `analiz`, `antrenman` |

> Shared terms: `interview` / `entrevista` (Spanish + Portuguese), `analyse` (French / German / Dutch), `training` (English / Dutch) are listed once and cover all relevant languages.

#### Allowlist (at least one term must match)

| Language | Terms |
|---|---|
| English | `highlight`, `highlights`, `extended highlights`, `match highlights`, `full match`, `goals` |
| French | `résumé`, `buts` |
| Spanish | `resumen`, `goles`, `mejores momentos` |
| German | `zusammenfassung`, `tore`, `spielzusammenfassung` |
| Italian | `sintesi`, `gol` |
| Portuguese | `melhores momentos`, `gols`, `resumo` |
| Arabic | `ملخص`, `أهداف` |
| Dutch | `samenvatting`, `doelpunten` |
| Turkish | `özet`, `goller`, `maç özeti` |
| Japanese | `ハイライト`, `ゴール` |
| Korean | `하이라이트`, `골` |

> `"gol"` is a substring — it intentionally matches `goles`, `gols`, `goal`, and `goals`.

#### Edge case: `"Match Highlights Preview"`

The word `"preview"` is in the blocklist, `"highlights"` is in the allowlist. Because the **blocklist is checked first**, this title is **rejected**. The filter never reaches the allowlist if any blocklist term matched.

### False-positive cleanup

If a filter update causes previously accepted videos to become false positives, run the **Clean highlights** workflow from the Actions tab:

**Actions → Clean highlights (false-positive removal) → Run workflow**

The script (`scripts/clean_highlights.py`):
1. Iterates every gameweek/matchday JSON file for all competitions
2. Re-evaluates the stored `title` field of each video against the current filter (no YouTube API calls — no quota consumed)
3. Removes any video whose title no longer passes `is_highlight_title()`
4. Regenerates `highlights/summary.json`
5. Commits and pushes the changed files with `[skip ci]`

Safe to re-run at any time. If nothing changed, no commit is made.

### Wrong-fixture video cleanup

If videos from other fixtures were stored (e.g. a "Rennais vs Nantes" recap stored against a "PSG vs Nantes" fixture because only one team name matched), run the **Clean wrong-fixture videos** workflow:

**Actions → Clean wrong-fixture videos (retroactive both-teams cleanup) → Run workflow**

The script (`scripts/clean_wrong_fixture_videos.py`):
1. Iterates every gameweek/matchday JSON file for all competitions
2. For each fixture, derives keyword sets for the home team and away team from the stored full names (strips year codes, org suffixes; adds known abbreviations from a built-in alias map)
3. Removes any video whose title does not contain at least one keyword for **both** the home team **and** the away team
4. Regenerates `highlights/summary.json`
5. Commits and pushes the changed files with `[skip ci]`

Safe to re-run at any time. Fixtures that lose all videos will show as "Highlight not available yet" placeholders in the app — which is correct behaviour when the only stored video was for the wrong match.

> **Note:** The live pipeline (`search_playlist()`) now requires both team names for tier 2 and tier 4 sources, so this cleanup script only needs to be run once for historical data. Re-run if the alias map in the script is extended to cover additional teams.

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

The backfill workflow checkout step uses `persist-credentials: true` so the Python script can push commits. Git identity (`github-actions[bot]`) is configured by the script itself at startup — GitHub Actions runners have no default identity and any commit attempted without it would fail with `fatal: empty ident name`.

#### Backfill commit cadence

The backfill script commits to git after **every competition completes**, not just at the end. This means:
- A `chore: backfill {competition} complete [skip ci]` commit appears after each competition
- If the process is killed mid-run, all completed competitions are already in git
- Re-triggering always resumes from the correct competition and gameweek

A YouTube HTTP 403 (quota exhaustion detected by YouTube itself) is treated identically to the internal cap — the checkpoint and all written files are committed and the script exits 0 (green run in GitHub Actions).

### Required secrets

| Secret | Used by |
|---|---|
| `FOOTBALL_DATA_API_KEY` | Fixture fetch from football-data.org |
| `YOUTUBE_API_KEY` | YouTube `playlistItems.list` calls |

## Admin Panel — Highlights Coverage dashboard

The coverage panel at the top of the admin page shows real-time highlights coverage loaded from `highlights/summary.json`. Competitions are displayed in two labelled groups that mirror the section separators in the Leagues panel below:

| Group | Competitions |
|---|---|
| **LEAGUES** | Premier League, LaLiga, Serie A, Bundesliga, Ligue 1 |
| **INTERNATIONAL** | Champions League, Europa League, Euro Cup, Copa America, World Cup |

Each row shows:
- A competition icon (Champions League uses `filter: invert(1)` via `.coverage-icon` to render its white-on-transparent PNG as dark/black on the light card background — the dark Leagues panel is unaffected)
- A progress bar: **green** = fully covered, **orange** = partially covered, **grey** = no data yet
- A covered/total fraction (e.g. `8/10`)
- A GW or MD badge for the most recent gameweek/matchday in the data

Competitions that have no data in `summary.json` yet (Euro Cup, Copa America, World Cup) render with a grey bar and `0/0` until the backfill workflow writes their files.

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
