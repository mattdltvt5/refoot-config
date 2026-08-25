# Favorite-teams feasibility — team identity & home-index join (read-only findings)

**Read-only audit (2026-08-24).** No source, data, or config was changed. Purpose: can a
favorited club be reliably joined across its competitions and surfaced on the home
screen as a client-side filter over cached `home-index/{YYYY-MM}.json`?

## TL;DR verdict

- **Yes, one stable team ID exists at the pipeline/provider level** that joins a club across
  its domestic league and its European (UCL) fixtures: the **football-data.org numeric team
  `id`** (e.g. Real Madrid = `86`, Barcelona = `81`, Atlético = `78` — identical in
  `fixtures/laliga/2026.json` and `tournament-groups/ucl.json`).
- **But `home-index` does NOT currently carry any team ID** — its per-fixture team objects
  hold only `name` / `shortName` / `tla` / `crest`. So a client-side favorite filter over
  home-index today can only match by **name**, not by stable ID.
- **Therefore Step 3 requires a small pipeline change**: add the team `id` to the home-index
  team object so the favorite (stored as an FD id) is joinable against home-index. Details below.
- **ID-space caveat:** football-data and API-Sports use **different** id spaces (confirmed).
  Club competitions (domestic + UCL/UEL) are **all football-data** → one consistent id space.
  API-Sports is used only for **Copa América (national teams)** — not club favorites — but if a
  national team were ever favoritable, its Copa id would NOT join with football-data ids.

---

## 1. home-index per-fixture schema (what it carries)

Writer: **`scripts/build_home_index.py`** → `normalize_match()` + `_team()`; output
`home-index/{YYYY-MM}.json`. Per-fixture emitted schema:

```json
{
  "match_id": 537327,
  "homeTeam": { "name": "…", "shortName": "…", "tla": "…", "crest": "…" },
  "awayTeam": { "name": "…", "shortName": "…", "tla": "…", "crest": "…" },
  "homeScore": null, "awayScore": null,
  "status": "TIMED", "utcDate": "2026-…Z",
  "videoId": "…"   // optional, only when a highlight is matched
}
```

`_team()` returns **`{name, shortName, tla, crest}` only** — it **drops the team `id`** even
though the raw fixture it reads DOES carry `id`. So **home-index has no stable team identity**;
the only per-fixture team-level keys are the display name / short name / TLA / crest URL.

## 2. FootballDataProvider team identity (per-season fixture files)

Per-season files `{type}/{slug}/{season}.json` (written via `fixture_providers.py`
`FootballDataProvider._normalize_artifact`) carry a **stable numeric FD team id** per side.
Example — `fixtures/laliga/2026.json` `homeTeam`:

```json
{ "id": 263, "name": "Deportivo Alavés", "shortName": "Alavés", "tla": "ALA",
  "crest": "https://crests.football-data.org/263.png" }
```

Fields present: **numeric `id`**, `name`, `shortName`, `tla`, `crest`. (No slug.)

**Same club → same id across domestic + UCL — verified against real cached files**
(`fixtures/laliga/2026.json` vs `tournament-groups/ucl.json`):

| Club | LaLiga id | UCL id | Match |
|---|---:|---:|:--:|
| Real Madrid CF | 86 | 86 | ✅ |
| FC Barcelona | 81 | 81 | ✅ |
| Club Atlético de Madrid | 78 | 78 | ✅ |

All shared clubs match. **UCL is a football-data competition** (`sync_tournaments.py`, FD comp
`2001`), so its club ids share the domestic FD id space. (UEL = FD comp `2146` would be the
same space; note no `tournament-groups/europa-league.json` is currently cached.) Team **names**
are also byte-identical across these FD sources (same provider), so a name-based match is
currently possible too — but names are fragile (variants, disambiguation) vs the stable id.

## 3. Are home-index and per-season joinable on the same team-ID field?

**No — not by ID today.** Per-season files have `homeTeam.id`; home-index does **not** emit any
id (§1). They share only `name`. So:

- **Robust join (recommended):** add the FD team id to home-index. One-line-ish change in
  `build_home_index.py::_team()` — include `"id": raw.get("id")` (the raw fixture already has
  it). Then a favorite stored as an FD id filters home-index directly and unambiguously.
