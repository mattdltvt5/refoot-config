#!/usr/bin/env python3
"""
rights_registry.py — season-scoped highlight/live rights registry.

The registry is CONFIG, edited by people: one file per season at
``rights/{season}.json`` (schema: ``rights/rights.schema.json``). It records
which broadcasters hold live or highlight rights for each competition and
launch country, with optional ``valid_from`` / ``valid_until`` dates for
mid-season changes. The pipeline never writes it.

Seasons follow the existing pipeline rule, not a new one:
``highlights_common.season_for_competition`` — the August threshold
(``season_utils.current_season``, mirrored by the Dart
``SeasonDateCalculator.currentSeasonYear()``) for leagues and UCL/UEL, and the
most recent edition year for tournament competitions (World Cup, Euro Cup,
Copa America), which are never cut by the August window.

A missing or unreadable season file yields empty results and a warning — it
never fails a pipeline run.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from highlights_common import COMPETITION_SLUG_MAP, REPO_ROOT, season_for_competition

log = logging.getLogger(__name__)

RIGHTS_DIR = REPO_ROOT / "rights"

_REQUIRED_KEYS = ("competition", "country", "holder", "role", "channels", "status")
_ROLES = {"live", "highlights"}
_STATUSES = {"unverified", "approved", "rejected"}

_SLUG_TO_NAME = {slug: name for name, slug in COMPETITION_SLUG_MAP.items()}


def competition_slug(competition: str) -> str:
    """Accept a sources.json competition name or a slug; return the slug."""
    return COMPETITION_SLUG_MAP.get(competition, competition)


def competition_name(competition: str) -> str:
    """Accept a slug or a name; return the sources.json competition name."""
    return _SLUG_TO_NAME.get(competition, competition)


def _as_date(value) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def resolve_season(competition: str, on_date=None) -> int:
    """Season key of the registry file that governs [competition] on [on_date].

    Reuses highlights_common.season_for_competition (no second season rule):
    leagues and UCL/UEL → August-threshold start year; tournaments → edition year.
    """
    return season_for_competition(competition_name(competition), now=_as_date(on_date))


def _entry_ok(entry: dict, path: Path) -> bool:
    """Light structural check so a hand-editing mistake skips one entry instead
    of breaking a run. Full validation lives in the JSON schema + tests."""
    if not isinstance(entry, dict) or any(k not in entry for k in _REQUIRED_KEYS):
        log.warning(f"rights: skipping malformed entry in {path.name}: {entry!r}")
        return False
    if entry["role"] not in _ROLES or entry["status"] not in _STATUSES:
        log.warning(f"rights: skipping entry with bad role/status in {path.name}: {entry!r}")
        return False
    if entry["country"] == "UK":
        log.warning(f"rights: 'UK' is not ISO 3166-1 (use 'GB') in {path.name}: {entry!r}")
        return False
    for key in ("valid_from", "valid_until"):
        if entry.get(key):
            try:
                date.fromisoformat(entry[key])
            except ValueError:
                log.warning(f"rights: bad {key} in {path.name}: {entry!r}")
                return False
    return True


def load_registry(season: int, rights_dir: "Path | None" = None) -> list[dict]:
    """All well-formed entries of rights/{season}.json; [] (with a warning) if the
    file is missing or unreadable."""
    path = Path(rights_dir or RIGHTS_DIR) / f"{season}.json"
    if not path.exists():
        log.warning(f"rights: no registry file for season {season} ({path}) — no entries")
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning(f"rights: could not read {path}: {exc} — no entries")
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        log.warning(f"rights: {path} has no 'entries' list — no entries")
        return []
    return [e for e in entries if _entry_ok(e, path)]


def is_date_active(entry: dict, on_date) -> bool:
    """True when [on_date] is inside the entry's optional [valid_from, valid_until]
    range (inclusive). An entry without dates is active for its whole season."""
    d = _as_date(on_date)
    start, end = entry.get("valid_from"), entry.get("valid_until")
    if start and d < date.fromisoformat(start):
        return False
    if end and d > date.fromisoformat(end):
        return False
    return True


def active_entries(
    competition: str,
    country: str,
    on_date=None,
    rights_dir: "Path | None" = None,
) -> list[dict]:
    """Registry entries for [competition] (name or slug) in [country] that apply
    on [on_date]: from the season file resolve_season() picks, inside any date
    range, and not ``rejected``. Includes to-confirm placeholders (holder None);
    callers decide how to treat them."""
    slug = competition_slug(competition)
    season = resolve_season(slug, on_date)
    cc = country.upper()
    return [
        e for e in load_registry(season, rights_dir)
        if e["competition"] == slug
        and e["country"] == cc
        and e["status"] != "rejected"
        and is_date_active(e, on_date)
    ]


def holders(entries: list[dict]) -> list[dict]:
    """Entries that name a holder (drops to-confirm placeholders)."""
    return [e for e in entries if e.get("holder")]


def entry_source_ids(entry: dict) -> set[str]:
    """Every YouTube source ID an entry points at: channel uploads playlists
    (UC→UU) and listed playlists."""
    ids: set[str] = set()
    for ch in entry.get("channels") or []:
        cid = ch.get("channel_id") or ""
        if cid.startswith("UC"):
            ids.add("UU" + cid[2:])
        ids.update(p for p in ch.get("playlist_ids") or [] if p)
    return ids
