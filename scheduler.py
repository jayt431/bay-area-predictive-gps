"""
The scheduler: what trips are coming up, and keeping them in sync with a
calendar feed.

Two sources feed one list. Manual trips are entered in the app and may recur on
chosen weekdays; calendar trips are imported from an .ics feed and are always
concrete one-off occurrences. Both live in the same table so everything
downstream — the upcoming list, the pre-trip alerts — treats them alike.

`DAYS_AHEAD` bounds the import. A calendar feed can contain years of a weekly
standup; only the near future is actionable. It covers about two months so the
panel's month view is populated through the end of next month.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import calendar_sync
import store

DAYS_AHEAD = 62
ICS_URL_KEY = "ics_url"
ALERT_EMAIL_KEY = "alert_email"
_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def local_now() -> datetime:
    return datetime.now(calendar_sync._PACIFIC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Calendar import
# ---------------------------------------------------------------------------

def sync_calendar(days_ahead: int = DAYS_AHEAD, user_id: str = store.LOCAL_USER) -> dict:
    """Pull the stored .ics feed into the schedule.

    Reconciliation is by UID+start: an occurrence keeps its row across syncs, a
    moved meeting updates in place, and anything no longer in the feed is
    dropped.

    Every sync re-geocodes rather than trusting coordinates already stored.
    Reusing them meant a bad match was permanent — a location that once
    resolved wrongly kept its wrong coordinates even after the geocoder was
    fixed. Within a single run `resolved` still dedupes by location string, so
    a recurring meeting costs one lookup, not one per occurrence.
    """
    url = store.get_setting(ICS_URL_KEY, user_id)
    if not url:
        return {"ok": False, "error": "no calendar connected"}

    text = calendar_sync.fetch_ics(url)
    if text is None:
        return {"ok": False, "error": "could not fetch or parse that calendar feed"}

    occurrences = calendar_sync.expand(calendar_sync.parse_events(text),
                                       days_ahead=days_ahead, now=local_now())

    known = {t.get("external_id"): t for t in store.list_trips(user_id)
             if t.get("source") == "ics"}
    resolved: dict[str, tuple] = {}
    imported, skipped, seen = 0, [], set()

    for occurrence in occurrences:
        # One row per occurrence, so a recurring meeting yields one trip per day.
        external_id = f"{occurrence['uid']}@{occurrence['start'].isoformat()}"
        location = occurrence["location"]

        if location in resolved:
            point = resolved[location]
        else:
            point = calendar_sync.geocode(location)
            resolved[location] = point          # cache misses too, so one bad
                                                # location costs one lookup
        if not point:
            if location not in skipped:
                skipped.append(location)
            continue

        lat, lon, _place = point
        store.save_trip({
            "label": occurrence["summary"],
            "destination": location,
            "dest_lat": lat,
            "dest_lon": lon,
            "arrive_at": occurrence["start"].isoformat(timespec="minutes"),
            "source": "ics",
            "external_id": external_id,
        }, user_id)
        # Marked as seen only once it is actually stored, so an occurrence that
        # fails to geocode is treated as absent and its stale row is cleaned up.
        seen.add(external_id)
        imported += 1

    removed = 0
    for external_id, trip in known.items():
        if external_id not in seen:
            store.delete_trip(trip["id"], user_id)
            removed += 1

    return {"ok": True, "imported": imported, "removed": removed,
            "skipped": skipped, "window_days": days_ahead}


# ---------------------------------------------------------------------------
# Upcoming trips
# ---------------------------------------------------------------------------

def next_occurrence(trip: dict, now: datetime) -> datetime | None:
    """When this trip next happens, or None if it never does again.

    One-off trips have an `arrive_at`. Recurring manual trips carry weekdays
    and a time of day, and the next occurrence is searched over the coming week.
    """
    if trip.get("arrive_at"):
        try:
            when = datetime.fromisoformat(trip["arrive_at"])
        except ValueError:
            return None
        return when if when >= now else None

    days, time_of_day = trip.get("days"), trip.get("time_of_day")
    if not days or not time_of_day:
        return None
    try:
        hour, minute = (int(part) for part in time_of_day.split(":")[:2])
    except ValueError:
        return None

    wanted = {d.strip().upper() for d in days.split(",") if d.strip()}
    for offset in range(0, 8):
        day = now + timedelta(days=offset)
        if _WEEKDAYS[day.weekday()] not in wanted:
            continue
        candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate >= now:
            return candidate
    return None


def upcoming(within_hours: float | None = None, user_id: str = store.LOCAL_USER,
             now: datetime | None = None) -> list[dict]:
    """Enabled trips that still lie ahead, soonest first."""
    now = now or local_now()
    horizon = now + timedelta(hours=within_hours) if within_hours else None

    out = []
    for trip in store.list_trips(user_id, include_disabled=False):
        when = next_occurrence(trip, now)
        if not when or (horizon and when > horizon):
            continue
        enriched = dict(trip)
        enriched["next_at"] = when.isoformat(timespec="minutes")
        enriched["hours_away"] = round((when - now).total_seconds() / 3600, 2)
        out.append(enriched)
    out.sort(key=lambda t: t["next_at"])
    return out
