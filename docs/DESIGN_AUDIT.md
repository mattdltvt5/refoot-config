# Web Admin — UI Component & Styling Audit

**Read-only inventory.** This document catalogues every UI component type and styling value
currently used by the Web Admin (the GitHub-Pages "config tool"), as raw material for a future
shared stylesheet + token set. It is **step 1 (audit only)** of the harmonization sequence
(audit → agree tokens → shared stylesheet → gallery → migrate). No existing file was changed and no
styling or behaviour was altered by producing this document.

Line references are into `admin.html` at the time of writing.

---

## 1. Scope & method

The Web Admin is a **single, self-contained HTML page**. All markup, CSS, and JS live in one file;
there are no separate admin `.css` or `.js` files and no second HTML page. Findings below come from
reading that file's `<style>` block, its static body, and the JS that generates the dynamic markup.

## 2. File inventory

| File | Role | Notes |
|---|---|---|
| `admin.html` (repo root, 2584 lines) | **The entire Web Admin.** | One inline `<style>` block (lines 11–680), the static HTML body (682–823), and one inline `<script>` (825–2584) that builds all dynamic markup (competition blocks, team rows, coverage panel, candidate cards). |
| `uicons/css/uicons-bold-rounded.css` | Third-party **UIcons Bold Rounded** icon webfont (`fi-br-*` glyphs). | Referenced via `<link>`. Provides icon glyphs only — not admin styling. |
| *(external)* Google Fonts **Roboto** (400/500/700) | Body font. | Loaded from `fonts.googleapis.com`. |
| *(embedded)* base64 PNG logo | Header logo (`.header-logo`). | Inlined as a `data:` URI (line 755). |

There is **no** `.nojekyll`, `_config.yml`, or `CNAME`; the page is served directly at
`mattdltvt5.github.io/refoot-config/admin.html`.

## 3. Styling mechanisms in use

1. **One page-level `<style>` block** (lines 11–680) — the bulk of styling, as CSS classes.
2. **A `:root` custom-property block** (lines 12–25) — 12 design tokens already exist (see §4).
3. **13 inline `style="…"` attributes** — mostly `display:none` toggles, plus a few one-off
   font/colour overrides (lines 710, 726, 727, 742, 777, 786, 820, 1443, 1446, 1568, 2402, 2403, 2579).
4. **JS-generated markup** — the `<script>` builds most components with template strings that reuse
   the CSS classes, with a handful of computed inline styles (e.g. a status dot's `background` at 1568,
   an error block at 2402–2403, and a hardcoded `color:#757575` at 2579 that bypasses `var(--muted)`).

## 4. Tokens that already exist (`:root`, lines 12–25)

```
--blue      #2196F3    --white   #FFFFFF    --muted   #757575
--blue-dark #1976D2    --bg      #F5F5F5    --green   #4CAF50
--blue-bg   #E3F2FD    --border  #E0E0E0    --yellow  #FF9800
--black     #000000    --text    #212121    --red     #F44336
```

These are used widely — but many components also hardcode colours *outside* this set, and there is
**no token scale for radius, spacing, font-size, shadow, or the dark-context (on-black) palette**.
Those are the primary gaps a future token set would fill.

---

## 5. Component catalogue

Legend for "styling": **C** = shared CSS class in the `<style>` block · **I** = inline `style=` ·
**J** = built in JS. Everything lives on the single page, so "where" = the region/feature that uses it.

### 5.1 Header / nav
- `header` (blue sticky bar, 56px, `box-shadow 0 2px 4px rgba(0,0,0,0.25)`), `.header-logo` (30px),
  `.header-right`. **C**, lines 36–47, 754–764.
- Header buttons: `.btn-ghost` (translucent white on blue), `.btn-icon` (36px circle). **C** 57–69.

