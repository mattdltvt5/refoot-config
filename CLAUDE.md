# ReFoot Config — Claude instructions

## Icon usage (ENFORCED)

**Always use the UIcons Bold Rounded icon font for every icon in the Web Admin (`admin.html`) and any app UI in this repo.**

The font is loaded via:
```html
<link rel="stylesheet" href="uicons/css/uicons-bold-rounded.css">
```

Usage:
```html
<i class="fi-br-{icon-name}"></i>
```

**Never use:**
- Unicode symbols / emoji as icons (e.g. `✓`, `✗`, `×`, `→`, `▶`, `⚙`, `📋`)
- Inline SVG icons
- Any external icon CDN (Font Awesome, Material Icons, etc.)
- Plain text characters standing in for icons

**Before adding any new icon**, search the available set first:
```bash
grep -o "fi-br-[a-z-]*" uicons/css/uicons-bold-rounded.css | sort -u | grep <keyword>
```

Common icons already in use in this project:

| Purpose | Class |
|---|---|
| Expand/collapse chevron | `fi-br-angle-right` |
| Delete / close | `fi-br-cross` |
| Delete (small, inline) | `fi-br-cross-small` |
| Confirm / covered | `fi-br-check` |
| Film / highlights | `fi-br-film` |
| Channel | `fi-br-channel` |
| Playlist | `fi-br-list` |
| Play button | `fi-br-play` |

## Repository overview

- `sources.json` — YouTube channel/playlist IDs, team lists and team colours (`teamPalettes`); read by the pipeline scripts, and by the ReFoot Flutter web app at runtime (teams + palettes). The old Android app is retired.
- `admin.html` — browser-based admin panel for managing `sources.json`
- `uicons/` — Flaticon UIcons Bold Rounded webfont (local, no CDN dependency)
- `highlights/` — pre-built video metadata written by GitHub Actions
- `scripts/highlights_common.py` — shared utilities for both highlight scripts
- `scripts/fetch_highlights.py` — incremental update (runs every 4 hours via Actions)
- `scripts/backfill_highlights.py` — full-season backfill (manual `workflow_dispatch` only)
- `.github/workflows/` — 14 workflows; check each file's `on:` for its trigger. Main ones:
  - `fetch-highlights.yml` (scheduled main run: highlights, `sync_tournaments`, `sync_standings`, `build_home_index`, `push_events` goal/result alerts, `commit_gate`)
  - `backfill-highlights.yml` / `backfill-copa-america.yml` (manual backfills)
  - `sync-standings.yml` (league tables), `sync-teams.yml` (team lists), `sync-copa-tournament.yml` (Copa América)
  - `run-tests.yml` (pytest, `scripts/tests`), `pages.yml` (admin + legal pages on GitHub Pages)
  - diagnostics / discovery: `audit-no-highlight-buckets.yml`, `detect-alias-gaps.yml`, `discover-playlists-readonly.yml`, `discover-season-playlists.yml`, `find-channel-candidates.yml`, `verify-playlist-owners.yml`

## Git / commit rules

- Always commit with `[skip ci]` in the message for any commit that touches `highlights/` or other data files, to prevent the fetch-highlights Action from re-triggering and burning YouTube quota
- Never force-push to `main`
- Use `gh` CLI (at `D:\4_Programs\gh.exe`) for GitHub operations

### Firebase deployment (Flutter app changes)

The Flutter web app is served by Firebase Hosting at `refoot-highlights-app.web.app`.
Source changes are NOT visible to the user until the web build is rebuilt and redeployed.

- After completing any change that affects the deployed Flutter web app, redeploy to Firebase Hosting as the final step, so the change is visible in the live app. Do not end the task with an undeployed Flutter change.
- If you cannot safely auto-deploy for any reason (build failure, uncertainty about the command, or anything ambiguous), explicitly ask whether to redeploy before finishing — never silently skip it.
- Deploy recipe (in `refoot_flutter`): build from the deploy worktree `D:/Projects/01_ReFoot/_wt_deploy_flutter` checked out (detached) at `origin/master`, run `bash tool/build_web.sh` (it stamps the version/build shown in Menu → About ReFoot; a plain `flutter build web` doesn't), then `npx.cmd firebase deploy --only hosting` (`npx.cmd` on Windows: PowerShell blocks the `.ps1` shim). Check `npx.cmd firebase use` says `refoot-highlights-app` first. A new web plugin needs `flutter clean; flutter pub get` before the build. Verify by comparing the md5 of `build/web/main.dart.js` with the live `https://refoot-highlights-app.web.app/main.dart.js`.
- Do NOT deploy if the build fails — report the failure instead of pushing a broken build live.
- Do NOT redeploy Firebase for `refoot-config` pipeline or data-only changes. Those reach the app via the GitHub push to `raw.githubusercontent.com`, not Firebase. Firebase redeploy applies only to Flutter app source changes.
