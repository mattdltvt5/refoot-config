#!/usr/bin/env python3
"""
discover_playlists_readonly.py — ONE-OFF, READ-ONLY diagnostic.

Lists the REAL playlists on the YouTube channels mapped in sources.json so the
auto-discovery matching rule can be designed from actual playlist names rather
than inference. Everything is printed to stdout (the GitHub Actions log).

Guarantees:
  • WRITES NOTHING — no repo files, no playlist-owners.json, no quota-tracker.json.
    (fetch_playlist_owner is called with quota=None so it never touches the tracker;
    unit usage is counted in a local integer and only PRINTED.)
  • playlists.list ONLY. NEVER search.list, never any mutating call.
  • Small quota: resolves each mapped playlist's owner once (cached), lists each
    unique channel once (cached, ≤5 pages). A few dozen units total; printed at end.

Auth: YOUTUBE_API_KEY from os.environ — identical to verify_playlist_owners.py.
Reuses highlights_common.fetch_playlist_owner() for owner resolution and the
YT_PLAYLISTS endpoint constant for channel listing.
"""

import json
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlights_common import YT_PLAYLISTS, SOURCES_JSON, fetch_playlist_owner

# Broadcaster labels flagged in the audit as hosting MULTIPLE competitions —
# their channels must distinguish competitions in the real playlist titles.
_MULTI_COMP_LABELS = {"CBS Sport Golazo", "TUDN USA", "Fox Soccer", "FOX sports"}

_MAX_PAGES = 5              # per-channel pagination cap (keeps quota bounded)
_TEAM_SAMPLE = 4           # sample only a few Tier-1c/1d team playlists


def _units():
    """Mutable unit counter (list cell so nested helpers can increment)."""
    return [0]


def list_channel_playlists(session, channel_id, api_key, counter, max_pages=_MAX_PAGES):
    """Read-only: list a channel's playlists via playlists.list?part=snippet,contentDetails.

    Returns (channel_title, [ {title, id, itemCount, publishedAt}, ... ]).
    Paginates up to max_pages (1 unit/page). Never writes anything.
    """
    out = []
    channel_title = ""
    token = ""
    for _ in range(max_pages):
        params = {
            "part":       "snippet,contentDetails",
            "channelId":  channel_id,
            "maxResults": 50,
            "key":        api_key,
        }
        if token:
            params["pageToken"] = token
        resp = session.get(YT_PLAYLISTS, params=params, timeout=30)
        counter[0] += 1
        if resp.status_code != 200:
            print(f"   [!] playlists.list HTTP {resp.status_code} for channel {channel_id}")
            break
        data = resp.json()
        for it in data.get("items", []):
            sn = it.get("snippet", {})
            channel_title = channel_title or sn.get("channelTitle", "")
            out.append({
                "title":       sn.get("title", ""),
                "id":          it.get("id", ""),
                "itemCount":   it.get("contentDetails", {}).get("itemCount"),
                "publishedAt": sn.get("publishedAt", ""),
            })
        token = data.get("nextPageToken", "")
        if not token:
            break
    return channel_title, out


def _safe(s):
    """ASCII-safe for CI logs regardless of runner encoding."""
    return (s or "").encode("ascii", "replace").decode("ascii")


def _print_playlists(playlists, highlight_terms=()):
    if not playlists:
        print("   (no playlists returned)")
        return
    for p in playlists:
        flag = ""
        low = (p["title"] or "").lower()
        if highlight_terms and any(t in low for t in highlight_terms):
            flag = "   <-- MATCHES"
        cnt = p["itemCount"] if p["itemCount"] is not None else "?"
        print(f"   - {_safe(p['title'])[:70]:70}  {p['id']}  items={cnt}  created={p['publishedAt'][:10]}{flag}")