### 5.2 Buttons — **21 distinct button treatments**
Base `button` (uppercase, 13px, `border-radius:4px`, `padding:7px 14px`, `letter-spacing:0.4px`), then:
`.btn-ghost`, `.btn-icon`, `.btn-blue`, `.btn-cred-save`, `.btn-cred-clear`, `.btn-reveal`,
`.debug-clear-btn`, `.drawer-close-btn`, `.cov-goto-btn`, `.cov-filter-btn` (+`.active`),
`.add-team-btn`, `.add-bcast-btn`, `.bcast-del-btn`, `.bcast-pl-add`, `.bcast-pl-del`,
`.wf-refresh`, `.cand-refresh`, `.cand-skip-btn`, `.cand-approve-btn`, `.yt-btn`/`.yt-btn-dark`.
**C**, scattered across 50–75, 176–182, 235–243, 273–285, 315–347, 395–418, 494–506, 556–578,
611–635, 654–677. Roughly split into: primary-filled (`.btn-blue`, `.btn-cred-save`, `.cand-approve-btn`),
translucent-on-blue (`.btn-ghost`, `.btn-icon`, `.drawer-close-btn`), outline/ghost-muted
(`.btn-cred-clear`, `.cand-skip-btn`, `.debug-clear-btn`), and bare icon buttons
(`.wf-refresh`, `.cand-refresh`, `.bcast-del-btn`, `.cov-goto-btn`). See §6 for their diverging values.

### 5.3 Cards / panels
- `#status-bar` — coverage dashboard card (`#fff`, `border-radius:4px`, `box-shadow 0 1px 3px rgba(0,0,0,0.12)`). **C** 90–95.
- `.comp-block` — competition card (same radius/shadow) + status accent variants
  `.status-green|yellow|red` (inset 4px left bar). **C** 189–195.
- `.cred-status-section` — status strip (`--bg`, `border-radius:6px`, 1px border). **C** 373–378.
- `.cand-head` / `.cand-body` / `.cand-card` — candidate review card (`border-radius:8px`, border only, no shadow;
  `.cand-card.approved` green border, `.cand-card.skipped` opacity .55). **C** 621–645.

### 5.4 List rows / "tables" (no `<table>` — all fl\x65x rows)
- `.cov-comp-row`, `.cov-gw-hdr`, `.cov-match-row` — coverage rows. **C** 123–175.
- `.team-row` (52px) — team list rows. **C** 248–259.
- `.bcast-row` (+`.editing`/`.dragging`/`.drag-over`) — broadcaster/playlist rows (draggable). **C** 471–486.
- `.cand-row` (+`.primary`/`.approved`) — candidate channel rows. **C** 646–657.
- `.wf-row` — pipeline run rows. **C** 613–614.

### 5.5 Form inputs / selects / checkboxes
- Text inputs — **6 variants** with differing borders/widths:
  `.form-input` (100%, `#BDBDBD` bottom-accent), `.ch-input` (210px) + `.ch-input.sm` (175px),
  `.ch-input-dark` (220px, on-black), `.bcast-name-input` (120px), `.pl-input` (256px) + `.pl-input.sm` (210px).
  **C** 226–271, 355–362, 487–518, 546–547.
- Validation states: `.valid` (green border) / `.bad` (red border) on `.ch-input`, `.ch-input-dark`, `.pl-input`. **C**.
- Reveal toggle: `.btn-reveal` (`👁`, joined to `.form-input`). **C** 391–403.
- Labels/hints: `.form-label`, `.form-hint`, `.form-group`. **C** 350–365.
- **No `<select>`, radio, or checkbox controls exist** — all toggling is via buttons/tabs/chips.

### 5.6 Badges / status pills / chips
- `.comp-badge` (`border-radius:10px`, on-black), `.wf-overdue-badge` (red, `radius:3px`),
  `.cand-approved-badge` (green, `radius:4px`), `.cand-rank-badge` (`radius:3px`),
  `.settings-badge` (8px red dot). **C** 219–223, 616, 652–653, 665–666, 420–425.
- Chips: `.field-chip` (light, `radius:12px`) and its twin `.field-chip-dark` (on-black). **C** 521–542.
- Status dots: `.dot` + `.dot-green|yellow|red`, `.status-dot` + `-set|-unset`, `.cand-verified`. **C** 183–186, 383–385, 530, 541.

### 5.7 Tabs — **two different treatments**
- `.drawer-tab` — **underline** style (full-width, `border-bottom:3px`, uppercase). **C** 321–330.
- `.cand-tab` — **segmented pill** style (bordered group, filled-blue active). **C** 627–632.

