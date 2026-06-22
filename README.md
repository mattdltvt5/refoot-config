# refoot-config

Remote channel configuration for the **ReFoot Highlights** Android app.

## Files

| File | Purpose |
|---|---|
| `sources.json` | YouTube channel/playlist IDs for all tiers (read by the app at runtime) |
| `admin.html` | Browser-based admin panel for managing `sources.json` |
| `uicons/` | Flaticon UIcons Bold Rounded webfont (used by the admin panel) |
| `highlights/` | Pre-built video metadata written by the fetch-highlights Action |
| `scripts/highlights_common.py` | Shared utilities, title filter constants, `is_highlight_title()`, `_normalize()`, `team_tokens()`, and `TEAM_TITLE_ALIASES` |
| `scripts/fixture_providers.py` | Pluggable fixture provider layer — `FootballDataProvider` (football-data.org), `ApiSportsProvider` (API-Sports free tier), `ApisportsQuotaTracker`, and `APISPORTS_COMPETITIONS` config registry |
| `scripts/fetch_highlights.py` | Incremental update script (runs every 4 hours) |
| `scripts/backfill_highlights.py` | Full-season backfill script (manual trigger only) |
| `scripts/backfill_copa_america.py` | Copa America 2024 backfill script — fetches fixtures from API-Sports, finds highlights on YouTube (manual trigger only) |
| `scripts/clean_highlights.py` | One-time cleanup script — re-evaluates existing JSON files and removes false positives |
| `scripts/clean_wrong_fixture_videos.py` | Retroactive cleanup — removes videos stored for the wrong fixture (both-teams rule) |
| `scripts/debug_match.py` | Dry-run diagnostic — tests the fixture↔title matcher over cached data without hitting the YouTube API |
| `diagnostics/apisports_probe.py` | **Manual-only** API-Sports probe — verifies season coverage and captures round-string formats; see [Diagnostics](#diagnostics) |
| `.github/workflows/fetch-highlights.yml` | GitHub Action that runs the incremental script every 4 hours |
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
| **Incremental** | `fetch_highlights.py` | Every 4 hours (scheduled) | football-data.org | 8,000 YouTube units/day |
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

Copa America is intentionally **not** in `COMPETITION_CODE_MAP`, so the scheduled 4-hour fetch Action never touches it.  To populate Copa America data, trigger the dedicated Action manually:

**Actions tab → Backfill Copa America highlights → Run workflow**

This fetches all 32 Copa America 2024 fixtures from API-Sports and searches YouTube for highlights using the same tier-waterfall logic as the main backfill.  Fixture data is written to `highlights/copa-america/matchday-{1,2,3}.json`, `quarter-final.json`, `semi-final.json`, `third-place.json`, and `final.json`.

Required secrets: `APISPORTS_API_KEY` (API-Sports key) and `YOUTUBE_API_KEY`.

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
