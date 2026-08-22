# refoot-config

Remote channel configuration for the **ReFoot Highlights** Android app.

## Recent changes

### Group-stage highlights grafted onto group matches (2026-07-06)

Group-stage matches in every `tournament-groups/*.json` now carry `video_id` (the matched YouTube ID) and `match_id` (the FD/API-Sports fixture ID), mirroring the existing knockout graft. Three surfaces — Euro Cup, World Cup, and Copa América group stages — are covered by the same generic code path.

**Confirmed: group fixtures were already being matched.** `stage_to_file_stem()` maps `GROUP_STAGE` → `matchday-{N}`, so `highlights/{slug}/{season}/matchday-1.json` etc. have been written by the highlights pipeline all along. The highlights data was present; the tournament cache just wasn't carrying the reference.

**What changed:**
- `build_group_matches()` now emits `match_id` (FD fixture ID) on each group match; `build_tournament_data()` wraps each group match with `video_id` from `existing_video_ids` — same pattern as knockout matches.
- `read_existing_video_ids()` now also reads group-match `video_id` values from the existing file, so a matched highlight survives an FD cache rebuild without the graft needing to re-run.
- `graft_video_ids()` now loops over `groupMatches[]` in addition to `matches[]`, looking up `matchday-{N}.json` files in the per-season highlights directory for each slug.
- Fixed a latent bug introduced by the per-season restructure: `_lookup_video_id` and Copa's `_lookup_copa_video_id` were reading from the old flat `highlights/{slug}/{stem}.json` path; both now correctly read `highlights/{slug}/{season}/{stem}.json`.
- `_read_video_from_stem()` — shared helper extracted to avoid duplicating the file-read logic between knockout and group lookups.
- `normalize_group()` in `sync_copa_tournament.py` now emits `match_id` (API-Sports fixture ID) and `video_id: null` on each group match; `graft_video_ids()` (which already covers Copa) grafts the Copa group matches on each 4-hour run.

**No new API calls.** The graft reads only local highlights files — the same `matchday-N.json` files already written by the incremental highlights pipeline.

**Files changed:** `scripts/sync_tournaments.py`, `scripts/sync_copa_tournament.py`  
**Tests added/updated:** 10 new tests in `test_sync_tournaments.py` (group-match graft, null-when-unmatched, non-regression, existing video_id preservation via round-trip); 3 new + 1 updated in `test_sync_copa_tournament.py` (match_id field, video_id field, Copa lookup path fix)  
**Total Python tests:** 248 passing

### League fixtures carry match_id (2026-07-06)

Each fixture in `fixtures/{slug}/{season}.json` now carries `"match_id"` — the Football-Data.org fixture ID (`m["id"]`). This is the same integer the highlights artifact (`highlights/{slug}/{season}/gameweek-N.json`) already stores as `match_id`. The app can now join a gameweek fixture to its highlight with an integer equality check (`fixture.matchId == highlight.matchId`) rather than a three-field string comparison, mirroring the knockout pattern where the match object carries its highlight reference directly.

**Why:** The audit confirmed that `_normalize_artifact()` had `m["id"]` in scope but omitted it from the output dict. Adding it is a pure mapping addition — no new API calls, no new workflow, no change to which fixtures are written or when. A match with a missing FD `id` field emits `match_id: null` (treated by the app as not-joinable, no play button) rather than crashing the artifact.

**Files changed:** `scripts/fixture_providers.py`  
**Tests added:** three new assertions in `scripts/tests/test_league_fixtures.py` — `match_id` equals FD id, value is identical to the highlights-path `_normalize` output for the same match (cross-artifact join is sound), missing id emits null without crashing  
**Total Python tests:** 237 passing

### Per-season file restructure (2026-07-06)

Restructured all three data stores to per-season file paths — `{type}/{slug}/{season}.json` for fixtures and standings, `highlights/{slug}/{season}/{stem}.json` for highlights — so prior seasons are preserved when the new season starts in August.

**Why:** with flat paths (`standings/premier-league.json`) the pipeline overwrites last season's data at the season rollover, which would break the app during the transition window. Per-season paths mean the current season's file is written without touching previous seasons.

**Pipeline changes:**
- `write_fixtures_artifacts()` now writes `fixtures/{slug}/{season}.json` (e.g. `fixtures/premier-league/2025.json`)
- `write_standings()` now writes `standings/{slug}/{season}.json`; `season` is passed from `main()`
- `gw_path()` now accepts a `season: int` parameter and writes to `highlights/{slug}/{season}/{stem}.json`
- `generate_summary()` scans `highlights/{slug}/{season}/` using `season_for_competition(comp_name)` to determine the right season directory
- Both `gw_path` callers (`fetch_highlights.py`, `backfill_highlights.py`) pass the season already in scope

**App changes:**
- `SeasonDateCalculator.currentSeasonYear(slug, now)` — new static method; August threshold for domestic/UCL/UEL slugs, cycle formula for summer tournaments (mirrors `season_for_competition()` in Python); all three cache services use it
- `LeagueFixturesCacheService` — updated URL to `fixtures/{slug}/{season}.json`; `_staleMinutes` increased from 30 → 360 to match the ~4-hour pipeline cadence
- `StandingsCacheService` — updated URL to `standings/{slug}/{season}.json`
- `HighlightsCacheService` — updated URL to `highlights/{slug}/{season}/{stem}.json`; added `now` injection for clock control in tests

**Migration (data already in place):**  
Existing standings and highlights files were COPIED (not moved) to their per-season paths. Old flat paths remain until the app is verified against the new paths. Fixtures were NOT copied — the remote file had a stale `season: 2026` from before the threshold fix; the pipeline writes the correct `fixtures/{slug}/2025.json` on its next run.