### 5.8 Modal / drawer
- `#settings-drawer` + `#settings-overlay` — right-slide **drawer** (400px, `rgba(0,0,0,0.4)` scrim). **C** 297–309.
- `.drawer-header`, `.drawer-tabs`, `.drawer-body`, `.drawer-pane`. **C** 310–336.
- `.modal-error` — inline error inside the drawer form. **C** 366–370. *(There is no centred modal dialog.)*

### 5.9 Alerts / banners / toast
- `#cors-banner` (yellow, `#FFF3E0` + left accent). **C** 81–87.
- `.cred-missing-banner` (yellow, `#FFF3E0`). **C** 386–390.
- `.comp-error` (red, `#FFF8F8` + `#FFCDD2` border). **C** 288–291.
- `.modal-error` (red, `#FFF8F8` + left accent). **C** 366–370.
- `#toast` (+`.success` `#388E3C` / `.error` `#D32F2F`; base `#323232`). **C** 457–468.

### 5.10 Empty states — **6 near-identical variants**
`.cov-empty`, `.empty-note`, `.debug-empty`, `.cand-empty`, `.wf-note`, `.cand-flag-note` — all
"muted, italic, centered/padded" but each defined separately with slightly different padding/size.
**C** 119–122, 292–295, 341, 617, 638, 679.

### 5.11 Loading states
- `#loading-state` + `.spinner` (32px, `--blue` top, `@keyframes spin`). **C** 447–455.
- Inline "Loading…" placeholders inside panels: `.cov-empty` "Loading coverage data…" (795),
  `.wf-note` "Loading run status…" (792), `.cand-empty` "Loading candidates…" (811).

### 5.12 Progress / meters
- `.cov-bar-wrap` + `.cov-bar` — coverage ratio bar (72px × 5px). **C** 136–137.

### Component × region matrix (single page)
| Component type | Header | Settings drawer | Coverage dashboard | Pipeline runs | Candidates | Competition blocks |
|---|---|---|---|---|---|---|
| Buttons | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Cards/panels | – | ✓ | ✓ | ✓ | ✓ | ✓ |
| List rows | – | – | ✓ | ✓ | ✓ | ✓ |
| Form inputs | – | ✓ | – | – | – | ✓ |
| Badges/pills/chips | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Tabs | – | ✓ (underline) | – | – | ✓ (segmented) | – |
| Drawer/modal | – | ✓ | – | – | – | – |
| Alerts/banners | – | ✓ | ✓ | – | – | ✓ |
| Toast | ✓ (global) | – | – | – | – | – |
| Empty states | – | ✓ | ✓ | ✓ | ✓ | ✓ |
| Loading | – | – | ✓ | ✓ | ✓ | ✓ (global) |

---

## 6. Divergence table (the inconsistencies to harmonize)

| Dimension | Conflicting values found | Where |
|---|---|---|
| **Border-radius** | `3px`, `4px`, `5px`, `6px`, `8px`, `10px`, `12px`, `50%` — no scale | cards 4/6/8; pills 3/4/10/12; inputs 4; toast 4 |
| **Card radius** | `4px` vs `6px` vs `8px` | `#status-bar`/`.comp-block` (4) · `.cred-status-section` (6) · `.cand-card`/`.cand-head` (8) |
| **Card elevation** | `box-shadow 0 1px 3px rgba(0,0,0,0.12)` vs **border-only** | shadow: status-bar, comp-block · border-only: cred-status-section, cand-card |
| **Green** | `#4CAF50` (`--green`), `#388E3C` (toast success), `#2E7D32` (log-ok) | 3 greens |
| **Red** | `#F44336` (`--red`), `#D32F2F` (toast error), `#C62828` (log-err), `#cc0000` (yt hover) | 4 reds |
| **Blue (info)** | `#2196F3` (`--blue`), `#1976D2` (`--blue-dark`), `#1565C0` (log-info) | 3 blues |
| **Input border grey** | `#BDBDBD` (13×, untokenized) vs `#E0E0E0` (`--border`) | `.form-input`/`.ch-input`/`.pl-input`/`.bcast-name-input` use `#BDBDBD`; other borders use `--border` |
| **Muted grey** | `#757575` (`--muted`) vs hardcoded `#757575` (line 2579), plus `#37474F` (debug text) | token bypassed in JS |
| **Warning text/bg** | text `#E65100`; bg `#FFF3E0`; accent `--yellow` `#FF9800` | cors-banner, cred-missing-banner, cov-filter hover |
| **Button padding** | `7px 14px` · `9px 22px` · `6px 14px` · `5px 12px` · `4px 10px` · `2px 6px` | base · btn-blue · cred-save · cand-tab · cand-skip · cov-goto |
| **Button case** | base = `uppercase` + `letter-spacing:0.4px`; ~9 buttons override `text-transform:none; letter-spacing:0` | cred-save/clear, drawer-close, cov-filter, debug-clear, cand-* |
| **Tabs** | underline (`.drawer-tab`) vs segmented pill (`.cand-tab`) | two unrelated tab systems |
| **Pill radius** | `3px` (rank/overdue) · `4px` (approved) · `10px` (comp-badge) · `12px` (field-chip) | badges vs chips |
| **Input width** | `100%` · `256px` · `220px` · `210px` · `175px` · `120px` | form-input / pl-input / ch-input-dark / ch-input / .sm / bcast-name |
| **Empty-state** | 6 separate definitions of the same "muted italic" pattern | §5.10 |
| **Dark-context alpha** | 17 distinct `rgba(255,255,255,α)` values, ad hoc | on-black comp-header + its inputs/chips/buttons |
| **Shadow/overlay alpha** | 6 distinct `rgba(0,0,0,α)` values (0.1/0.12/0.2/0.25/0.3/0.4) | scrims, card shadows, toast, header |

