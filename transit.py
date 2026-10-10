"""
Transit directions from open schedule data — our own planner.

The schedules come from 511 SF Bay's GTFS feeds (Muni, BART, Caltrain), built
into `transit_data/` by gtfs_build.py. Open data can be drawn on any map,
unlike Google's Routes API, whose terms forbid showing its results on or near
a non-Google map — which this Mapbox app is.

**What it plans today: direct rides.** Walk to a stop, ride one line, walk to
the destination. No transfers yet. That covers most trips inside San
Francisco; a trip that needs a change of line comes back as "no direct ride"
rather than a wrong answer.

How a search works:

1. Find every stop within a short walk of home and of the destination.
2. Find every *pattern* (a line's run through a fixed list of stops) that
   passes a stop near home and later a stop near the destination.
3. For each, find the first trip running today that leaves that stop after
   you can walk there — or, for "arrive by", the last trip that gets you in
   on time.
4. Keep the best option per line, earliest arrival first.

Walking times are estimated from straight-line distance for the search, then
replaced with real Mapbox walking routes for the options actually shown.

Schedules only: live delays are not included yet.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import sys
import threading
from array import array
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic

import requests
from dateutil import tz

_DATA_DIR = Path(__file__).parent / "transit_data"
_PACIFIC = tz.gettz("America/Los_Angeles")
_WALK_URL = "https://api.mapbox.com/directions/v5/mapbox/walking/{},{};{},{}"

MAX_WALK_M = 800          # straight-line reach to a stop, about a 12-minute walk
WALK_SPEED_MPS = 1.25     # an unhurried pace
DETOUR = 1.3              # streets are longer than a straight line
SEARCH_WINDOW_S = 3 * 3600  # don't offer a ride hours away
WORTH_RIDING_S = 3 * 60     # a ride must beat walking the whole way by this much

_CACHE_TTL_S = 120
_cache: dict = {}
_walk_cache: dict = {}
_lock = threading.Lock()
_data: dict | None = None

# What riders call each GTFS route_type.
_VEHICLE = {0: "Light rail", 1: "Train", 2: "Train", 3: "Bus", 4: "Ferry",
            5: "Cable car", 7: "Funicular", 11: "Trolleybus", 12: "Monorail"}
_HISTORIC_STREETCARS = {"E", "F"}   # Muni's heritage lines, type 0 like the Metro


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def available() -> bool:
    return (_DATA_DIR / "index.json.gz").exists() and (_DATA_DIR / "times.bin.gz").exists()


def _load() -> dict | None:
    """Read the index once, on first use rather than at startup, so the app
    still boots fast after a free-tier spin-down."""
    global _data
    if _data is not None or not available():
        return _data
    with _lock:
        if _data is not None:
            return _data
        with gzip.open(_DATA_DIR / "index.json.gz", "rt", encoding="utf-8") as fh:
            index = json.load(fh)
        times = array("i")
        with gzip.open(_DATA_DIR / "times.bin.gz", "rb") as fh:
            times.frombytes(fh.read())
        if sys.byteorder != "little":
            times.byteswap()

        at_stop: list[list[tuple[int, int]]] = [[] for _ in index["stops"]]
        for p, pat in enumerate(index["patterns"]):
            for pos, stop in enumerate(pat["stops"]):
                at_stop[stop].append((p, pos))

        # A coarse grid (~1 km cells) so "stops near here" checks a handful of
        # cells instead of every stop in the region.
        grid: dict[tuple[int, int], list[int]] = {}
        for s, (_, lat, lon) in enumerate(index["stops"]):
            grid.setdefault((int(lat * 100), int(lon * 100)), []).append(s)

        for svc in index["services"]:
            svc["add"], svc["remove"] = set(svc["add"]), set(svc["remove"])

        _data = {**index, "times": times, "at_stop": at_stop, "grid": grid, "active": {}}
        return _data


def data_until() -> str | None:
    """The earliest date any loaded feed stops covering, as YYYY-MM-DD."""
    if not available():
        return None
    data = _load()
    ends = [f["end"] for f in data["feeds"] if f.get("end")]
    if not ends:
        return None
    end = min(ends)
    return f"{end[:4]}-{end[4:6]}-{end[6:]}"


def _active_services(data: dict, day: date) -> set[int]:
    cached = data["active"].get(day)
    if cached is not None:
        return cached
    stamp, weekday = day.strftime("%Y%m%d"), day.weekday()
    active = set()
    for i, svc in enumerate(data["services"]):
        if stamp in svc["remove"]:
            continue
        if stamp in svc["add"] or (svc["days"][weekday] == "1" and svc["start"] <= stamp <= svc["end"]):
            active.add(i)
    data["active"][day] = active
    return active


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110_540
    return math.hypot(dx, dy)


def _stops_near(data: dict, lat: float, lon: float) -> dict[int, float]:
    """Stops within walking reach, mapped to the estimated walk in seconds."""
    out = {}
    cy, cx = int(lat * 100), int(lon * 100)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for s in data["grid"].get((cy + dy, cx + dx), ()):
                _, slat, slon = data["stops"][s]
                d = _meters(lat, lon, slat, slon)
                if d <= MAX_WALK_M:
                    out[s] = d * DETOUR / WALK_SPEED_MPS
    return out


def _ride_coords(data: dict, pat: dict, i: int, j: int) -> list[list[float]]:
    """The stretch of the line between boarding and alighting, cut from its shape."""
    stops = data["stops"]
    ends = [[stops[pat["stops"][i]][2], stops[pat["stops"][i]][1]],
            [stops[pat["stops"][j]][2], stops[pat["stops"][j]][1]]]
    if pat.get("shape") is None or not pat.get("vertices"):
        return [[stops[s][2], stops[s][1]] for s in pat["stops"][i:j + 1]]
    flat = data["shapes"][pat["shape"]]
    a, b = pat["vertices"][i], pat["vertices"][j]
    middle = [[flat[k] / 1e5, flat[k + 1] / 1e5] for k in range(2 * a, 2 * b + 2, 2)] if b >= a else []
    return [ends[0]] + middle + [ends[1]]


def _walk(frm: tuple[float, float], to: tuple[float, float], estimate_s: float) -> dict:
    """A real walking route from Mapbox, or a straight line with the estimate."""
    key = (round(frm[0], 5), round(frm[1], 5), round(to[0], 5), round(to[1], 5))
    if key in _walk_cache:
        return _walk_cache[key]
    straight = {"duration_s": round(estimate_s), "distance_m": round(_meters(frm[0], frm[1], to[0], to[1]) * DETOUR),
                "coords": [[frm[1], frm[0]], [to[1], to[0]]]}
    token = os.environ.get("MAPBOX_TOKEN")
    if not token:
        return straight
    try:
        resp = requests.get(_WALK_URL.format(frm[1], frm[0], to[1], to[0]),
                            params={"access_token": token, "geometries": "geojson", "overview": "full"},
                            timeout=10)
        resp.raise_for_status()
        best = (resp.json().get("routes") or [None])[0]
    except Exception:
        best = None
    if not best:
        return straight
    leg = {"duration_s": round(best["duration"]), "distance_m": round(best["distance"]),
           "coords": best["geometry"]["coordinates"]}
    _walk_cache[key] = leg
    return leg


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _candidates(data: dict, near_o: dict, near_d: dict):
    """(pattern, board position, walk there, alight position, walk after) for
    every pattern that passes a stop near home and later one near the end."""
    boards: dict[int, list[tuple[int, float]]] = {}
    for s, walk in near_o.items():
        for p, pos in data["at_stop"][s]:
            boards.setdefault(p, []).append((pos, walk))
    for p, options in boards.items():
        pat = data["patterns"][p]
        alights = [(pos, near_d[s]) for pos, s in enumerate(pat["stops"]) if s in near_d]
        for i, walk_o in options:
            for j, walk_d in alights:
                if j > i:
                    yield p, i, walk_o, j, walk_d


def _search(data: dict, day: date, target_s: int, arrive_by: bool,
            near_o: dict, near_d: dict) -> list[dict]:
    """Best trip per pattern. `target_s` is seconds after `day`'s midnight:
    the earliest you can leave, or (arrive_by) the latest you can arrive."""
    times, best = data["times"], {}
    # Today's trips, plus yesterday's that run past midnight (GTFS writes a
    # 12:40 AM trip on yesterday's schedule as 24:40:00).
    days = [(day, 0), (day - timedelta(days=1), 86_400)]
    for p, i, walk_o, j, walk_d in _candidates(data, near_o, near_d):
        pat = data["patterns"][p]
        n = len(pat["stops"])
        for service_day, shift in days:
            active = _active_services(data, service_day)
            for svc, off in zip(pat["svc"], pat["off"]):
                if svc not in active:
                    continue
                dep, arr = times[off + i] - shift, times[off + j] - shift
                leave, arrive = dep - walk_o, arr + walk_d
                if arrive_by:
                    if arrive > target_s or leave < target_s - SEARCH_WINDOW_S:
                        continue
                    better = p not in best or leave > best[p]["leave"]
                else:
                    if leave < target_s or leave > target_s + SEARCH_WINDOW_S:
                        continue
                    better = p not in best or arrive < best[p]["arrive"] or (
                        arrive == best[p]["arrive"] and leave > best[p]["leave"])
                if better:
                    best[p] = {"pattern": p, "i": i, "j": j, "dep": dep, "arr": arr,
                               "walk_o": walk_o, "walk_d": walk_d,
                               "leave": leave, "arrive": arrive, "n": n}
    return list(best.values())


def _line_names(route: dict) -> tuple[str, str]:
    """(badge text, full name). BART and Caltrain have no rider-facing short
    name, so their badge falls back to the agency in the page."""
    short, long_name, agency = route["short"], route["long"], route["agency"]
    if agency == "BART":
        color = short.split("-")[0]
        return "", (f"{color} line" if color and color[0].isupper() and "Bridge" not in short else long_name)
    if agency == "Caltrain":
        return "", short
    return short, long_name


def _at(day: date, seconds: int) -> datetime:
    return datetime.combine(day, time.min).replace(tzinfo=_PACIFIC) + timedelta(seconds=seconds)


def _clock(moment: datetime | None) -> str | None:
    return moment.strftime("%I:%M %p").lstrip("0") if moment else None


def _build(data: dict, day: date, hit: dict, origin, destination) -> dict:
    pat = data["patterns"][hit["pattern"]]
    route = data["routes"][pat["route"]]
    stops = data["stops"]
    board, alight = stops[pat["stops"][hit["i"]]], stops[pat["stops"][hit["j"]]]

    with ThreadPoolExecutor(max_workers=2) as pool:
        w1 = pool.submit(_walk, origin, (board[1], board[2]), hit["walk_o"])
        w2 = pool.submit(_walk, (alight[1], alight[2]), destination, hit["walk_d"])
        walk_in, walk_out = w1.result(), w2.result()

    short, full = _line_names(route)
    coords = _ride_coords(data, pat, hit["i"], hit["j"])
    depart, arrive = _at(day, hit["dep"]), _at(day, hit["arr"])
    leave = depart - timedelta(seconds=walk_in["duration_s"])
    done = arrive + timedelta(seconds=walk_out["duration_s"])
    ride = {
        "kind": "transit", "line": short, "line_name": full,
        "color": route["color"], "text_color": route["text_color"],
        "vehicle": "Streetcar" if short in _HISTORIC_STREETCARS and route["agency"] == "Muni"
                   else _VEHICLE.get(route["type"], "Transit"),
        # Muni's feed writes apostrophes as backticks ("Fisherman`s Wharf").
        "agency": route["agency"], "headsign": (pat["headsign"] or "").replace("`", "'") or None,
        "from_stop": board[0], "to_stop": alight[0], "stops": hit["j"] - hit["i"],
        "depart": depart.isoformat(), "arrive": arrive.isoformat(),
        "depart_text": _clock(depart), "arrive_text": _clock(arrive),
        "duration_s": hit["arr"] - hit["dep"],
        "distance_m": round(sum(_meters(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))),
        "coords": coords,
    }
    segments = [
        {"kind": "walk", **walk_in, "to": board[0]},
        ride,
        {"kind": "walk", **walk_out, "to": None},
    ]
    return {
        "duration_s": round((done - leave).total_seconds()),
        "distance_m": sum(s["distance_m"] for s in segments),
        "leave_at": leave.isoformat(), "leave_text": _clock(leave),
        "arrive_at": done.isoformat(), "arrive_text": _clock(done),
        "lines": [short or route["agency"]],
        "segments": segments,
    }


def plan(origin: tuple[float, float], destination: tuple[float, float],
         arrive_by: str | None = None, alternatives: bool = True) -> dict:
    """Direct-ride transit options between two (lat, lon) points.

    `arrive_by` is a naive Pacific time ("2026-10-20T08:30"); without it the
    trip leaves now. Always returns `routes` (possibly empty) and, when empty,
    an `error` that says why — so "no data", "too far from a stop" and "needs
    a transfer" are distinguishable instead of all looking like nothing.
    """
    data = _load()
    if data is None:
        return {"routes": [], "error": "transit schedules not built (run gtfs_build.py)"}

    if arrive_by:
        try:
            when = datetime.fromisoformat(arrive_by)
        except ValueError:
            return {"routes": [], "error": "arrive_by must be an ISO time"}
    else:
        when = datetime.now(_PACIFIC).replace(tzinfo=None)
    day, target = when.date(), when.hour * 3600 + when.minute * 60 + when.second

    until = data_until()
    if until and day.isoformat() > until:
        return {"routes": [], "error": f"transit schedules expired on {until}; rebuild with gtfs_build.py"}

    cache_key = (round(origin[0], 4), round(origin[1], 4), round(destination[0], 4),
                 round(destination[1], 4), arrive_by or when.strftime("%Y-%m-%dT%H:%M"), alternatives)
    hit = _cache.get(cache_key)
    if hit and monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]

    near_o, near_d = _stops_near(data, *origin), _stops_near(data, *destination)
    if not near_o:
        result = {"routes": [], "error": "no Muni, BART or Caltrain stop within a short walk of home"}
    elif not near_d:
        result = {"routes": [], "error": "no Muni, BART or Caltrain stop within a short walk of the destination"}
    else:
        # A ride that barely beats walking (one stop down the street) isn't
        # worth offering; if nothing clears that bar, say walking wins.
        walk_all = _meters(*origin, *destination) * DETOUR / WALK_SPEED_MPS
        hits = [h for h in _search(data, day, target, bool(arrive_by), near_o, near_d)
                if h["arrive"] - h["leave"] <= walk_all - WORTH_RIDING_S]
        # Arriving by a deadline, the best option is the one you can leave
        # latest for; leaving now, it's the one that gets you there first.
        rank = ((lambda h: (-h["leave"], h["arrive"])) if arrive_by
                else (lambda h: (h["arrive"], -h["leave"])))
        # One option per line: the N in two nearby patterns is still the N.
        per_line: dict[int, dict] = {}
        for h in sorted(hits, key=rank):
            per_line.setdefault(data["patterns"][h["pattern"]]["route"], h)
        ranked = sorted(per_line.values(), key=rank)[: 3 if alternatives else 1]
        if not ranked and walk_all <= 45 * 60:
            result = {"routes": [], "error": f"walking is about as fast (around {round(walk_all / 60)} min)"}
        elif not ranked:
            result = {"routes": [], "error": "no direct ride found; trips that need a transfer aren't supported yet"}
        else:
            result = {"routes": [_build(data, day, h, origin, destination) for h in ranked]}

    _cache[cache_key] = (monotonic(), result)
    return result
