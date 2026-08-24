# Black-crest audit — club crests rendering as solid black silhouettes

**Read-only audit (2026-08-24).** No crest, asset, widget, or cache file was changed.
This report inventories every crest reference in the cache, flags the ones that render
as a flat black silhouette on the Home feed (like Le Mans FC), assigns a root-cause
bucket per crest, and recommends a fix per bucket. Fixes are a **separate follow-up**.

## Method

- **Inventory:** enumerated every unique crest URL across the cache — `home-index/*.json`
  (`homeTeam`/`awayTeam.crest`), `standings/*/*.json`, and `tournament-groups/*.json`.
  **191 unique crest URLs** (56 SVG, 135 raster).
- **Detector** (`tool`-style dev script, dev-side fetch only — not the client):
  - *Raster (png):* over **opaque** pixels only, compute the near-black fraction (R,G,B < 40)
    and the "colourful" fraction (saturation > 0.25, value > 60). Flag only if opaque pixels
    are **> 95 % near-black AND < 1 % colourful** — a flat single-colour black silhouette. This
    deliberately does **not** flag dark-but-multicoloured crests.
  - *SVG:* count **inline presentation colour fills** (`fill=`/`stroke=` directly on shapes,
    excluding `<style>` contents). Flag if there are **0 inline colour fills** and the colours
    come only from a `<style>` block / CSS classes / `currentColor` / gradient `url()` — because
    `flutter_svg` 2.3.0 does not apply those, so every shape falls back to default **black**.
- **Validation:** the detector flags **Le Mans FC** (the known-positive) and does **not** flag
  legitimately dark-but-coloured crests (guard sample below).

### False-positive guard (darkest crests that were correctly NOT flagged)

| Team | near-black % (opaque) | colourful % | flagged? |
|---|---:|---:|---|
| Venezia FC | 55.9 | 8.55 | no |
| SC Freiburg | 54.3 | 0.00 | no (only 54 % black) |
| Wolverhampton Wanderers FC | 53.9 | 35.37 | no |
| AC Pisa 1909 | 49.5 | 25.42 | no |
| Borussia Mönchengladbach | 48.5 | 0.00 | no |

## Flagged crests (2)

| Team | Competition | Format | Source | Bucket | Evidence |
|---|---|---|---|---|---|
| **Le Mans FC** | Ligue 1 | SVG | `upload.wikimedia.org/wikipedia/en/5/57/Le_Mans_FC_logo.svg` | **B** | 14 `<path>`s, **0 inline colour fills**; all fills come from a CSS `<style>` block (`.cls-N{fill:…}`), several are **gradient `url(#Dégradé…)`** refs → flutter_svg 2.3.0 renders every path black. |
| **Juventus FC** | Serie A | PNG | `crests.football-data.org/109.png` | **A** | 200×200, **100 % of opaque pixels near-black, 0 % colourful** → the source PNG is a flat black shape (the logo's white areas are transparent, so only black opaque pixels remain). |

Both fetch with HTTP 200 (no load failure), so **Bucket C does not apply**. The `TeamCrest`
widget applies **no `ColorFilter`/tint/blendMode** on either the SVG or raster path, so
**Bucket D does not apply**. (Its load-failure fallback is a grey football `Icon`, not a black
shape, so a failed load would look like a football glyph, not a black silhouette.)

## Le Mans FC — root cause (end to end)

- **Source:** a **manual override** in `sources.json` → `teamLists["Ligue 1"]` → `Le Mans FC` →
  `"crestUrl": "https://upload.wikimedia.org/wikipedia/en/5/57/Le_Mans_FC_logo.svg"` (a Wikipedia
  SVG, presumably added because football-data has no Le Mans crest). Flows into `standings` /
  `home-index`, which the Home feed reads.
- **Format:** SVG whose 14 paths are styled **only** via a `<style>` block of CSS classes
  (`.cls-1{fill:url(#Dégradé_sans_nom_476)} .cls-3{fill:#fff} …`) — a mix of **gradient
  references and flat colours**, with **no inline `fill=`** on any shape.
- **Render:** `TeamCrest` draws it with `SvgPicture.network(...)` (no tint). **flutter_svg 2.3.0
  does not apply the `<style>`-block class fills (nor the accented-id gradient `url()` refs)**, so
  all 14 paths use the default fill (**black**) → the crest renders as a solid black silhouette.
- **Bucket: B.** It is the **only** SVG among the 56 with a `<style>` block / zero inline fills;
  every other SVG crest uses inline presentation fills and renders correctly.

## Recommended fix — per bucket (not implemented here)

- **Bucket A — Juventus FC (black PNG source).** Football-data also hosts **`109.svg`** (HTTP 200)
  with **inline fills `#131516` + `#ffffff`** and no `<style>` block — it renders the full
  black-and-white logo correctly. **Recommended:** source Juventus's crest from the **`.svg`**
  instead of the `.png` (e.g. rewrite `.png`→`.svg` for football-data crests in the pipeline, or a
  per-team crest override). General rule: prefer FD `.svg` over `.png` for crests whose PNG is a
  transparent-background monochrome shape.
- **Bucket B — Le Mans FC (CSS/gradient-styled SVG).** Options, cleanest first:
  1. **Replace the manual override** in `sources.json` `teamLists` with a crest that renders in
     flutter_svg — either a **raster PNG** (edge-to-edge, colour) or an **SVG that uses inline
     presentation fills** (no `<style>`/class/gradient dependence). Simplest and data-driven.
  2. **Normalise the SVG at the source** — flatten the `<style>` classes into inline `fill=`
     attributes and inline/resolve the gradients — and host the corrected file. More work.
  3. Prefer a raster crest source for teams lacking an inline-fill SVG.
- **Bucket C — load failure (none found).** If one appears later (404/decode): fix or replace the
  failing asset URL in the cache/source; the widget already degrades to a football placeholder.
- **Bucket D — render-path tint (not present).** `TeamCrest` applies no colour filter; no change
  needed. If a tint is ever added, exclude crests.

## Scope note

Both fixes are **data/provider-level** (crest source in `sources.json` / the pipeline), not
`TeamCrest` widget changes — consistent with only two teams being affected while every crest uses
the same widget. No Firebase redeploy is warranted for this audit (no user-visible change).