---

## 7. Full colour-palette inventory

*(HTML entities `&#9888;` (⚠) and `&#128065;` (👁) are **not** colours and are excluded.)*

### 7.1 Tokenised (`:root`) — 12
| Hex | Token | Representative use |
|---|---|---|
| `#2196F3` | `--blue` | primary, header, focus rings |
| `#1976D2` | `--blue-dark` | button hover, links |
| `#E3F2FD` | `--blue-bg` | ghost hovers, rank badge bg |
| `#000000` | `--black` | `.comp-header` background |
| `#FFFFFF` | `--white` | surfaces (also written as `#fff`, 25×) |
| `#F5F5F5` | `--bg` | page bg, hover fills, chips |
| `#E0E0E0` | `--border` | most borders/dividers |
| `#212121` | `--text` | body text |
| `#757575` | `--muted` | secondary text, labels |
| `#4CAF50` | `--green` | success dots/badges |
| `#FF9800` | `--yellow` | warning accents |
| `#F44336` | `--red` | error dots/borders |

### 7.2 Hardcoded hex (outside `:root`)
| Hex | # | Where |
|---|---|---|
| `#fff` / `#ffffff` | 25 / 1 | `color:#fff` on dark surfaces (duplicates `--white`) |
| `#BDBDBD` | 13 | input borders + `::placeholder` (`.form-input`, `.ch-input`, `.pl-input`, `.bcast-name-input`, `.btn-reveal`, hovers) |
| `#FFF8F8` | 3 | error backgrounds (`.comp-error`, `.modal-error`, `.bcast-del-btn:hover`) |
| `#FFF3E0` | 3 | warning backgrounds (`#cors-banner`, `.cred-missing-banner`, `.cov-filter-btn:hover`) |
| `#FAFAFA` | 3 | subtle row/section bg (`.cov-matches`, `.bcast-row`, `.add-bcast-btn`) |
| `#E8E8E8` | 2 | `.cov-gw-hdr` border, `.field-chip:hover` bg |
| `#E65100` | 2 | orange warning text (`#cors-banner strong`, `.cred-missing-banner`) |
| `#cc0000` | 2 | YouTube red (`.yt-btn:hover`) |
| `#757575` | 2 | hardcoded muted (line 2579 JS + one more) — duplicates `--muted` |
| `#FFEBEE` | 1 | `.cov-filter-btn.active` bg |
| `#FFCDD2` | 1 | `.comp-error` top border |
| `#FAFFFB` | 1 | `.cand-row.primary` bg (near-white green tint) |
| `#F2F2F2` | 1 | `.cov-gw-hdr` bg |
| `#F0F0F0` | 1 | `.cov-match-row` border |
| `#D32F2F` | 1 | `#toast.error` |
| `#C62828` | 1 | `.log-err` |
| `#388E3C` | 1 | `#toast.success` |
| `#2E7D32` | 1 | `.log-ok` |
| `#37474F` | 1 | `#debug-log` text |
| `#323232` | 1 | `#toast` base bg |
| `#1565C0` | 1 | `.log-info` |

