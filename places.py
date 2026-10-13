"""
Your places: home, and the destinations you've saved.

Kept on the server, behind the schedule passcode, for two reasons. They follow
you between devices — the phone app will need them — where browser storage
stayed on one laptop. And home is the most personal thing in the app: the map
is a public demo, so the demo starting point (`mock_data.HOME`, a generic spot
in SoMa) is what everyone else sees, and your real home is only ever sent to a
browser holding the passcode. The overnight alert job reads it here, so
scheduled trips are planned from where you actually live.

Stored as one small JSON document in the settings table: no new schema.
"""

from __future__ import annotations

import json

import mock_data
import store

PLACES_KEY = "places"
MAX_SAVED = 50


def _place(raw) -> dict | None:
    """A validated {name, lat, lon}, or None. Bay Area-ish bounds keep a typo
    from putting home in the Atlantic."""
    try:
        name = str(raw.get("name") or "").strip()[:120]
        lat, lon = float(raw["lat"]), float(raw["lon"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if not name or not (36.5 <= lat <= 39.0 and -123.8 <= lon <= -121.0):
        return None
    return {"name": name, "lat": round(lat, 6), "lon": round(lon, 6)}


def get_places(user_id: str = store.LOCAL_USER) -> dict:
    try:
        data = json.loads(store.get_setting(PLACES_KEY, user_id) or "{}")
    except ValueError:
        data = {}
    saved = [p for p in (_place(x) for x in data.get("saved") or []) if p]
    return {"home": _place(data.get("home") or {}), "saved": saved[:MAX_SAVED]}


def save_places(data: dict, user_id: str = store.LOCAL_USER) -> dict:
    """Replace the whole document. Invalid entries are dropped, not stored;
    duplicates (same name) keep the first."""
    seen, saved = set(), []
    for p in (_place(x) for x in (data.get("saved") or [])):
        if p and p["name"] not in seen:
            seen.add(p["name"])
            saved.append(p)
    clean = {"home": _place(data.get("home") or {}), "saved": saved[:MAX_SAVED]}
    store.set_setting(PLACES_KEY, json.dumps(clean), user_id)
    return clean


def home(user_id: str = store.LOCAL_USER) -> dict:
    """Where trips start, as {label, lat, lon}: your home if you've set one,
    else the public demo starting point."""
    mine = get_places(user_id)["home"]
    if mine:
        return {"label": mine["name"], "lat": mine["lat"], "lon": mine["lon"]}
    return dict(mock_data.HOME)