**Files changed:** `scripts/fetch_highlights.py`, `scripts/sync_standings.py`, `scripts/highlights_common.py`, `scripts/backfill_highlights.py`, `lib/services/season_date_calculator.dart`, `lib/services/league_fixtures_cache_service.dart`, `lib/services/standings_cache_service.dart`, `lib/services/highlights_cache_service.dart`  
**Tests added:** `scripts/tests/test_per_season_paths.py` — 11 Python tests (per-season write paths, prior-season preservation, fixtures+standings agreement); `test/season_date_calculator_test.dart` — 14 Dart tests (August boundary, cycle logic for summer tournaments); per-season URL assertions added to `test/standings_cache_service_test.dart`, `test/highlights_cache_test.dart`, `test/gameweeks_tab_test.dart`  
**Total Python tests:** 253 passing · **Total Dart tests:** 220 passing

### Season-selection consolidation (2026-07-06)

Fixed a season-boundary bug that caused the Gameweeks tab to show the upcoming (2026-27) season while the Standings tab correctly showed the previous (2025-26) season.

**Root cause:** two separate `current_season()` implementations with different thresholds. `sync_standings.py` used August (month ≥ 8 → new season), which is correct. `highlights_common.py` used July (month ≥ 7), which returned 2026 in July 2026 and caused the fixtures pipeline to query the registered-but-not-started 2026-27 season instead of the completed 2025-26 season.

**Fix:** extracted a single canonical `current_season(now=None)` into `scripts/season_utils.py` (stdlib-only, no external deps). Both `sync_standings.py` and `highlights_common.py` now import from it — neither defines its own implementation. The August boundary rule is the same as the app's `SeasonDateCalculator._leagueSeasonStart` (Dart); the rule and its location are cross-referenced in both files. No backfill is needed: the next pipeline run calls `get_full_season(code, comp, 2025)`, which fetches the completed 2025-26 season from FD, and overwrites the artifact.

**The canonical rule:** `month < 8` → previous season year; `month ≥ 8` → current year. This keeps fixtures and standings on the same season from now through July 31; both roll to the new season on August 1.

**Files changed:** `scripts/season_utils.py` (new), `scripts/highlights_common.py`, `scripts/sync_standings.py`, `lib/services/season_date_calculator.dart` (comment only)  
**Tests added:** `scripts/tests/test_season_utils.py` — 31 tests covering the August boundary, all 5 domestic leagues + UCL/UEL, summer tournaments unchanged, and a consolidation identity check (`sync_standings.current_season is season_utils.current_season`)  
**Total Python tests:** 222 passing

### League fixtures artifact (2026-07-05)

Added a dedicated `fixtures/{slug}.json` artifact for all five domestic leagues (Premier League, LaLiga, Serie A, Bundesliga, Ligue 1). Each file carries every fixture for the current season — scheduled, in-play, and finished — with score, status, gameweek, and team crest URLs in a shape that `GroupMatch.fromJson` consumes unchanged.

**How it works:**
- `FootballDataProvider` now fetches all match statuses (no `status=FINISHED` filter) and caches the raw FD response per `(code, season)`.
- `get_fixtures()` (highlights path) filters to `FINISHED` only — unchanged behaviour for `is_gameweek_complete()`.
- `get_full_season()` (artifact path) uses the cached response to produce a flat list of GroupMatch-compatible dicts; zero extra FD calls.
- `fetch_highlights.py` calls `write_fixtures_artifacts()` before the YouTube quota guard, so artifacts stay current even when the YouTube daily budget is spent.
- The `fetch-highlights.yml` workflow's `git add` step now includes `fixtures/`.

**Files changed:** `scripts/fixture_providers.py`, `scripts/fetch_highlights.py`, `scripts/highlights_common.py`, `.github/workflows/fetch-highlights.yml`  
**Tests added:** `scripts/tests/test_league_fixtures.py` — 33 tests covering artifact shape, highlights-path FINISHED filter, no-double-fetch cache, and `DOMESTIC_LEAGUE_COMPS` membership  
**Total Python tests:** 192 passing

## Files

