"""
Transit directions from open schedule data — our own planner.

The schedules come from 511 SF Bay's GTFS feeds — every Bay Area agency 511
publishes, one file pair each — built into `transit_data/` by gtfs_build.py
and merged here when first needed. Open data can be drawn on any map,
unlike Google's Routes API, whose terms forbid showing its results on or near
a non-Google map — which this Mapbox app is.

Two searches run side by side:

**Direct rides** (`_direct`) — walk to a stop, ride one line, walk to the
destination. It looks at every line that passes a stop near home and later a
stop near the destination, so it can offer one option per line.

**Transfers** (`_raptor`) — RAPTOR, the round-based algorithm transit
planners use. Round 1 finds the earliest you can reach every stop with one
ride; round 2 boards again from everything round 1 reached, and so on, up to
`MAX_RIDES`. Between rounds you may walk to a nearby stop (BART Powell to the
Muni Metro platform) and you need `CHANGE_S` to make a connection. Each round
keeps a trip only if it beats every earlier round, so it returns at most one
journey per number of rides — e.g. a direct ride and a faster one with a
transfer.

"Arrive by" runs the same search backwards: from the destination, the latest
you can leave each stop and still make it. Rather than a second copy of the
algorithm, the backward search runs the forward one on mirrored data — stop
order reversed and times negated — so "latest departure" becomes "earliest
arrival".

Walking is estimated from straight-line distance during the search, then
replaced with real Mapbox walking routes for the journeys actually shown.

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
TRANSFER_M = 300          # walking between stops to change lines
WALK_SPEED_MPS = 1.25     # an unhurried pace
DETOUR = 1.3              # streets are longer than a straight line
CHANGE_S = 2 * 60         # time to make a connection
MAX_RIDES = 3             # at most two transfers
SEARCH_WINDOW_S = 3 * 3600  # don't offer a ride hours away
WORTH_RIDING_S = 3 * 60     # a ride must beat walking the whole way by this much
TRANSFER_WORTH_S = 5 * 60   # each extra transfer must save this much

_CACHE_TTL_S = 120
_cache: dict = {}
_walk_cache: dict = {}
_lock = threading.Lock()
_data: dict | None = None
_INF = float("inf")

# What riders call each GTFS route_type.
_VEHICLE = {0: "Light rail", 1: "Train", 2: "Train", 3: "Bus", 4: "Ferry",
            5: "Cable car", 7: "Funicular", 11: "Trolleybus", 12: "Monorail"}
_HISTORIC_STREETCARS = {"E", "F"}   # Muni's heritage lines, type 0 like the Metro


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

# The agencies whose expiry matters for "is transit working" — the rest run
# out on their own dates without taking anything else down.
MAJOR = ("SF", "BA", "CT", "AC", "SM", "SC", "GG")


def available() -> bool:
    return (_DATA_DIR / "feeds.json").exists()


def _load() -> dict | None:
    """Merge every agency's files once, on first use rather than at startup,
    so the app still boots fast after a free-tier spin-down.

    Each agency's ids are local to it; merging shifts them past the agencies
    already loaded. Numbers go into `array`s rather than Python lists — a
    list spends about 36 bytes per small integer, an array 4 — which is the
    difference between fitting comfortably in a 512 MB server and not.
    """
    global _data
    if _data is not None or not available():
        return _data
    with _lock:
        if _data is not None:
            return _data
        manifest = json.loads((_DATA_DIR / "feeds.json").read_text())
        stops, routes, services, shapes, patterns, feeds = [], [], [], [], [], []
        times = array("i")
        for op in sorted(manifest):
            try:
                with gzip.open(_DATA_DIR / f"{op}.json.gz", "rt", encoding="utf-8") as fh:
                    part = json.load(fh)
                chunk = array("i")
                with gzip.open(_DATA_DIR / f"{op}.bin.gz", "rb") as fh:
                    chunk.frombytes(fh.read())
            except OSError:
                continue                              # listed but missing: plan without it
            if sys.byteorder != "little":
                chunk.byteswap()
            s0, r0, v0, h0, t0 = len(stops), len(routes), len(services), len(shapes), len(times)
            stops += part["stops"]
            routes += part["routes"]
            services += part["services"]
            shapes += [array("i", sh) for sh in part["shapes"]]
            times += chunk
            for pat in part["patterns"]:
                patterns.append({
                    "route": pat["route"] + r0, "headsign": pat["headsign"],
                    "stops": array("i", (x + s0 for x in pat["stops"])),
                    "shape": None if pat["shape"] is None else pat["shape"] + h0,
                    "vertices": None if pat["vertices"] is None else array("i", pat["vertices"]),
                    "svc": array("i", (x + v0 for x in pat["svc"])),
                    "off": array("i", (x + t0 for x in pat["off"])),
                })
            feeds.append({"operator": op, **manifest[op]})
            del part, chunk

        at_stop: list[list[tuple[int, int]]] = [[] for _ in stops]
        for p, pat in enumerate(patterns):
            for pos, stop in enumerate(pat["stops"]):
                at_stop[stop].append((p, pos))

        # A coarse grid (~1 km cells) so "stops near here" checks a handful of
        # cells instead of every stop in the region.
        grid: dict[tuple[int, int], list[int]] = {}
        for s, (_, lat, lon) in enumerate(stops):
            grid.setdefault((int(lat * 100), int(lon * 100)), []).append(s)

        for svc in services:
            svc["add"], svc["remove"] = set(svc["add"]), set(svc["remove"])

        _data = {"stops": stops, "routes": routes, "services": services, "shapes": shapes,
                 "patterns": patterns, "feeds": feeds, "times": times, "at_stop": at_stop,
                 "grid": grid, "active": {}, "day_trips": {}, "walks": {}}
        return _data


def _walks_from(data: dict, s: int) -> list[tuple[int, float]]:
    """Walking connections from stop `s` to nearby stops, for changing lines —
    worked out the first time a search needs them rather than for all twenty
    thousand stops up front."""
    walks = data["walks"].get(s)
    if walks is None:
        _, lat, lon = data["stops"][s]
        walks = data["walks"][s] = [(n, w) for n, w in _stops_near(data, lat, lon, TRANSFER_M).items()
                                    if n != s]
    return walks


def agencies() -> int:
    data = _load() if available() else None
    return len(data["feeds"]) if data else 0


def data_until() -> str | None:
    """The earliest date a major agency's schedules stop covering, as
    YYYY-MM-DD. Smaller agencies expire on their own dates: their services
    simply stop being active, and the rest keeps planning."""
    if not available():
        return None
    data = _load()
    ends = [f["end"] for f in data["feeds"] if f.get("end") and f["operator"] in MAJOR]
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


def _day_trips(data: dict, p: int, day: date) -> list[tuple[int, int]]:
    """The trips of pattern `p` running on `day`, as (offset into times,
    shift), earliest first. Includes yesterday's trips that run past midnight
    (GTFS writes 12:40 AM on yesterday's schedule as 24:40:00), shifted by a
    day. Within one day's trips no trip overtakes another on any line —
    checked against the data — which is what makes binary search valid."""
    by_day = data["day_trips"]
    if day not in by_day:
        if len(by_day) > 3:
            by_day.clear()
        by_day[day] = {}
    cache = by_day[day]
    if p in cache:
        return cache[p]
    times, pat = data["times"], data["patterns"][p]
    last = len(pat["stops"]) - 1
    found = []
    for service_day, shift in ((day, 0), (day - timedelta(days=1), 86_400)):
        active = _active_services(data, service_day)
        for svc, off in zip(pat["svc"], pat["off"]):
            if svc in active and times[off + last] >= shift:
                found.append((times[off] - shift, off, shift))
    found.sort()
    cache[p] = [(off, shift) for _, off, shift in found]
    return cache[p]


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110_540
    return math.hypot(dx, dy)


def _stops_near(data: dict, lat: float, lon: float, reach_m: float = MAX_WALK_M) -> dict[int, float]:
    """Stops within `reach_m`, mapped to the estimated walk in seconds."""
    out = {}
    cy, cx = int(lat * 100), int(lon * 100)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for s in data["grid"].get((cy + dy, cx + dx), ()):
                _, slat, slon = data["stops"][s]
                d = _meters(lat, lon, slat, slon)
                if d <= reach_m:
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
    if not token or straight["distance_m"] < 30:
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
# Search: direct rides
# ---------------------------------------------------------------------------
#
# A journey, whichever search found it, is a list of legs:
#   ("walk", from_stop or None, to_stop or None, estimate_s)   None = origin/destination
#   ("ride", pattern, offset, shift, board_pos, alight_pos)

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


def _direct(data: dict, day: date, target_s: int, arrive_by: bool,
            near_o: dict, near_d: dict) -> list[dict]:
    """The best direct ride per pattern. `target_s` is seconds after `day`'s
    midnight: the earliest you can leave, or (arrive_by) the latest you can
    arrive."""
    times, best = data["times"], {}
    for p, i, walk_o, j, walk_d in _candidates(data, near_o, near_d):
        pat = data["patterns"][p]
        for off, shift in _day_trips(data, p, day):
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
                best[p] = {
                    "leave": leave, "arrive": arrive, "rides": 1,
                    "lines": (pat["route"],),
                    "legs": [("walk", None, pat["stops"][i], walk_o),
                             ("ride", p, off, shift, i, j),
                             ("walk", pat["stops"][j], None, walk_d)],
                }
    return list(best.values())


# ---------------------------------------------------------------------------
# Search: transfers (RAPTOR)
# ---------------------------------------------------------------------------

class _View:
    """One pattern's trips as the search sees them. Forward, it is the real
    timetable. Backward, the stop order is reversed and every time negated,
    so the latest departure becomes the earliest "arrival" and the same
    forward code answers an arrive-by question."""

    __slots__ = ("stops", "trips", "times", "n", "forward")

    def __init__(self, data: dict, p: int, day: date, forward: bool):
        pat = data["patterns"][p]
        trips = _day_trips(data, p, day)
        self.stops = pat["stops"] if forward else pat["stops"][::-1]
        self.trips = trips if forward else trips[::-1]
        self.times, self.n, self.forward = data["times"], len(pat["stops"]), forward

    def at(self, t: int, i: int) -> int:
        off, shift = self.trips[t]
        if self.forward:
            return self.times[off + i] - shift
        return -(self.times[off + self.n - 1 - i] - shift)

    def first_from(self, i: int, ready: float) -> int:
        """Index of the first trip leaving position `i` at or after `ready`."""
        lo, hi = 0, len(self.trips)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.at(mid, i) < ready:
                lo = mid + 1
            else:
                hi = mid
        return lo


def _raptor(data: dict, day: date, starts: dict[int, float], ends: dict[int, float],
            forward: bool) -> list[tuple[float, list]]:
    """Round-based search. `starts` maps stops to the (search-direction) time
    you can be there; `ends` maps stops to the walk from there to the goal.
    Returns (total, legs-in-search-order) for each round that improved on
    every round before it."""
    best: dict[int, tuple[float, int]] = {s: (t, 0) for s, t in starts.items()}
    rounds: list[dict] = [{s: ("start",) for s in starts}]
    marked, results, best_total = set(starts), [], _INF
    views: dict[int, _View] = {}

    for k in range(1, MAX_RIDES + 1):
        snap = dict(best)
        queue: dict[int, int] = {}
        for s in marked:
            for p, pos in data["at_stop"][s]:
                n = len(data["patterns"][p]["stops"])
                pos = pos if forward else n - 1 - pos
                if pos < queue.get(p, 1 << 30):
                    queue[p] = pos
        slack = 0 if k == 1 else CHANGE_S
        parents, improved = {}, {}

        for p, start in queue.items():
            view = views.get(p) or views.setdefault(p, _View(data, p, day, forward))
            if not view.trips:
                continue
            trip, board = None, None
            for i in range(start, len(view.stops)):
                s = view.stops[i]
                if trip is not None:
                    arr = view.at(trip, i)
                    if arr < best.get(s, (_INF,))[0] and arr < best_total:
                        best[s] = (arr, k)
                        improved[s] = arr
                        parents[s] = ("ride", p, trip, board[0], i, board[1])
                label = snap.get(s)
                if label is not None and label[1] == k - 1:
                    ready = label[0] + slack
                    if trip is None or ready <= view.at(trip, i):
                        t = view.first_from(i, ready)
                        if t < len(view.trips) and (trip is None or t < trip):
                            if abs(view.at(t, i) - ready) <= SEARCH_WINDOW_S:
                                trip, board = t, (i, s)

        # Walk to nearby stops from everything a ride just reached. A stop a
        # ride reached this round keeps that ride as its label: unwinding a
        # walk expects to find a ride at the stop it started from.
        rode = dict(improved)
        for s, arr in rode.items():
            for n, w in _walks_from(data, s):
                t = arr + w
                if n not in rode and t < best.get(n, (_INF,))[0] and t < best_total:
                    best[n] = (t, k)
                    improved[n] = t
                    parents[n] = ("walk", s, w)
        rounds.append(parents)
        marked = set(improved)

        finish = min(((improved[s] + w, s) for s, w in ends.items() if s in improved), default=None)
        if finish and finish[0] < best_total:
            best_total = finish[0]
            results.append((finish[0], _unwind(rounds, views, k, finish[1], ends[finish[1]])))
        if not marked:
            break
    return results


def _unwind(rounds: list[dict], views: dict, k: int, stop: int, walk_end: float) -> list:
    """Follow parent labels back from the final stop to the start, giving
    legs in search order (ride legs carry search-direction positions)."""
    legs = [("walk", stop, None, walk_end)]
    while k > 0:
        label = rounds[k][stop]
        if label[0] == "walk":
            _, frm, w = label
            legs.append(("walk", frm, stop, w))
            stop = frm
            label = rounds[k][stop]
        _, p, trip, i, j, board_stop = label
        legs.append(("ride", p, views[p], trip, i, j))
        stop, k = board_stop, k - 1
    legs.append(("walk", None, stop, None))
    legs.reverse()
    return legs


def _transfers(data: dict, day: date, target_s: int, arrive_by: bool,
               near_o: dict, near_d: dict) -> list[dict]:
    """RAPTOR's journeys, in the same shape `_direct` returns. Mostly ones
    with transfers, but a single ride can appear too: one whose walk passes a
    nearby stop on the way to a station farther out, which `_direct` (stops
    within reach only) cannot see."""
    if arrive_by:
        starts = {s: -(target_s - w) for s, w in near_d.items()}
        found = _raptor(data, day, starts, near_o, forward=False)
    else:
        starts = {s: target_s + w for s, w in near_o.items()}
        found = _raptor(data, day, starts, near_d, forward=True)

    out = []
    for _, search_legs in found:
        legs = _real_legs(data, search_legs, arrive_by, near_o, near_d)
        rides = [leg for leg in legs if leg[0] == "ride"]
        # A line split into two patterns where they meet — the 25 runs out to
        # Treasure Island as one trip and loops back as the next — is one
        # ride to the rider: staying on, not transferring. _build merges them.
        lines = [data["patterns"][r[1]]["route"] for r in rides]
        lines = [x for i, x in enumerate(lines) if i == 0 or x != lines[i - 1]]
        times = data["times"]
        first, last = rides[0], rides[-1]
        dep = times[first[2] + first[4]] - first[3]
        arr = times[last[2] + last[5]] - last[3]
        out.append({
            "leave": dep - legs[0][3], "arrive": arr + legs[-1][3], "rides": len(lines),
            "lines": tuple(lines),
            "legs": legs,
        })
    return out


def _real_legs(data: dict, search_legs: list, backward: bool, near_o: dict, near_d: dict) -> list:
    """Turn search-order legs into real-world order with real positions."""
    legs = []
    for leg in search_legs:
        if leg[0] == "walk":
            legs.append(leg)
            continue
        _, p, view, trip, i, j = leg
        off, shift = view.trips[trip]
        if view.forward:
            legs.append(("ride", p, off, shift, i, j))
        else:
            # Backward, the search boarded at the later real stop.
            legs.append(("ride", p, off, shift, view.n - 1 - j, view.n - 1 - i))
    if backward:
        legs = [(l[0], l[2], l[1], l[3]) if l[0] == "walk" else l for l in reversed(legs)]
    # Fill the walking estimates at the two ends from the search inputs.
    first, last = legs[0], legs[-1]
    legs[0] = ("walk", None, first[2], near_o[first[2]])
    legs[-1] = ("walk", last[1], None, near_d[last[1]])
    return legs


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

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


def _point(data: dict, stop: int | None, fallback: tuple[float, float]) -> tuple[float, float]:
    return fallback if stop is None else (data["stops"][stop][1], data["stops"][stop][2])


def _build(data: dict, day: date, journey: dict, origin, destination) -> dict:
    """A journey as the page, the email and the brief read it."""
    times, stops = data["times"], data["stops"]
    # Back-to-back walks (home → a nearby stop → on to a station) are one walk
    # to the rider, and one Mapbox route is more accurate than two.
    legs = []
    for leg in journey["legs"]:
        if leg[0] == "walk" and legs and legs[-1][0] == "walk":
            legs[-1] = ("walk", legs[-1][1], leg[2], legs[-1][3] + leg[3])
        else:
            legs.append(leg)

    walks = [leg for leg in legs if leg[0] == "walk"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_walk, _point(data, w[1], origin), _point(data, w[2], destination), w[3])
                   for w in walks]
        walked = iter([f.result() for f in futures])

    segments, previous = [], None
    for leg in legs:
        if leg[0] == "ride" and previous and previous[0] == "ride" \
                and data["patterns"][previous[1]]["route"] == data["patterns"][leg[1]]["route"]:
            # Same line, same stop, straight after: the rider stays on board.
            _, p, off, shift, i, j = leg
            pat, ride = data["patterns"][p], segments[-1]
            more = _ride_coords(data, pat, i, j)
            arrive = _at(day, times[off + j] - shift)
            ride.update({"to_stop": stops[pat["stops"][j]][0], "stops": ride["stops"] + j - i,
                         "arrive": arrive.isoformat(), "arrive_text": _clock(arrive),
                         "duration_s": int((arrive - datetime.fromisoformat(ride["depart"])).total_seconds()),
                         "distance_m": ride["distance_m"] + round(sum(
                             _meters(a[1], a[0], b[1], b[0]) for a, b in zip(more, more[1:]))),
                         "coords": ride["coords"] + more[1:]})
            previous = leg
            continue
        previous = leg
        if leg[0] == "walk":
            done = next(walked)
            if leg[1] is not None and leg[2] is not None and leg[1] == leg[2]:
                continue                                    # changing at the same stop
            segments.append({"kind": "walk", **done,
                             "to": stops[leg[2]][0] if leg[2] is not None else None})
            continue
        _, p, off, shift, i, j = leg
        pat = data["patterns"][p]
        route = data["routes"][pat["route"]]
        board, alight = stops[pat["stops"][i]], stops[pat["stops"][j]]
        short, full = _line_names(route)
        coords = _ride_coords(data, pat, i, j)
        depart = _at(day, times[off + i] - shift)
        arrive = _at(day, times[off + j] - shift)
        segments.append({
            "kind": "transit", "line": short, "line_name": full,
            "color": route["color"], "text_color": route["text_color"],
            "vehicle": "Streetcar" if short in _HISTORIC_STREETCARS and route["agency"] == "Muni"
                       else _VEHICLE.get(route["type"], "Transit"),
            # Muni's feed writes apostrophes as backticks ("Fisherman`s Wharf").
            "agency": route["agency"], "headsign": (pat["headsign"] or "").replace("`", "'") or None,
            "from_stop": board[0], "to_stop": alight[0], "stops": j - i,
            "depart": depart.isoformat(), "arrive": arrive.isoformat(),
            "depart_text": _clock(depart), "arrive_text": _clock(arrive),
            "duration_s": int((arrive - depart).total_seconds()),
            "distance_m": round(sum(_meters(a[1], a[0], b[1], b[0]) for a, b in zip(coords, coords[1:]))),
            "coords": coords,
        })

    rides = [s for s in segments if s["kind"] == "transit"]
    # Real walking times replace the estimates at both ends.
    first_walk = segments[0]["duration_s"] if segments[0]["kind"] == "walk" else 0
    last_walk = segments[-1]["duration_s"] if segments[-1]["kind"] == "walk" else 0
    leave = datetime.fromisoformat(rides[0]["depart"]) - timedelta(seconds=first_walk)
    done = datetime.fromisoformat(rides[-1]["arrive"]) + timedelta(seconds=last_walk)
    return {
        "duration_s": round((done - leave).total_seconds()),
        "distance_m": sum(s["distance_m"] for s in segments),
        "leave_at": leave.isoformat(), "leave_text": _clock(leave),
        "arrive_at": done.isoformat(), "arrive_text": _clock(done),
        "transfers": len(rides) - 1,
        "lines": [r["line"] or r["agency"] for r in rides],
        "segments": segments,
    }


def plan(origin: tuple[float, float], destination: tuple[float, float],
         arrive_by: str | None = None, alternatives: bool = True) -> dict:
    """Transit options between two (lat, lon) points: direct rides and trips
    with up to two transfers.

    `arrive_by` is a naive Pacific time ("2026-10-20T08:30"); without it the
    trip leaves now. Always returns `routes` (possibly empty) and, when empty,
    an `error` that says why — so "no data", "too far from a stop" and "no
    route" are distinguishable instead of all looking like nothing.
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

    # No blanket expiry check: every service carries its own end date, so an
    # agency whose schedules have run out simply stops contributing trips
    # while the others keep planning. /api/health and the nightly check are
    # what notice an expiry.

    cache_key = (round(origin[0], 4), round(origin[1], 4), round(destination[0], 4),
                 round(destination[1], 4), arrive_by or when.strftime("%Y-%m-%dT%H:%M"), alternatives)
    hit = _cache.get(cache_key)
    if hit and monotonic() - hit[0] < _CACHE_TTL_S:
        return hit[1]

    near_o, near_d = _stops_near(data, *origin), _stops_near(data, *destination)
    if not near_o:
        result = {"routes": [], "error": "no transit stop within a short walk of home"}
    elif not near_d:
        result = {"routes": [], "error": "no transit stop within a short walk of the destination"}
    else:
        result = {"routes": [_build(data, day, j, origin, destination)
                             for j in _choose(data, day, target, bool(arrive_by), near_o, near_d,
                                              origin, destination, 3 if alternatives else 1)]}
        if not result["routes"]:
            walk_all = _meters(*origin, *destination) * DETOUR / WALK_SPEED_MPS
            result["error"] = (f"walking is about as fast (around {round(walk_all / 60)} min)"
                               if walk_all <= 45 * 60 else
                               "no route found with up to two transfers in the next few hours")

    _cache[cache_key] = (monotonic(), result)
    return result


def _choose(data, day, target, arrive_by, near_o, near_d, origin, destination, limit) -> list[dict]:
    """Rank direct rides and transfer journeys together and pick the few worth showing."""
    # Arriving by a deadline, the best journey is the one you can leave latest
    # for; leaving now, it's the one that gets you there first. Fewer rides
    # break ties.
    rank = ((lambda j: (-j["leave"], j["arrive"], j["rides"])) if arrive_by
            else (lambda j: (j["arrive"], j["rides"], -j["leave"])))
    # A ride that barely beats walking the whole way isn't worth offering.
    walk_all = _meters(*origin, *destination) * DETOUR / WALK_SPEED_MPS
    worth = lambda j: j["arrive"] - j["leave"] <= walk_all - WORTH_RIDING_S

    found = [j for j in _transfers(data, day, target, arrive_by, near_o, near_d) if worth(j)]
    singles = [j for j in _direct(data, day, target, arrive_by, near_o, near_d) if worth(j)]
    singles += [j for j in found if j["rides"] == 1]
    # One option per line: the N in two nearby patterns is still the N.
    per_line: dict = {}
    for j in sorted(singles, key=rank):
        per_line.setdefault(j["lines"], j)
    options = sorted(per_line.values(), key=rank)

    # Every extra change of line has to earn its place: a journey is kept
    # only if it beats everything with fewer rides by TRANSFER_WORTH_S —
    # leaving that much later, or arriving that much sooner. Two transfers to
    # leave three minutes later than one transfer isn't worth it.
    gain = (lambda j, other: j["leave"] - other["leave"]) if arrive_by \
        else (lambda j, other: other["arrive"] - j["arrive"])
    for j in sorted((j for j in found if j["rides"] > 1), key=lambda j: (j["rides"], rank(j))):
        fewer = [o for o in options if o["rides"] < j["rides"]]
        if all(gain(j, o) >= TRANSFER_WORTH_S for o in fewer):
            options.append(j)
    return sorted(options, key=rank)[:limit]