### 7.3 `rgba()` values
- **White-alpha (on-black palette) — 17 distinct:** `.08 .1 .12 .14 .15 .18 .2 .22 .25 .28 .35 .4 .5 .55 .6 .75 .88` — used across `.comp-header`, `.comp-badge`, `.chevron`, `.ch-input-dark`, `.yt-btn-dark`, `.field-chip-dark`, `.btn-ghost`, `.btn-icon`, `.drawer-close-btn`, `.team-count-mobile`.
- **Black-alpha — 6 distinct:** `rgba(0,0,0,0.1|0.12|0.2|0.25|0.3|0.4)` — card shadows, header shadow, drawer shadow, toast shadow, `#settings-overlay` scrim, `.pl-input:focus` ring.
- **Brand-alpha — 3:** `rgba(33,150,243,0.45)` (`.btn-blue` shadow), `rgba(33,150,243,0.2)` (`.ch-input:focus` ring), `rgba(76,175,80,0.08)` (`.cand-row.approved` bg).

---

## 8. Structural duplication

1. **Light/dark twin components** — the same component is defined twice, once for white rows and once
   for the black `.comp-header`, with parallel `rgba(255,255,255,α)` values:
   - `.ch-input` ↔ `.ch-input-dark`
   - `.field-chip` ↔ `.field-chip-dark`
   - `.yt-btn` ↔ `.yt-btn-dark`
2. **Repeated "bare icon button" boilerplate** — `background:none; border:none; cursor:pointer; color:var(--muted); border-radius:…` recurs in `.wf-refresh`, `.cand-refresh`, `.bcast-del-btn`, `.bcast-pl-del`, `.cov-goto-btn`, `.debug-clear-btn`.
3. **Repeated text-input boilerplate** — `background:#fff; border:1px solid #BDBDBD; border-radius:4px; padding:6px 8px; font-size:12px; outline:none; transition:border-color .15s` recurs in `.ch-input`, `.bcast-name-input`, `.pl-input` (with only the width differing).
4. **Repeated pill/badge boilerplate** — `font-size:9–11px; font-weight:700; padding:1–2px 6–8px; border-radius:3–4px; text-transform:uppercase; letter-spacing:.04em` recurs in `.wf-overdue-badge`, `.cand-approved-badge`, `.cand-rank-badge`.
5. **Repeated empty-state boilerplate** — see §5.10 (6 copies of "muted italic, padded/centered").
6. **Repeated validation states** — `.valid`/`.bad` (green/red border) are declared separately on
   `.ch-input`, `.ch-input-dark`, and `.pl-input`.
7. **JS-built markup** — competition blocks, team rows, coverage rows, and candidate cards are assembled
   from template strings in the `<script>` (825–2584) reusing the classes above; a few computed inline
   styles appear (status-dot `background` at 1568; error block at 2402–2403; a hardcoded `#757575` at 2579).
   Because there is a single page, there is no cross-page markup copy-paste — the duplication is the
   light/dark twins and the boilerplate clusters above.

---

## 9. Summary — raw material for a future token set

The audit surfaces the consolidation targets (recorded, **not** prescribed here):

- **Colour:** 12 existing `:root` tokens **+ ~20 hardcoded hex + 26 ad-hoc `rgba()` values.** Notably a
  second untokenised grey (`#BDBDBD`), three greens, four reds, three blues, and a whole undefined
  on-black (`rgba(255,255,255,α)`) palette.
- **Radius:** 8 different values → wants a small scale (e.g. sm/md/lg/pill).
- **Spacing / padding:** button and card paddings vary ad hoc → wants a spacing scale.
- **Typography:** font-sizes 9–16px scattered; label/badge uppercase treatment applied inconsistently.
- **Elevation:** shadow-vs-border cards are inconsistent → wants an elevation decision.
- **Components to unify:** buttons (21 treatments → a few roles), inputs (6 → 1 base + width modifiers),
  tabs (2 systems → 1), badges/chips (several → a `pill` primitive with variants), empty states (6 → 1),
  and the light/dark twins (→ one component + a context/theme modifier).

These are the inputs to the **next** step (agree a token set); this document changes nothing.