| File | Purpose |
|---|---|
| `sources.json` | YouTube channel/playlist IDs for all tiers (read by the app at runtime) |
| `admin.html` | Browser-based admin panel for managing `sources.json` |
| `uicons/` | Flaticon UIcons Bold Rounded webfont (used by the admin panel) |
| `highlights/` | Pre-built video metadata written by the fetch-highlights Action (`{slug}/{season}/{stem}.json`) |
| `fixtures/` | Per-league fixture artifacts (`{slug}/{season}.json`) — all match statuses, GroupMatch shape, written every ~4 hours |
| `standings/` | Pre-built standings cache (`{slug}/{season}.json`) — written daily by sync-standings |
| `scripts/season_utils.py` | Canonical `current_season(now=None)` — the shared August-boundary rule used by both the standings and fixtures pipelines |
| `scripts/highlights_common.py` | Shared utilities, title filter constants, `is_highlight_title()`, `_normalize()`, `team_tokens()`, `DOMESTIC_LEAGUE_COMPS`, `TEAM_TITLE_ALIASES`, and `season_for_competition()` |
| `scripts/fixture_providers.py` | Pluggable fixture provider layer — `FootballDataProvider` (all-status fetch with `_raw_cache`, `get_fixtures()`, `get_full_season()`, `_normalize_artifact()`), `ApiSportsProvider`, and `APISPORTS_COMPETITIONS` config registry |
| `scripts/fetch_highlights.py` | Incremental update script (runs every 15 minutes) |
| `scripts/backfill_highlights.py` | Full-season backfill script (manual trigger only) |
| `scripts/backfill_copa_america.py` | Copa America 2024 backfill script — fetches fixtures from API-Sports, finds highlights on YouTube (manual trigger only) |
| `scripts/clean_highlights.py` | One-time cleanup script — re-evaluates existing JSON files and removes false positives |
| `scripts/clean_wrong_fixture_videos.py` | Retroactive cleanup — removes videos stored for the wrong fixture (both-teams rule) |
| `scripts/debug_match.py` | Dry-run diagnostic — tests the fixture↔title matcher over cached data without hitting the YouTube API |
| `diagnostics/apisports_probe.py` | **Manual-only** API-Sports probe — verifies season coverage and captures round-string formats; see [Diagnostics](#diagnostics) |
| `.github/workflows/fetch-highlights.yml` | GitHub Action that runs the incremental script every 15 minutes |
| `.github/workflows/backfill-highlights.yml` | GitHub Action for the manual backfill (workflow_dispatch only) |
| `.github/workflows/backfill-copa-america.yml` | GitHub Action for the Copa America backfill (workflow_dispatch only) |
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

The highlights pipeline has **two operating modes** for football-data.org competitions plus a separate Copa America backfill path:

| Mode | Script | Trigger | Fixture source | Budget |
|---|---|---|---|---|
| **Incremental** | `fetch_highlights.py` | Every 15 minutes (scheduled) | football-data.org | 8,000 YouTube units/day |
| **Backfill** | `backfill_highlights.py` | Manual only (`workflow_dispatch`) | football-data.org | 9,500 YouTube units/day |
| **Copa America backfill** | `backfill_copa_america.py` | Manual only (`workflow_dispatch`) | API-Sports free tier | 9,500 YouTube units/day (isolated) |

The incremental and main-backfill scripts track YouTube consumption in `highlights/quota-tracker.json`.  The Copa America backfill uses a **separate** in-memory YouTube quota tracker (never writes `quota-tracker.json`) and tracks API-Sports calls in `highlights/apisports-quota-tracker.json`.

#### Fixture provider architecture

`scripts/fixture_providers.py` implements a pluggable provider layer:

- **`FootballDataProvider`** — wraps football-data.org fetch + normalize logic.  Used by both the incremental and main-backfill scripts for all competitions in `COMPETITION_CODE_MAP`.
- **`ApiSportsProvider`** — fetches from the [API-Sports](https://www.api-sports.io/) free tier (100 req/day).  Used only by `backfill_copa_america.py`.
- **`APISPORTS_COMPETITIONS`** — config-driven registry.  Adding a new API-Sports competition is a one-entry change with no code modification needed.

> **API-Sports free-tier constraint:** seasons 2022–2024 only.  Querying season 2025+ returns a paywall error.  The free tier is a **historical backfill source only** — it cannot cover the current season.  Live coverage requires a paid plan.  Canonical league IDs are pinned in the config (Copa America = id 9) to avoid accidentally selecting women's or youth variants.

#### Copa America 2024

Copa America is intentionally **not** in `COMPETITION_CODE_MAP`, so the scheduled 30-minute fetch Action never touches it.  To populate Copa America data, trigger the dedicated Action manually:

**Actions tab → Backfill Copa America highlights → Run workflow**

The workflow (`backfill-copa-america.yml`) uses `permissions: contents: write` and `persist-credentials: true` — the same push mechanism as the other backfill workflows — so the commit and push happen automatically inside CI.  No manual `git push` is needed.

This fetches all 32 Copa America 2024 fixtures from API-Sports and searches YouTube for highlights using the same tier-waterfall logic as the main backfill.  Fixture data is written to `highlights/copa-america/matchday-{1,2,3}.json`, `quarter-final.json`, `semi-final.json`, `third-place.json`, and `final.json`.

**Required secrets** (Settings → Secrets and variables → Actions):
- `APISPORTS_API_KEY` — API-Sports API key (free tier, 100 req/day)
- `YOUTUBE_API_KEY` — YouTube Data API v3 key (shared with the other workflows)

The workflow is **manual-only** (`workflow_dispatch`).  It has no `schedule:` trigger and is never invoked by the 30-minute incremental run.  This is intentional — running it on a schedule would burn API-Sports free-tier quota and violate the standing rule that the scheduled job is API-Sports-free.

### First-time setup

Before the incremental job can cover the full season, trigger the backfill once manually from the **Actions** tab → **Backfill highlights cache** → **Run workflow**. It will fetch all finished fixtures for the current season. If the daily cap is hit, re-trigger the next day — it resumes exactly where it stopped.

The incremental job runs automatically every 15 minutes and keeps new results flowing once the backfill is complete.

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
- `publishedAt` is within 3 days of the fixture date
- If the source is a club channel uploads playlist (Tiers 1a/1b): the title contains a competition keyword
- The title matches **at least one candidate token** for the home team **or** (for tier 2/4) **both** the home and away team.  Candidates are derived from the FD `{name, shortName, tla}` triplet with NFKD diacritic normalisation (e.g. `"Barça"` → `"barca"`) and the title is normalised the same way before comparison.  TLAs shorter than 4 characters are excluded from the auto-derived set.  `TEAM_TITLE_ALIASES` provides explicit override tokens for teams whose YouTube title form cannot be derived from the FD fields (e.g. `"LOSC"` for Lille OSC, `"Stade Rennais"` for Stade Rennais FC 1901).
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

#### LaLiga competition-channel strict gate (tier 2 only)

Videos from the **LaLiga official channel** (tier 2 — both per-matchday playlists and the uploads feed) are subject to a second, stricter filter beyond `is_highlight_title()`.  `is_laliga_highlight_title()` requires the title to contain **`HIGHLIGHTS LALIGA`** (case-insensitive, whitespace-normalised).

**Why this exists:** The LaLiga channel publishes two types of highlight content:
- `VILLARREAL CF 5 - 1 ATLÉTICO DE MADRID | HIGHLIGHTS LALIGA EA SPORTS` ✅ accepted
- `FC BARCELONA 2 - 1 GIRONA FC | RESUMEN LALIGA EA SPORTS` ❌ rejected

`RESUMEN` passes the global Spanish allowlist (it means "highlights") but the LaLiga channel's RESUMEN format produces shorter, vertically-cropped social edits rather than full broadcast highlight packages.  Adding `resumen` to the global blocklist would break legitimate Spanish-language videos from team channels and broadcasters — so the gate is scoped to the source, not the language.

**Scope is by source, not by competition:**
- Tier 2 (LaLiga competition channel): `is_laliga_highlight_title()` required in addition to `is_highlight_title()`
- Tier 1 (team channels / team playlists): unchanged — `is_highlight_title()` only
- Tier 4 (broadcaster playlists): unchanged — `is_highlight_title()` only

**Durability:** The `HIGHLIGHTS LALIGA` marker is sponsor-agnostic.  The EA SPORTS suffix may change between seasons; the check deliberately omits it.

### False-positive cleanup

If a filter update causes previously accepted videos to become false positives, run the **Clean highlights** workflow from the Actions tab:

**Actions → Clean highlights (false-positive removal) → Run workflow**

The script (`scripts/clean_highlights.py`) applies the **source-scoped LaLiga gate**:
1. Iterates all LaLiga `gameweek-*.json` files
2. For each video whose `tier_used == 2` (LaLiga competition channel): removes it if the title does not contain `HIGHLIGHTS LALIGA`
3. Videos with `tier_used == 1` or `tier_used == 4` are **never removed** by this pass, regardless of title
4. Prints a per-gameweek audit log — removed video IDs + titles, non-LaLiga-channel count, fixtures that became empty
5. Regenerates `highlights/summary.json`

Safe to re-run at any time. Fixtures that lose all videos stay in the JSON as empty `videos: []` — the app shows them as "Highlight not available yet".

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

### Team name matching and diacritic normalisation

`team_tokens()` in `highlights_common.py` resolves the candidate set for each team in two steps:

1. **Override path** (`TEAM_TITLE_ALIASES`): explicit token list for every known team across all five competitions.  An entry **replaces** the auto-derived set entirely and is the expected path for all current teams.  96 teams are mapped — see `highlights_common.py` for the full dict.

2. **Auto-derivation fallback** (`_auto_tokens()`): for teams with **no entry** (newly promoted / relegated clubs).  Derives candidates from the FD `{name, shortName, tla}` triplet plus progressively stripped variants:
   - Strip `"1. FC/FSV"` numeric prefix
   - Strip trailing year numbers (`1909`, `1846`, `05` …)
   - Strip trailing org suffixes (`FC`, `AFC`, `SC`, `SV`, `Calcio`, `Balompié` …)
   - Strip trailing geographic qualifiers (`de Madrid`, `de Vigo`, `de Fútbol` …)

   Examples: `"Leicester City FC"` → `["leicester city fc", "leicester city"]`; `"Bologna FC 1909"` → `["bologna fc 1909", "bologna fc", "bologna"]`; `"Rayo Vallecano de Madrid"` → `["rayo vallecano de madrid", "rayo", "rayo vallecano"]`.

Both paths normalise through `_normalize()` (NFKD + drop combining marks + casefold), and the video title is normalised the same way before comparison.

**Hard collision guards** baked into the alias entries:
- `"FC Barcelona"` maps to `["FC Barcelona", "Barça", "Barca"]` — NOT bare `"Barcelona"`, which is a substring of `"RCD Espanyol de Barcelona"`
- `"Real Madrid CF"` maps to `["Real Madrid CF", "Real Madrid"]` — NOT bare `"Madrid"`, shared with Atlético and Rayo
- `"Paris FC"` maps only to `["Paris FC"]` — bare `"Paris"` would absorb PSG videos in the broad uploads tier

**Adding/updating entries:** use the exact FD `team.name` value (same string as `home_team`/`away_team` in the JSON files).  Newly promoted teams that are not yet in the map are handled automatically by `_auto_tokens()` — only add an explicit entry if the auto-derivation produces tokens that don't appear in actual broadcast titles.

**Diagnostics:** `python scripts/debug_match.py` tests the matcher over all cached Ligue 1 data with zero YouTube API calls.  Shows per-fixture root-cause analysis and a regression summary.

### Matching diagnostics (`REFOOT_DEBUG_MATCHING`)

When a fixture repeatedly shows "No highlights" and the root cause is unclear, enable the matching debugger:

```bash
# Via environment variable (works with any runner):
REFOOT_DEBUG_MATCHING=1 python scripts/fetch_highlights.py

# Via CLI flag:
python scripts/fetch_highlights.py --debug-matching
```

**What it emits** (only for fixtures that end up with no highlights):

```
=== DEBUG MATCH FAILURE  LaLiga / gameweek-14 / 2025-11-30
  fixture: 'RC Celta de Vigo'  vs  'RCD Espanyol de Barcelona'
  accent-probe home: 'RC Celta de Vigo'  →  'rc celta de vigo'  (no diacritic change)
  accent-probe away: 'RCD Espanyol de Barcelona'  →  'rcd espanyol de barcelona'  (no diacritic change)
  home tokens: ['celta', 'celta de vigo', 'celta vigo', 'rc celta', 'rc celta de vigo']
  away tokens: ['espanyol', 'espanyol de barcelona', 'rcd espanyol', 'rcd espanyol de barcelona']
  [Tier 2 / PLxxx] — 5 candidate(s)
    [abc123]  cross-match-guard:away-missing (need one of ['espanyol', ...])
      title: 'CELTA 2-1 GETAFE | RESUMEN LALIGA EA SPORTS'
      norm:  'celta 2-1 getafe | resumen laliga ea sports'
    [def456]  too-short:63s
      title: 'CELTA vs ESPANYOL | #Shorts'
===
```

**Rejection reasons:**

| Code | Meaning |
|---|---|
| `outside-date-window (YYYY-MM-DD)` | Video published outside the ±3-day window |
| `no-comp-keyword` | Tier 1c/1d competition filter: no competition keyword in title |
| `cross-match-guard:home-missing` | `requires_both_teams=True` but home team tokens absent |
| `cross-match-guard:away-missing` | `requires_both_teams=True` but away team tokens absent |
| `no-token-overlap` | Tier 1a/1b: neither team found in title |
| `title-filter:blocked:<term>` | Global blocklist matched first (e.g. `'press conference'`) |
| `title-filter:no-allowlist-match` | No allowlist term found after hashtag strip |
| `laliga-gate:no-highlights-laliga` | Passed all other filters but title lacks `HIGHLIGHTS LALIGA` (tier 2 only) |
| `too-short:<N>s` | Quality filter: clip shorter than 120 s |
| `portrait-video` | Quality filter: vertical/portrait aspect ratio |

**Accent probe:** The `accent-probe` lines show what `_normalize()` does to each team name.  This pinpoints diacritic bugs:
- `'Deportivo Alavés'` → `'deportivo alaves'` (ÿ/é/á stripped by NFKD combining-mark removal)
- `'Köln'` → `'koln'` (ö = o + combining umlaut, stripped)
- `'FC København'` → `'fc københavn'` (ø has no NFKD decomposition — survives as-is)

When a team whose name contains ø or ß doesn't match broadcast titles that use the ASCII transliteration, the fix is a `TEAM_TITLE_ALIASES` entry, not a change to `_normalize()`.

**Properties:**
- **Quota-neutral**: reuses already-fetched playlist items in memory; makes zero new YouTube API calls.
- **Output-only gate**: no accept/reject thresholds, token sets, or quota tracking are modified.
- **Normal runs unaffected**: without the flag, behavior is byte-identical to before.

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

## Testing

### Running the tests

**Python test suite** (pure-function + mocked-HTTP + config schema):
```bash
# From the repo root
pip install -r requirements-dev.txt
pytest scripts/tests/ -v
```

**Fast subset only** (pre-commit / quick iteration — no network, no file I/O):
```bash
pytest scripts/tests/test_pure_functions.py -x --no-header -q
```

### Cross-repo season-boundary contract

`scripts/tests/test_season_boundary_contract.py` pins the canonical August-1 **UTC**
season rule as explicit `UTC timestamp → season integer` cases. The **identical** table
also lives in the app repo at `refoot_flutter/test/season_boundary_contract_test.dart`.
The two are a cross-repo contract with no shared code: `current_season()` here and
`SeasonDateCalculator.currentSeasonYear()` there must both satisfy it. If you change one
table you MUST change the other — a mismatch is the drift this guard catches, and it
turns one repo's CI red.

### Pre-commit hook

Runs the no-network pure-function tests automatically before every local commit.

Install (Unix / Git Bash on Windows):
```bash
cp .githooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit   # Unix only; not needed in Git Bash on Windows
```

After installation the hook runs `test_pure_functions.py` on every `git commit`.
If `pytest` is not installed it warns and exits 0 (never hard-blocks a commit).

### CI gate

`.github/workflows/run-tests.yml` runs the full Python suite on every push and
pull request.  It carries **no API keys**, so any accidental live call to
YouTube, football-data.org, or API-Sports fails immediately with a
missing-key/auth error — quota is never burned by tests.

The auto-commit produced by `fetch-highlights.yml` carries `[skip ci]` in its
message.  GitHub Actions skips all workflows for commits that contain `[skip ci]`
— this workflow is therefore **never triggered by data-only commits**.  Do NOT
remove `[skip ci]` from the fetch-highlights auto-commit.

### Where fixtures live

| Repo | Path | Purpose |
|---|---|---|
| `refoot-config` | `scripts/tests/fixtures/` | Python test snapshot files (future use) |
| `refoot_flutter` | `test/fixtures/` | Flutter test JSON fixtures |

### API-isolation rules

- **No live API calls in any test.** Python tests patch `requests.get` via
  `unittest.mock.patch("highlights_common.requests.get", ...)`.  Flutter tests
  inject `MockClient` via the `svgHttpClient` / `http.Client?` constructor param.
- **No API keys in CI.** The `run-tests.yml` workflow intentionally omits
  `YOUTUBE_API_KEY`, `FOOTBALL_DATA_API_KEY`, and `APISPORTS_API_KEY`.
- **Fixtures are snapshots.** Python fixtures in `scripts/tests/fixtures/` and
  Flutter fixtures in `test/fixtures/` are in-repo copies, never read from the
  live `highlights/` directory.

### PR-review policy

Any change to matcher logic (`_normalize`, `team_tokens`, `TEAM_TITLE_ALIASES`),
title-filter logic (`is_highlight_title`, `TITLE_BLOCKLIST`, `TITLE_ALLOWLIST`),
or normalization must include a corresponding test in the same commit.

## Playlist owner verification

Each PL-prefixed playlist ID in `sources.json` is verified against the YouTube
API to confirm that the playlist's owning channel matches the broadcaster label
it is filed under.  A correct-length, plausible-title ID whose owner is a
different broadcaster is caught at this layer.

### How it works

| Concept | Detail |
|---|---|
| Trigger | Push to `sources.json` or `workflow_dispatch` |
| Workflow | `.github/workflows/verify-playlist-owners.yml` |
| Script | `scripts/verify_playlist_owners.py` (entry point) |
| Core logic | `verify_playlist_owners()` in `highlights_common.py` |
| Cache | `highlights/playlist-owners.json` — one entry per playlist ID |
| API call | `playlists.list?part=snippet` (1 quota unit per new ID) |
| Quota | Counted against `highlights/quota-tracker.json` |

**On-change gating** (with seeded-entry exception):
- IDs with a resolved `channel_id` in `playlist-owners.json` are not re-fetched.
  The label-vs-owner comparison re-runs on every invocation so a re-label in
  `sources.json` is caught without a re-fetch.
- IDs with `channel_id = null` (manually seeded, not yet API-verified) are
  treated as new — they are fetched even though they appear in the cache.  This
  ensures a hand-asserted owner is confirmed by a real API call before being trusted.

**Failure modes** (non-zero exit):
- Owner mismatch — correct-length ID filed under the wrong broadcaster label.
  Owner comparison uses `channel_title` (the channel's own name), never
  `playlist_title` — a playlist titled "FIFA World Cup 2026™ Match Highlights"
  but owned by "SBS Sport" filed under "FIFA" is correctly flagged.
- Unresolvable ID — playlist is private, deleted, or the API returns no items
- Uncached or seeded (`channel_id = null`) ID with no `YOUTUBE_API_KEY`

**Quota**: `playlists.list?part=snippet` costs 1 unit — identical to
`playlistItems.list`.  `playlistItems.list` cannot return the playlist owner
(only the item uploader); `playlists.list` is therefore both the correct and the
most efficient endpoint for owner verification.  All calls are counted in
`highlights/quota-tracker.json` using the same `quota.increment()` mechanism as
every other YouTube API call.

### Owner-matching tolerance

Label and channel-title comparisons strip common qualifier words (`Sport`,
`Sports`, `TV`, `US`, `Deportes`) and lowercase before comparing token sets.
`"CBS Sport Golazo"` matches `"CBS Sports"` (same org); `"FIFA"` does not match
`"SBS Sport"` (different entity).  Tolerance applies to **owner names only** —
a playlist title containing "FIFA" never satisfies a "FIFA" broadcaster label if
the owning channel is a different entity.

## Known TODOs

### World Cup broadcaster playlist IDs

**SBS Sport** (`PLNuJDkj3zBvPVhoKC6Oq8j4w7AH9l-ejG`) — active source, correctly
filed under `"SBS Sport"`.  The playlist title ("FIFA World Cup 2026™ Match
Highlights") is the title SBS Sport chose for their playlist; it is NOT the
channel's name.  Owner verification compares against `channel_title` ("SBS Sport"),
not `playlist_title`, so the "FIFA" tokens in the title do not satisfy a "FIFA"
broadcaster label.  The guard's `highlights/playlist-owners.json` cache was
seeded with `channel_id = null`; the verify-playlist-owners workflow will
resolve the real `channel_id` on its next run triggered by this push.

**FIFA-official** — `sources.json → playlists["World Cup"]["FIFA"]` is `[]`.
The FIFA-official YouTube channel playlist for the 2026 World Cup tournament
highlights has not been confirmed.  Do not commit unverified IDs.

**Telemundo** — `sources.json → playlists["World Cup"]["Telemundo"]` is `[]`.
The previous 13-character value (`PLXHZm5xDlEdQ`) failed the format check and
was silently skipped.  A qualifying playlist (`PLXEMPXZ3PY1i3lX_C0Tul361dLtGE1SrT`)
was found but covers only qualifying matches, not the main tournament.  Do not
commit unverified IDs.

FOX Sports (`PLSoN6Th-EepMUaxmTobuR_SBwVkdkxdfO`) and SBS Sport
(`PLNuJDkj3zBvPVhoKC6Oq8j4w7AH9l-ejG`) are active and cover World Cup
highlights in the meantime.

## Tournament-groups cache

Euro Cup, World Cup, Champions League, and Copa América each write a JSON file to
`tournament-groups/{slug}.json`.  The Flutter app reads these files via
`TeamCacheService.fetchTournamentData()` to populate the Groups and Knockout tabs.

### Writers

`fetch-highlights.yml` is the **single refresher of the FD tournament cache** — on
its **~15-minute** cadence it rebuilds the *whole* match write (standings + knockout
**scores + status** + group fixtures) and grafts `video_id`s, all via
`scripts/sync_tournaments.py`.  `sync-teams.yml` is now purely weekly roster /
TeamLists work (`sources.json`) and **no longer writes `tournament-groups/`**.

> **Why this changed.**  Previously the FD tournament cache — including knockout
> **scores and status** — was written only by the weekly `sync-teams.yml`
> (Mondays 04:00 UTC), while `fetch-highlights.yml` grafted just the `video_id`s
> every 4 hours.  So during a live tournament a game that finished mid-week
> showed **no score for up to a week** (fresh video link, stale scoreline).  The
> full score/status write now runs on the frequent cadence, so a finished game
> populates its score **within one highlights cycle** instead of on the next
> Monday.

> **Cadence: every 15 minutes (was 30 min → was 4 h `0 */4 * * *`).**  Budget
> analysis cleared it against both APIs: **football-data.org** (free tier —
> 10 req/min, **no daily cap**) is respected by the in-run `FD_SLEEP_SECONDS=6`
> throttle regardless of cadence (15 FD calls/run over ~90 s; ~1,440 calls/day
> at 15 min — still no daily cap concern on the free tier), and **YouTube** stays
> far under its 8,000 units/day cap (steady-state ≈ near-zero via smart-skip —
> ~7 units/day observed).  A run takes ~1–3 min under normal load.
> _(If the FD key is ever moved to a metered/paid plan, re-check ~1,440 calls/day
> against that plan's daily cap.)_
>
> **Concurrency guard (`cancel-in-progress: false`).**  A `concurrency:` block
> (group `fetch-highlights`) prevents overlapping runs by construction — if a run
> is in progress when the next 15-min slot fires, the new run queues rather than
> cancelling the active one.  At most one queued run exists at a time (later
> triggers replace the pending slot, not the active one), so the FD 10-req/min
> guarantee is never broken by two concurrent runs and no commit is interrupted
> mid-write.  Skip/queue was chosen over cancel-in-progress to preserve an
> in-flight tournament-groups write.

| Slug | What is written | Pipeline | Schedule |
|---|---|---|---|
| `euro-cup` | Full structure (standings + knockout scores/status + group fixtures) + `video_id` graft | `fetch-highlights.yml` → `scripts/sync_tournaments.py` (FD `/standings` + `/matches`) | **Every ~15 minutes** |
| `world-cup` | Full structure (standings + knockout scores/status + group fixtures) + `video_id` graft | `fetch-highlights.yml` → `scripts/sync_tournaments.py` (FD `/standings` + `/matches`) | **Every ~15 minutes** |
| `ucl` | Full structure (standings + knockout scores/status + group fixtures) + `video_id` graft | `fetch-highlights.yml` → `scripts/sync_tournaments.py` (FD `/standings` + `/matches`) | **Every ~15 minutes** |
| `copa-america` | Full structure (standings + fixtures + scores) | `scripts/sync_copa_tournament.py` (API-Sports `/fixtures`) | Monday 05:00 UTC |
| `copa-america` | `video_id` graft only (scores untouched) | `fetch-highlights.yml` → `sync_tournaments.py` (local reads, no API calls) | Every ~15 minutes |
| _(roster / TeamLists in `sources.json` — not `tournament-groups/`)_ | Team lists | `sync-teams.yml` (FD `/teams`) | Monday 04:00 UTC |

`sync_tournaments.py` makes only football-data.org calls (2 per FD tournament —
`/standings` + `/matches` — × 3 = 6 calls/run, throttled to the free-tier
10 req/min limit).  It adds **no** YouTube or API-Sports quota: these are the same
FD calls the weekly roster job used to make, simply moved to the frequent cadence.
Copa América's **scores** stay owned by its own weekly API-Sports job — the
frequent path never calls API-Sports and never forces FD fields onto Copa's
schema; it only grafts Copa's `video_id`s from the local highlights cache.

### Top-level shape

```json
{
  "generated_at": "2026-07-01T13:07:43Z",
  "slug": "copa-america",
  "standings":    [...],
  "matches":      [...],
  "groupMatches": [...]
}
```

### `standings` — aggregated group table

One entry per group (`type: "TOTAL"`).  Consumed by the Groups tab standings table.

```json
{
  "group": "GROUP_A",
  "type":  "TOTAL",
  "table": [
    {
      "position": 1,
      "team": { "id": 26, "name": "Argentina", "tla": "ARG", "crest": "https://…" },
      "playedGames": 3, "won": 3, "draw": 0, "lost": 0,
      "goalsFor": 7, "goalsAgainst": 1, "goalDifference": 6,
      "points": 9, "form": "WWW"
    }
  ]
}
```

### `matches` — knockout-stage fixtures

One entry per knockout match.  Consumed by the Flutter app's Knockout tab, which renders matches grouped by stage with a horizontally scrollable stage tab strip (All · Round of 16 · Quarter-finals · Semi-finals · 3rd Place · Final — only tabs for stages present in the data are shown).  Each stage tab carries a colour-coded status dot (green = complete, orange = in progress, red = not started) derived from match scores; the dot colours are defined in `KnockoutTabTheme` (a Flutter `ThemeExtension`) so they live in the theme, not in the widget.  The selected stage resets to "All" when the user navigates away and back.

```json
{
  "id":    1010,
  "stage": "QUARTER_FINALS",
  "homeTeam": { "id": 26, "name": "Argentina", "tla": "", "crest": "https://…" },
  "awayTeam": { "id": 2382, "name": "Ecuador", "tla": "", "crest": "https://…" },
  "score": {
    "fullTime":  { "home": 1, "away": 1 },
    "penalties": { "home": 4, "away": 2 }
  },
  "video_id": "abc123XYZ"
}
```

**`id`** — source match ID (FD fixture ID for Euro/WC/UCL; API-Sports fixture ID for Copa América). Used by the pipeline to look up the corresponding highlight entry.

**`video_id`** — YouTube video ID embedded at sync time by cross-referencing the match `id` against the corresponding `highlights/{slug}/{stem}.json` file. `null` when no highlight has been found yet.  The Flutter app renders a "Highlights" affordance on finished ties that carry a non-null `video_id`; tapping it navigates to `MatchHighlightScreen`.  Ties with `video_id: null` show "No highlights yet."

The entire knockout write — **scores, status, and `video_id`s** — is refreshed
inside `fetch-highlights.yml` (via `scripts/sync_tournaments.py`) on every ~15-minute
run, so both a finished game's scoreline and its matched video become visible in
the app within one highlights cycle.  `sync_tournaments.py` preserves any
`video_id`s already on disk while rebuilding from FD, then re-grafts them from the
freshly-written highlights — a rebuild never wipes highlight links.

#### Freshness & live fallback

The Flutter app treats the pre-built cache as stale after 7 days
(`TeamCacheService._staleDays = 7`) and then falls back to a live football-data.org
call (`FootballDataService.getKnockoutMatches`).  Because the tournament cache now
refreshes every ~30 minutes, it never reaches that 7-day window under normal
operation — the freshness check remains only as a safety net.  As defence in
depth, the live fallback's stage filter now includes `LAST_32`, so even if it ever
fires it returns Round-of-32 ties rather than silently dropping them (the cache
path already included `LAST_32`; the two are now aligned).

#### Rejected alternatives

- **Daily roster cron** — running `sync-teams.yml` daily would invoke the full football-data.org roster sequence 7× per week for no roster benefit; team lists change on a transfer/season cadence, not a daily one.  (The tournament *scores* moved to the frequent cadence; the *roster* stays weekly.)
- **Separate `tournament-refresh` workflow triggered via `workflow_run`** — adds a cross-workflow dependency and a second failure surface.  The tournament refresh is instead consolidated **inside** the existing `fetch-highlights.yml` run: it reuses that job's football-data.org access for the score/status rebuild and reads local highlights files for the `video_id` graft, so no new workflow and no `workflow_run` chain are introduced.

Penalty-shootout score handling: Football-Data.org sets `score.fullTime` to the
penalty tally when `score.duration == "PENALTY_SHOOTOUT"`; the regulation result
lives in `score.regularTime`.  The Flutter `KnockoutMatch.fromJson()` checks
`duration` and reads `regularTime` for the headline score in that case.
Copa América (API-Sports) always places the regulation result in `score.fullTime`,
so no special handling is needed there.

### `groupMatches` — group-stage fixtures

One entry per group-stage fixture.  Consumed by the Flutter app's group detail screen (`GroupStandingsScreen`), which renders them below the standings table grouped by matchday with a view-local matchday filter chip.
Competitions without a group stage (e.g. UCL from the 2024-25 league-phase format)
emit `groupMatches: []`.

```json
{
  "group":       "GROUP_A",
  "matchday":    1,
  "sourceRound": "Group Stage - 1",
  "homeTeam": { "id": 26, "name": "Argentina", "tla": "", "crest": "https://…" },
  "awayTeam": { "id": 5529, "name": "Canada",   "tla": "", "crest": "https://…" },
  "score": {
    "fullTime": { "home": 2, "away": 0 }
  },
  "status": "FT"
}
```

**Matchday normalisation rule** — both pipelines produce an integer `matchday`:

| Source | Raw value | Normalised to |
|---|---|---|
| Football-Data.org (Euro, WC, UCL) | integer `matchday` field on the match object | stored directly as integer |
| API-Sports (Copa América) | string `league.round` = `"Group Stage - N"` | `N` extracted via regex |

**`sourceRound`** preserves the raw API value for traceability:
- FD: `"Matchday N"` (derived from the integer)
- API-Sports: verbatim round string (e.g. `"Group Stage - 1"`)

**`status`** values differ by source: FD uses `"FINISHED"` / `"SCHEDULED"`;
API-Sports uses `"FT"` / `"NS"`.  The app handles both.

### API-Sports quota — hard invariants

1. `scripts/sync_copa_tournament.py` is the **only** source of API-Sports tournament calls — exactly 2 calls per run (standings + fixtures).
2. The 30-minute incremental highlights workflow **never** triggers API-Sports.
3. The Flutter app **never** calls API-Sports directly.

Copa 2024 is in the free-tier window (seasons 2022–2024).  A future edition outside
that window is a separate paid-plan decision.

## Diagnostics

Diagnostic scripts live in `diagnostics/` and are **manual-only** — they are never
wired into any GitHub Actions workflow and must always be committed with `[skip ci]`.

### `diagnostics/apisports_probe.py` — API-Sports season/round probe

Verifies whether Copa America and UEFA Europa League target seasons are available on
the [API-Sports](https://www.api-sports.io/) free tier (100 req/day) and captures the
exact `league.round` strings the API returns — the strings that the pipeline's Round/Stage
filter normalization will later key off.

> **Free-tier limitation:** The API-Sports free plan only covers seasons **2022–2024**.
> Querying 2025 or later returns a paywall error (`"Free plans do not have access to this
> season"`).  This makes the free tier a **historical backfill source only** — it cannot
> cover the current season.  Live/current-season coverage requires a paid plan.
> Canonical league IDs are pinned in the script (Copa America = id 9, Europa League = id 3)
> to avoid accidentally selecting women's or youth variants.

**Prerequisites**

```bash
pip install requests   # already present in the pipeline venv
export APISPORTS_API_KEY=<your-key>   # never hardcode
```

**Run**

```bash
python diagnostics/apisports_probe.py
```

**What it does (read-only, ~5 API calls)**

1. Calls `/status` and aborts early if fewer than 10 daily calls remain.
2. Searches `/leagues?search=copa+america` and `/leagues?search=europa+league`,
   printing every matched league with its full seasons table (year, start/end dates,
   `fixtures.events` coverage flag) so you can verify the canonical IDs.
3. Resolves the canonical entry for each competition (pinned ID first, name-filter
   fallback) and selects the newest season with year ≤ 2024 and `fixtures.events = True`.
   Seasons > 2024 are never queried — a `SeasonLockedError` is caught and logged if one
   somehow slips through.
4. Reports fixture count, explicit `NOT COVERED` if the API returns 0 results, and
   the complete ordered list of distinct `league.round` strings.
5. Writes `diagnostics/apisports_probe_report.md` with identical content.

**Throttling:** 7 s sleep between calls — safely under the 10 req/min free-tier cap.

**No side effects:** does not modify `fetch_highlights.py`, `backfill_highlights.py`,
`highlights_common.py`, any competition config, or `quota-tracker.json`.

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