- **Interim join (no pipeline change):** match `home-index` `homeTeam.name` / `awayTeam.name`
  against the favorited club's name. Works today because FD names are canonical across its
  sources, but is fragile (name changes, non-ASCII, two clubs sharing a short name) and can't
  disambiguate — not recommended as the permanent key.

## 4. Competition-favorites storage (Flutter) — shape to mirror

File: **`lib/providers/competitions_provider.dart`**, class **`CompetitionsProvider`**
(`ChangeNotifier`).

- **SharedPreferences key:** `'favourite_competitions'` (const `_kFavsKey`).
- **Value format:** a **`List<String>`** via `prefs.setStringList` / `prefs.getStringList`.
- **What the strings are:** competition **display names** (e.g. `"Premier League"`) — `toggleFavourite(String name)` adds/removes the name; `isFavourite(name)` checks membership; `favouriteCompetitions` filters `kCompetitions.where((c) => _favourites.contains(c.name))`.
- **Read/write:** `loadFavourites()` reads (`getStringList` → in-memory `Set<String>` → `notifyListeners`); `toggleFavourite()` mutates the set, `notifyListeners()`, then `setStringList`.

Verbatim shape:
```dart
const _kFavsKey = 'favourite_competitions';           // SharedPreferences key
final saved = prefs.getStringList(_kFavsKey) ?? [];   // List<String> of competition NAMES
await prefs.setStringList(_kFavsKey, _favourites.toList());
```

To mirror for teams: e.g. key `'favourite_teams'`, a `List<String>` of **FD team ids** (as
strings) — preferred over names once home-index carries the id (§3). Storing ids avoids the
name-fragility that competition-name storage tolerates only because comp names are a small
fixed set.

## 5. Does a team-context screen exist? (inline-star candidate)

**Yes.** **`lib/screens/highlights_screen.dart`** → `HighlightsScreen` is the per-team screen:
constructor takes `competitionSlug`, `teamName`, `teamCrestUrl`, `teamList`. It's reached by
tapping a team (from `competition_detail_screen.dart` and `tournament_detail_screen.dart`).
This is the natural home for the future inline favorite star.

Caveat: `HighlightsScreen` identifies the team by **`teamName` (String)**, not an FD id — so
wiring a star to an id-based favorite would need the id threaded to this screen (it currently
receives name + crest + slug only).

## 6. ID-space mismatch flag (football-data vs API-Sports)

Confirmed different id spaces (same national team, different ids):

| National team | Copa (API-Sports) id | World Cup (football-data) id |
|---|---:|---:|
| Argentina | 26 | 762 |
| Ecuador | 2382 | 791 |
| Colombia | 8 | 818 |

- **Club favorites are unaffected:** domestic + UCL (+ UEL) are all football-data → single id
  space (§2). A favorited club can be stored + joined by its FD id everywhere it appears as a
  club side.
- **The mismatch only bites national teams:** Copa América is API-Sports (`sync_copa_tournament.py`),
  while WC/Euro national teams are football-data (`sync_tournaments.py`, comps `2000`/`2018`).
  If "favorites" ever include national teams, an API-Sports (Copa) id will not join FD data —
  a favorite would need per-provider ids or a name/normalized-key bridge. **Recommend scoping
  the feature to club teams (football-data) initially.**

---

## Recommendations (for the build, not done here)

1. **Add team `id` to home-index** (`build_home_index.py::_team()` → include `id`); this is the
   prerequisite that makes an ID-based favorite filter reliable. Without it, only a fragile
   name match is possible.
2. **Store favorites as football-data team ids** (`'favourite_teams'`, `List<String>`), mirroring
   `CompetitionsProvider`'s SharedPreferences pattern.
3. **Client-side filter only:** favorites surface by filtering already-cached `home-index`
   fixtures where `homeTeam.id`/`awayTeam.id` ∈ favorites — no fan-out, consistent with the
   existing home-index reader contract.
4. **Inline star on `HighlightsScreen`** (team context); thread the FD team id into that screen.
5. **Scope to club teams** (football-data id space); defer/national-teams handling given the
   API-Sports id mismatch.
