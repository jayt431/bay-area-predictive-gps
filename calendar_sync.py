"""
Calendar import over secret iCal (.ics) URLs.

Google Calendar, Apple iCloud and Outlook all publish a private .ics feed, so
one parser covers every provider — no OAuth, no per-provider client, no user
accounts. In Google it is Settings -> <calendar> -> "Secret address in iCal
format".

What this deliberately does NOT do:
  - write back to the calendar (the feed is read-only by nature)
  - notice an edit instantly; Google caches its ICS output, so a change can
    take hours to appear. OAuth is the fix when that lag starts to matter.
  - import events with no LOCATION, or all-day events with no start time —
    neither can be routed to.

The secret URL is a bearer credential: anyone holding it can read that
calendar. It is stored server-side in the settings table, never sent to the
browser after it is saved.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

import requests
from dateutil import rrule as dateutil_rrule
from dateutil import tz

_GEOCODE_URL = "https://api.mapbox.com/geocoding/v5/mapbox.places/{}.json"
# Region bias for ambiguous location strings ("Main St" is everywhere). These
# match the map's own search box. When this goes multi-region they should be
# derived from the user's home rather than pinned to the Bay Area.
_GEOCODE_PROXIMITY = os.environ.get("GEOCODE_PROXIMITY", "-122.42,37.77")
_GEOCODE_BBOX = os.environ.get("GEOCODE_BBOX", "-122.65,37.20,-121.70,38.20")
_GEOCODE_COUNTRY = os.environ.get("GEOCODE_COUNTRY", "US")

# Mapbox always returns *something*. Unconstrained, "zzzqqq not a real place"
# resolves to a village in Poland, and venue names land on similarly-spelled
# streets ("Chase Center" -> "Chase Court, Fremont", relevance 0.65) because v5
# geocoding has thin POI coverage. In the map's search box a human sees five
# suggestions and picks; an unattended import takes the top hit, so it has to
# refuse weak matches instead. Skipped events are reported back to the user.
_GEOCODE_MIN_RELEVANCE = float(os.environ.get("GEOCODE_MIN_RELEVANCE", "0.8"))
_PACIFIC = tz.gettz("America/Los_Angeles")

_LINE_RE = re.compile(r"^([A-Za-z0-9-]+)((?:;[^:]*)?):(.*)$")


def fetch_ics(url: str, timeout: int = 20) -> str | None:
    """Fetch a calendar feed. Returns None on anything that isn't a calendar."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig", errors="replace")
    except Exception:
        return None
    return text if "BEGIN:VCALENDAR" in text else None


def _unfold(text: str) -> list[str]:
    """RFC 5545 folds long lines by continuing them with a leading space or tab."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (value.replace("\\n", " ").replace("\\,", ",")
                 .replace("\;", ";").replace("\\\\", "\\")).strip()


def _parse_dt(params: str, value: str) -> tuple[datetime | None, bool]:
    """Parse a DTSTART. Returns (naive local datetime, is_all_day).

    Three shapes appear in the wild: a UTC stamp ending in Z, a floating or
    TZID-qualified local stamp, and a date-only VALUE=DATE for all-day events.
    Everything is normalised to naive Pacific local time, which is what the
    rest of the app reasons in.
    """
    value = value.strip()
    if "VALUE=DATE" in params.upper() or re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value[:8], "%Y%m%d"), True
        except ValueError:
            return None, True

    tzid = ""
    match = re.search(r"TZID=([^;:]+)", params)
    if match:
        tzid = match.group(1).strip()

    try:
        if value.endswith("Z"):
            stamp = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=tz.UTC)
        else:
            stamp = datetime.strptime(value, "%Y%m%dT%H%M%S")
            stamp = stamp.replace(tzinfo=tz.gettz(tzid) or _PACIFIC)
    except ValueError:
        return None, False
    return stamp.astimezone(_PACIFIC).replace(tzinfo=None), False


def parse_events(text: str) -> list[dict]:
    """Pull VEVENTs out of a calendar feed. Recurrence is not expanded here."""
    events: list[dict] = []
    current: dict | None = None
    for line in _unfold(text):
        if line.startswith("BEGIN:VEVENT"):
            current = {"exdates": []}
            continue
        if line.startswith("END:VEVENT"):
            if current:
                events.append(current)
            current = None
            continue
        if current is None:
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        name, params, value = match.group(1).upper(), match.group(2), match.group(3)
        if name == "UID":
            current["uid"] = value.strip()
        elif name == "SUMMARY":
            current["summary"] = _unescape(value)
        elif name == "LOCATION":
            current["location"] = _unescape(value)
        elif name == "DTSTART":
            current["start"], current["all_day"] = _parse_dt(params, value)
        elif name == "RRULE":
            current["rrule"] = value.strip()
        elif name == "EXDATE":
            for piece in value.split(","):
                stamp, _ = _parse_dt(params, piece)
                if stamp:
                    current["exdates"].append(stamp)
        elif name == "STATUS":
            current["status"] = value.strip().upper()
    return events


def expand(events: list[dict], days_ahead: int = 14,
           now: datetime | None = None) -> list[dict]:
    """Flatten events into concrete occurrences inside the window.

    Only events that can actually be driven to survive: they need a location
    and a real start time. Cancelled events and all-day entries are dropped.
    """
    now = now or datetime.now(_PACIFIC).replace(tzinfo=None)
    horizon = now + timedelta(days=days_ahead)
    out: list[dict] = []

    for event in events:
        start = event.get("start")
        if not start or event.get("all_day") or not event.get("location"):
            continue
        if event.get("status") == "CANCELLED":
            continue

        occurrences: list[datetime] = []
        if event.get("rrule"):
            try:
                rule = dateutil_rrule.rrulestr(event["rrule"], dtstart=start)
                occurrences = list(rule.between(now, horizon, inc=True))
            except Exception:
                occurrences = []          # malformed rule: fall back to the seed
        if not occurrences and now <= start <= horizon:
            occurrences = [start]

        excluded = set(event.get("exdates") or [])
        for occurrence in occurrences:
            if occurrence in excluded:
                continue
            out.append({
                "uid": event.get("uid") or "",
                "summary": event.get("summary") or "Calendar event",
                "location": event["location"],
                "start": occurrence,
            })
    out.sort(key=lambda e: e["start"])
    return out


def geocode(query: str, token: str | None = None) -> tuple[float, float, str] | None:
    """Resolve a calendar LOCATION string to coordinates via Mapbox.

    Returns None rather than a guess when the match is weak — a trip routed to
    the wrong place is worse than a trip the user is told was skipped.
    """
    token = token or os.environ.get("MAPBOX_TOKEN", "")
    if not token or not query:
        return None
    try:
        resp = requests.get(
            _GEOCODE_URL.format(requests.utils.quote(query[:250])),
            params={"access_token": token, "limit": 1,
                    "proximity": _GEOCODE_PROXIMITY,
                    "bbox": _GEOCODE_BBOX,
                    "country": _GEOCODE_COUNTRY},
            timeout=15,
        )
        resp.raise_for_status()
        features = resp.json().get("features") or []
    except Exception:
        return None
    if not features:
        return None
    best = features[0]
    if float(best.get("relevance", 0)) < _GEOCODE_MIN_RELEVANCE:
        return None
    lon, lat = best["center"]
    return float(lat), float(lon), best.get("place_name") or query