def main():
    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        print("YOUTUBE_API_KEY is not set — cannot run read-only discovery.")
        sys.exit(1)

    raw = json.load(open(SOURCES_JSON, encoding="utf-8"))
    competitions  = raw.get("competitions", {})     # name -> channelId (Tier 2)
    playlists_cfg = raw.get("playlists", {})         # comp -> {broadcaster -> [PL...]} (Tier 4)
    team_pls      = raw.get("teamPlaylists", {})     # comp -> {team -> PL} (Tier 1c/1d)

    session = requests.Session()
    counter = _units()

    owner_cache: dict = {}    # playlist_id -> owner dict | None
    channel_cache: dict = {}  # channel_id  -> (channel_title, [playlists])

    def resolve_owner(pid):
        if pid in owner_cache:
            return owner_cache[pid]
        try:
            # quota=None → does NOT write quota-tracker.json; count locally instead.
            owner = fetch_playlist_owner(pid, api_key, session=session, quota=None)
            counter[0] += 1
        except Exception as exc:  # HTTP 4xx/5xx, network, malformed
            print(f"   [!] could not resolve playlist {pid}: {type(exc).__name__}: {exc}")
            owner = None
        owner_cache[pid] = owner
        return owner

    def channel_listing(channel_id):
        if channel_id in channel_cache:
            return channel_cache[channel_id]
        result = list_channel_playlists(session, channel_id, api_key, counter)
        channel_cache[channel_id] = result
        return result

    print("=" * 78)
    print("READ-ONLY playlist discovery — sources.json mapped channels")
    print("playlists.list only · no search.list · writes nothing")
    print("=" * 78)

    # ── TIER 2 competition channels (known channelIds; the working reference) ──
    print("\n== TIER 2 COMPETITION CHANNELS ==")
    for comp, chid in competitions.items():
        if not chid:
            print(f"\n[{comp}] Tier-2 channel: (none mapped)")
            continue
        title, pls = channel_listing(chid)
        print(f"\n[{comp}] Tier-2 channel {chid}  owner={_safe(title)!r}  ({len(pls)} playlists)")
        _print_playlists(pls)

    # ── TIER 4 broadcaster playlists: resolve owner, then list owning channel ──
    print("\n\n== TIER 4 BROADCASTER PLAYLISTS ==")
    # (comp, label, pid)
    tier4 = []
    for comp, bmap in playlists_cfg.items():
        if not isinstance(bmap, dict):
            continue
        for label, ids in bmap.items():
            for pid in (ids if isinstance(ids, list) else [ids]):
                tier4.append((comp, label, pid))

    for comp, label, pid in tier4:
        print(f"\n[{comp} / {label}] mapped playlist {pid}")
        owner = resolve_owner(pid)
        if not owner:
            print("   -> UNRESOLVABLE (private / deleted / invalid ID)")
            continue
        print(f"   -> owner channel: {_safe(owner['channel_title'])!r}  ({owner['channel_id']})")
        print(f"   -> mapped playlist title: {_safe(owner['playlist_title'])!r}")
        title, pls = channel_listing(owner["channel_id"])
        # show the mapped playlist's live item count from the channel listing
        row = next((p for p in pls if p["id"] == pid), None)
        if row:
            print(f"   -> mapped playlist items now: {row['itemCount']}")
        else:
            print("   -> mapped playlist NOT found in owner channel's current playlists "
                  "(possible cross-channel move / stale)")

    # ── EUROPA LEAGUE focused check ──
    print("\n\n== EUROPA LEAGUE ==")
    uel = playlists_cfg.get("Europa League", {})
    for label, ids in (uel.items() if isinstance(uel, dict) else []):
        for pid in (ids if isinstance(ids, list) else [ids]):
            print(f"\n[Europa League / {label}] mapped playlist {pid}")
            owner = resolve_owner(pid)
            if not owner:
                print("   -> UNRESOLVABLE — likely stale/deleted")
                continue
            title, pls = channel_listing(owner["channel_id"])
            row = next((p for p in pls if p["id"] == pid), None)
            print(f"   -> owner {_safe(owner['channel_title'])!r}; mapped title "
                  f"{_safe(owner['playlist_title'])!r}; items="
                  f"{row['itemCount'] if row else '?'}")
            print(f"   -> ALL playlists on this channel mentioning europa/UEL "
                  f"(is there a newer season one the mapping misses?):")
            _print_playlists(
                [p for p in pls if any(t in p["title"].lower()
                                       for t in ("europa", "uel", "uefa europa"))],
                highlight_terms=("2025", "25/26", "2025/26", "25-26"),
            )

    # ── PREMIER LEAGUE focused check ──
    print("\n\n== PREMIER LEAGUE ==")
    pl_bmap = playlists_cfg.get("Premier League", {})
    pl_pids = [pid for ids in (pl_bmap.values() if isinstance(pl_bmap, dict) else [])
               for pid in (ids if isinstance(ids, list) else [ids])]
    if not pl_pids:
        print("   (no PL broadcaster playlist mapped)")
    for pid in pl_pids:
        print(f"\n[Premier League] broadcaster playlist {pid}")
        owner = resolve_owner(pid)
        if not owner:
            print("   -> UNRESOLVABLE")
            continue
        print(f"   -> owning channel (candidate PL Tier-2 seed): "
              f"{_safe(owner['channel_title'])!r}  ({owner['channel_id']})")
        title, pls = channel_listing(owner["channel_id"])
        print(f"   -> channel has {len(pls)} playlists; ones matching the PL "
              f"'club highlights' pattern:")
        _print_playlists(
            [p for p in pls if "club highlights" in p["title"].lower()
                            or "highlights" in p["title"].lower()],
            highlight_terms=("club highlights",),
        )

    # ── MULTI-COMP BROADCASTERS: how do real titles distinguish competitions? ──
    print("\n\n== MULTI-COMP BROADCASTERS ==")
    seen_channels = set()
    for comp, label, pid in tier4:
        if label not in _MULTI_COMP_LABELS:
            continue
        owner = resolve_owner(pid)
        if not owner or owner["channel_id"] in seen_channels:
            continue
        seen_channels.add(owner["channel_id"])
        title, pls = channel_listing(owner["channel_id"])
        print(f"\n[{label}] channel {owner['channel_id']}  owner={_safe(title)!r}  "
              f"({len(pls)} playlists) — full listing so we can see comp naming:")
        _print_playlists(pls)

    # ── TIER 1c/1d team playlists — small sample (naming on club channels) ──
    print("\n\n== TEAM PLAYLISTS (SAMPLE) ==")
    sampled = 0
    for comp, tmap in (team_pls.items() if isinstance(team_pls, dict) else []):
        if sampled >= _TEAM_SAMPLE:
            break
        if not isinstance(tmap, dict):
            continue
        for team, pid in tmap.items():
            if sampled >= _TEAM_SAMPLE or not pid:
                continue
            print(f"\n[{comp} / {team}] mapped playlist {pid}")
            owner = resolve_owner(pid)
            if not owner:
                print("   -> UNRESOLVABLE")
            else:
                title, pls = channel_listing(owner["channel_id"])
                row = next((p for p in pls if p["id"] == pid), None)
                print(f"   -> owner {_safe(owner['channel_title'])!r}; mapped title "
                      f"{_safe(owner['playlist_title'])!r}; items="
                      f"{row['itemCount'] if row else '?'}")
            sampled += 1

    # ── WORLD CUP Telemundo config-bug confirmation (do NOT fix) ──
    print("\n\n== WORLD CUP TELEMUNDO CHECK ==")
    tel_id = "PLXHZm5xDlEdQ"
    print(f"[World Cup / Telemundo] mapped id {tel_id!r} (len={len(tel_id)}; "
          f"valid PL IDs are ~34 chars)")
    owner = resolve_owner(tel_id)
    if owner:
        print(f"   -> resolved (unexpected): {_safe(owner['playlist_title'])!r} "
              f"on {_safe(owner['channel_title'])!r}")
    else:
        print("   -> UNRESOLVABLE / empty — confirms the truncated/invalid config entry "
              "(load_sources already skips it). Not fixing here.")

    print("\n" + "=" * 78)
    print(f"Estimated YouTube quota used: ~{counter[0]} units "
          f"(playlists.list @ 1 unit/call). No writes performed.")
    print("=" * 78)


if __name__ == "__main__":
    main()
