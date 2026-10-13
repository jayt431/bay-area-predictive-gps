"""
Big events that move Bay Area traffic: games, concerts, festivals.

This is the app's reason to exist — Waze can't tell you tonight that a Warriors
game will jam Mission Bay tomorrow at 6:30 — and until now the events in it
were invented sample data that the live site never even showed.

Two sources:

- **Ticketmaster Discovery API** (`TICKETMASTER_API_KEY`, free): concerts,
  Warriors, 49ers, Sharks, big shows. Its terms allow non-commercial use with
  reasonable caching; they forbid deriving revenue from the API, which a
  future paid product would have to revisit.
- **MLB's public schedule** (no key): Giants home games, which Ticketmaster
  doesn't sell. Published for personal, non-commercial use.

Only events at **big venues** count. Ticketmaster lists every bar show in the
region; a 300-person gig doesn't move traffic, an 18,000-seat arena letting
out does. `VENUES` is that list, with each venue's typical crowd.

**Timing is half the signal.** An event affects the roads around it in two
windows: people arriving in the two hours before it starts, and leaving in the
hour after it ends. A trip only cares if it overlaps one of them.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from time import monotonic

import requests
from dateutil import tz

_PACIFIC = tz.gettz("America/Los_Angeles")
_TM_URL = "https://app.ticketmaster.com/discovery/v2/events.json"
_MLB_URL = "https://statsapi.mlb.com/api/v1/schedule"

DAYS_AHEAD = 14
CACHE_TTL_S = 3600               # an hour: schedules don't change by the minute
ARRIVE_WINDOW = timedelta(hours=2)
LEAVE_WINDOW = timedelta(hours=1)
TYPICAL_LENGTH = timedelta(hours=3)
MAJOR_CROWD = 15_000

# Venues big enough to move traffic: a lowercase fragment of the name as
# Ticketmaster writes it -> (display name, typical crowd). Capacities are
# approximate. Anything not listed is ignored.
VENUES = {
    "chase center": ("Chase Center", 18_000),
    "oracle park": ("Oracle Park", 41_000),
    "levi's stadium": ("Levi's Stadium", 68_000),
    "levis stadium": ("Levi's Stadium", 68_000),
    "bill graham civic": ("Bill Graham Civic Auditorium", 8_500),
    "sap center": ("SAP Center", 17_500),
    "oakland arena": ("Oakland Arena", 19_500),
    "oakland-alameda county": ("Oakland Arena", 19_500),
    "coliseum": ("Oakland Coliseum", 50_000),
    "shoreline amphitheatre": ("Shoreline Amphitheatre", 22_500),
    "greek theatre": ("Greek Theatre, Berkeley", 8_500),
    "frost amphitheater": ("Frost Amphitheater", 10_000),
    "stanford stadium": ("Stanford Stadium", 50_000),
    "california memorial stadium": ("California Memorial Stadium", 63_000),
    "golden gate park": ("Golden Gate Park", 75_000),
    "paypal park": ("PayPal Park", 18_000),
    "cow palace": ("Cow Palace", 12_000),
    "kezar stadium": ("Kezar Stadium", 10_000),
}

# Giants home games: Oracle Park, fixed.
_ORACLE_PARK = (37.7786, -122.3893)
_GIANTS = 137

_lock = threading.Lock()
_cache: dict = {"at": None, "events": [], "errors": {}}


def _venue(name: str) -> tuple[str, int] | None:
    low = (name or "").lower()
    return next((v for key, v in VENUES.items() if key in low), None)


def _event(source, ident, name, venue, crowd, lat, lon, start: datetime, url=None, kind=None) -> dict:
    end = start + TYPICAL_LENGTH
    return {
        "id": f"{source}:{ident}", "source": source, "name": name, "venue": venue,
        "crowd": crowd, "major": crowd >= MAJOR_CROWD, "kind": kind,
        "lat": lat, "lon": lon, "url": url,
        "start": start.isoformat(), "end": end.isoformat(),
        "start_text": start.astimezone(_PACIFIC).strftime("%a %I:%M %p").replace(" 0", " "),
        # When the roads around it are worst.
        "busy_from": (start - ARRIVE_WINDOW).isoformat(),
        "busy_until": (end + LEAVE_WINDOW).isoformat(),
    }


def _ticketmaster(key: str, now: datetime) -> list[dict]:
    out, page = [], 0
    params = {
        "apikey": key, "latlong": "37.6,-122.2", "radius": 60, "unit": "miles",
        "startDateTime": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": (now + timedelta(days=DAYS_AHEAD)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size": 200, "sort": "date,asc",
    }
    while page < 5:                                   # 1,000 events at most; well inside 5,000 calls/day
        resp = requests.get(_TM_URL, params={**params, "page": page}, timeout=20)
        if resp.status_code == 401:
            raise RuntimeError("Ticketmaster rejected the API key")
        resp.raise_for_status()
        data = resp.json()
        for e in (data.get("_embedded") or {}).get("events") or []:
            dates = e.get("dates") or {}
            if (dates.get("status") or {}).get("code") in ("canceled", "postponed"):
                continue
            stamp = (dates.get("start") or {}).get("dateTime")
            venue = ((e.get("_embedded") or {}).get("venues") or [{}])[0]
            known = _venue(venue.get("name"))
            if not stamp or not known:
                continue                              # no fixed time, or too small to matter
            loc = venue.get("location") or {}
            try:
                lat, lon = float(loc["latitude"]), float(loc["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            segment = ((e.get("classifications") or [{}])[0].get("segment") or {}).get("name")
            out.append(_event("tm", e.get("id"), e.get("name"), known[0], known[1], lat, lon,
                              datetime.fromisoformat(stamp.replace("Z", "+00:00")), e.get("url"), segment))
        info = data.get("page") or {}
        page += 1
        if page >= (info.get("totalPages") or 0):
            break
    return out


def _giants(now: datetime) -> list[dict]:
    end = now + timedelta(days=DAYS_AHEAD)
    resp = requests.get(_MLB_URL, params={
        "sportId": 1, "teamId": _GIANTS, "hydrate": "venue",
        "startDate": now.strftime("%Y-%m-%d"), "endDate": end.strftime("%Y-%m-%d"),
    }, timeout=20)
    resp.raise_for_status()
    out = []
    for day in resp.json().get("dates") or []:
        for g in day.get("games") or []:
            if (g.get("venue") or {}).get("name") != "Oracle Park":
                continue                              # away game
            if (g.get("status") or {}).get("detailedState") in ("Postponed", "Cancelled"):
                continue
            teams = g.get("teams") or {}
            away = ((teams.get("away") or {}).get("team") or {}).get("name", "")
            start = datetime.fromisoformat(g["gameDate"].replace("Z", "+00:00"))
            out.append(_event("mlb", g.get("gamePk"), f"Giants vs. {away}".strip(), "Oracle Park", 41_000,
                              *_ORACLE_PARK, start, kind="Sports"))
    return out


def upcoming(force: bool = False) -> list[dict]:
    """Every big event in the next two weeks, cached for an hour. A source
    that fails keeps its last good answer out of the way: the others still
    count, and `status()` says which one failed and why."""
    with _lock:
        if not force and _cache["at"] and monotonic() - _cache["at"] < CACHE_TTL_S:
            return _cache["events"]
        now = datetime.now(timezone.utc)
        events, errors = [], {}
        key = os.environ.get("TICKETMASTER_API_KEY")
        if key:
            try:
                events += _ticketmaster(key, now)
            except Exception as exc:
                errors["ticketmaster"] = str(exc)[:200]
        else:
            errors["ticketmaster"] = "TICKETMASTER_API_KEY not set"
        try:
            giants = _giants(now)
            # Ticketmaster sometimes lists the same game; keep one.
            events += [g for g in giants if not any(
                e["venue"] == "Oracle Park" and abs(datetime.fromisoformat(e["start"])
                                                    - datetime.fromisoformat(g["start"])) < timedelta(hours=3)
                for e in events)]
        except Exception as exc:
            errors["mlb"] = str(exc)[:200]
        events.sort(key=lambda e: e["start"])
        _cache.update(at=monotonic(), events=events, errors=errors)
        return events


def status() -> dict:
    upcoming()
    return {"count": len(_cache["events"]), "errors": _cache["errors"]}


def during(start: datetime, end: datetime) -> list[dict]:
    """Events whose busy window (arrivals through departures) overlaps a trip
    running from `start` to `end`. Naive datetimes are taken as Pacific."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=_PACIFIC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=_PACIFIC)
    return [e for e in upcoming()
            if datetime.fromisoformat(e["busy_from"]) <= end and start <= datetime.fromisoformat(e["busy_until"])]


def describe(event: dict) -> str:
    """'Sat 7:30 PM · about 18,000 people — roads busiest 5:30–7:30 PM and as it lets out'"""
    start = datetime.fromisoformat(event["start"]).astimezone(_PACIFIC)
    clock = lambda d: d.strftime("%I:%M %p").lstrip("0")
    return (f"{event['start_text']} at {event['venue']} · about {event['crowd']:,} people — "
            f"roads busiest {clock(start - ARRIVE_WINDOW)}–{clock(start)} and as it lets out")
