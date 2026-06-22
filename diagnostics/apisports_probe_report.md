# API-Sports Probe Report — 2026-06-22T00:09 UTC

**Read-only diagnostic** — no pipeline file, config, or quota tracker is modified.

> **Free-tier constraint:** API-Sports free plan covers seasons 2022–2024 only.  Seasons 2025+ return a paywall error and are never queried.

---
## 1. Quota check (`/status`)

- `requests.current`   : **2**
- `requests.limit_day` : **100**
- Estimated remaining  : **98**

---
## 2. League discovery

### 2.1 `copa america` → Copa America

**2 result(s) returned**

#### id=9  'Copa America'  (type='Cup', country='World')

Seasons (5 total):

| year | start | end | current | fixtures.events |
|------|-------|-----|---------|-----------------|
| 2024 | 2024-06-21 | 2024-07-15 | True | True |
| 2021 | 2021-06-13 | 2021-07-11 | False | True |
| 2019 | 2019-06-15 | 2019-07-07 | False | True |
| 2016 | 2016-06-04 | 2016-06-27 | False | True |
| 2015 | 2015-06-11 | 2015-07-04 | False | True |

#### id=926  'Copa America Femenina'  (type='Cup', country='World')

Seasons (2 total):

| year | start | end | current | fixtures.events |
|------|-------|-----|---------|-----------------|
| 2025 | 2025-07-12 | 2025-08-02 | True | True |
| 2022 | 2022-07-08 | 2022-07-31 | False | True |

### 2.2 `europa league` → UEFA Europa League

**1 result(s) returned**

#### id=3  'UEFA Europa League'  (type='Cup', country='World')

Seasons (13 total):

| year | start | end | current | fixtures.events |
|------|-------|-----|---------|-----------------|
| 2026 | 2026-07-09 | 2026-07-30 | True | False |
| 2025 | 2025-07-10 | 2026-05-20 | False | True |
| 2024 | 2024-07-11 | 2025-05-21 | False | True |
| 2023 | 2023-08-08 | 2024-05-22 | False | True |
| 2022 | 2022-08-04 | 2023-05-31 | False | True |
| 2021 | 2021-08-03 | 2022-05-18 | False | True |
| 2020 | 2020-08-18 | 2021-05-26 | False | True |
| 2019 | 2019-06-27 | 2020-08-21 | False | True |
| 2018 | 2018-06-26 | 2019-05-29 | False | True |
| 2017 | 2017-06-29 | 2018-05-16 | False | True |
| 2016 | 2016-06-28 | 2017-05-24 | False | True |
| 2015 | 2015-06-30 | 2016-05-18 | False | True |
| 2014 | 2014-07-01 | 2015-05-27 | False | True |

---
## 3. Fixtures probe (newest free-tier season per canonical league)

> **Free-tier window: 2022–2024.**  Only seasons within this range are queried.  Current seasons (2025+) require a paid plan.

> **Canonical IDs are pinned** (id=9 for Copa America, id=3 for Europa League).  Name-filter heuristic is used only if the pinned ID is absent from search results.

### Copa America

**Selected:** id=9  'Copa America'
**Target season:** 2024  (2024-06-21 → 2024-07-15)  *(newest ≤ 2024 with coverage)*

**Fixture count:** 32
**Distinct `league.round` strings** (7) — in order of first appearance:

- `Group Stage - 1`
- `Group Stage - 2`
- `Group Stage - 3`
- `Quarter-finals`
- `Semi-finals`
- `3rd Place Final`
- `Final`

### UEFA Europa League

**Selected:** id=3  'UEFA Europa League'  *(excludes: `conference`, `qualification`, `qualifying`, `play-off`, `championship`, `reserve`, `youth` + gender/age variants)*
**Target season:** 2024  (2024-07-11 → 2025-05-21)  *(newest ≤ 2024 with coverage)*

**Fixture count:** 269
**Distinct `league.round` strings** (17) — in order of first appearance:

- `1st Qualifying Round`
- `2nd Qualifying Round`
- `3rd Qualifying Round`
- `Play-offs`
- `League Stage - 1`
- `League Stage - 2`
- `League Stage - 3`
- `League Stage - 4`
- `League Stage - 5`
- `League Stage - 6`
- `League Stage - 7`
- `League Stage - 8`
- `Knockout Round Play-offs`
- `Round of 16`
- `Quarter-finals`
- `Semi-finals`
- `Final`

---
## 4. Summary

**Total API-Sports calls this run: 5**

**Free-tier coverage window: 2022–2024.**  Seasons 2025+ require a paid plan.  The free tier is a historical backfill source only — it cannot cover the current season.

| Competition | League ID | Season | Span | Status | Fixture count |
|-------------|-----------|--------|------|--------|---------------|
| Copa America | 9 | 2024 | 2024-06-21 → 2024-07-15 | COVERED | 32 |
| UEFA Europa League | 3 | 2024 | 2024-07-11 → 2025-05-21 | COVERED | 269 |

### Round strings by competition

**Copa America** (league id=9, season 2024):
- `Group Stage - 1`
- `Group Stage - 2`
- `Group Stage - 3`
- `Quarter-finals`
- `Semi-finals`
- `3rd Place Final`
- `Final`

**UEFA Europa League** (league id=3, season 2024):
- `1st Qualifying Round`
- `2nd Qualifying Round`
- `3rd Qualifying Round`
- `Play-offs`
- `League Stage - 1`
- `League Stage - 2`
- `League Stage - 3`
- `League Stage - 4`
- `League Stage - 5`
- `League Stage - 6`
- `League Stage - 7`
- `League Stage - 8`
- `Knockout Round Play-offs`
- `Round of 16`
- `Quarter-finals`
- `Semi-finals`
- `Final`

