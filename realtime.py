"""
Live transit: delays, cancellations and service alerts from 511 SF Bay.

The planner (transit.py) works from published timetables. This module lays
what is actually happening on top of a planned trip: for each ride, how late
that exact vehicle is running at your stop, whether it was cancelled, whether
a delay means you'll miss your connection, and any service alert on the line
or its stops.

511 serves the standard GTFS-realtime feeds as JSON with `format=json`, one
request per agency: trip updates (every trip's predicted delay per stop) and
service alerts.

**The request budget is the design constraint.** The 511 token allows 60
requests an hour, and the traffic alerts on the map already use up to 30 of
them. So every 511 call in the app goes through `record()`, live transit may
use at most `REALTIME_PER_HOUR`, trip updates are reused for 90 seconds and
alerts for 5 minutes, and when the budget is spent, slightly older data is
served — always labelled with how old it is — rather than none. Live data is
only fetched for agencies in a trip someone is looking at, and only for trips
leaving soon: a prediction for next Tuesday doesn't exist.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from time import monotonic, time

import requests

_BASE = "https://api.511.org/transit/{}"
HOUR_LIMIT = 60                  # 511's per-token ceiling
REALTIME_PER_HOUR = 25           # live transit's share; traffic keeps the rest
_SAFETY = 3                      # never spend the last few

TRIP_TTL_S = 90
ALERT_TTL_S = 5 * 60
STALE_LIMIT_S = 15 * 60          # older than this is not "live" any more
LIVE_HORIZON_S = 90 * 60         # predictions only exist for trips leaving soon
ON_TIME_S = 60                   # within a minute either way is on time
CHANGE_S = 2 * 60                # same connection time the planner uses

_calls: deque = deque()          # (timestamp, kind) of every 511 request
_lock = threading.Lock()
_trips: dict = {}                # op -> (fetched, feed_time, {trip_id: update})
_alerts: dict = {}               # op -> (fetched, [alert])


# ---------------------------------------------------------------------------
# The shared 511 budget
# ---------------------------------------------------------------------------

def _recent() -> list:
    cutoff = time() - 3600
    while _calls and _calls[0][0] < cutoff:
        _calls.popleft()
    return list(_calls)


def record(kind: str) -> None:
    """Note a 511 request. Every caller in the app reports here — the traffic
    feed included — so the budget sees the token's real use."""
    with _lock:
        _calls.append((time(), kind))


def allowed(kind: str) -> bool:
    with _lock:
        recent = _recent()
        total = len(recent)
        if kind == "traffic":
            return total < HOUR_LIMIT - 1
        live = sum(1 for _, k in recent if k != "traffic")
        return total < HOUR_LIMIT - _SAFETY and live < REALTIME_PER_HOUR


def usage() -> dict:
    with _lock:
        recent = _recent()
    return {"last_hour": len(recent), "live": sum(1 for _, k in recent if k != "traffic")}


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

def _fetch(endpoint: str, op: str) -> dict | None:
    token = os.environ.get("TRAFFIC_511_TOKEN")
    if not token or not allowed("realtime"):
        return None
    record("realtime")
    try:
        resp = requests.get(_BASE.format(endpoint),
                            params={"api_key": token, "agency": op, "format": "json"}, timeout=10)
        resp.raise_for_status()
        return json.loads(resp.content.decode("utf-8-sig"))   # 511 prefixes a BOM
    except Exception:
        return None


def trip_updates(op: str) -> tuple[float | None, dict]:
    """(feed timestamp, {trip_id: update}) for one agency, cached. Each update
    is {"canceled": bool, "delays": {stop_id: seconds}}."""
    cached = _trips.get(op)
    if cached and monotonic() - cached[0] < TRIP_TTL_S:
        return cached[1], cached[2]
    data = _fetch("tripupdates", op)
    if data is None:
        if cached and monotonic() - cached[0] < STALE_LIMIT_S:
            return cached[1], cached[2]               # over budget: older, labelled
        return None, {}
    updates = {}
    for entity in data.get("Entities") or []:
        tu = entity.get("TripUpdate") or {}
        trip = tu.get("Trip") or {}
        if not trip.get("TripId"):
            continue
        delays = {}
        for stu in tu.get("StopTimeUpdates") or []:
            event = stu.get("Departure") or stu.get("Arrival") or {}
            if stu.get("StopId") and event.get("Delay") is not None:
                delays[str(stu["StopId"])] = int(event["Delay"])
        updates[str(trip["TripId"])] = {
            # GTFS-realtime: schedule_relationship 3 is CANCELED.
            "canceled": trip.get("schedule_relationship") in (3, "CANCELED"),
            "delays": delays,
        }
    stamp = (data.get("Header") or {}).get("Timestamp")
    _trips[op] = (monotonic(), stamp, updates)
    return stamp, updates


def service_alerts(op: str) -> list[dict]:
    """Current service alerts for one agency, cached."""
    cached = _alerts.get(op)
    if cached and monotonic() - cached[0] < ALERT_TTL_S:
        return cached[1]
    data = _fetch("servicealerts", op)
    if data is None:
        return cached[1] if cached else []
    alerts = []
    for entity in data.get("Entities") or []:
        a = entity.get("Alert") or {}
        text = lambda field: next((t.get("Text") for t in ((a.get(field) or {}).get("Translations") or [])
                                   if t.get("Language") in (None, "", "en")), None)
        alerts.append({
            "id": entity.get("Id"),
            "header": text("HeaderText"),
            "description": text("DescriptionText"),
            "url": text("Url"),
            "periods": [(p.get("Start") or 0, p.get("End") or 0) for p in a.get("ActivePeriods") or []],
            "routes": {str(e["RouteId"]) for e in a.get("InformedEntities") or [] if e.get("RouteId")},
            "stops": {str(e["StopId"]) for e in a.get("InformedEntities") or [] if e.get("StopId")},
        })
    _alerts[op] = (monotonic(), alerts)
    return alerts


def _active(alert: dict, when: float) -> bool:
    return not alert["periods"] or any(s <= when and (not e or when <= e) for s, e in alert["periods"])


# ---------------------------------------------------------------------------
# Laying live data over a planned trip
# ---------------------------------------------------------------------------

def _clock(moment: datetime) -> str:
    from transit import _PACIFIC
    return moment.astimezone(_PACIFIC).strftime("%I:%M %p").lstrip("0")


def _status(delay: int | None) -> str:
    if delay is None:
        return "scheduled"
    if delay >= ON_TIME_S:
        return "late"
    if delay <= -ON_TIME_S:
        return "early"
    return "on_time"


def annotate(plan: dict) -> dict:
    """Add live status to every ride of every route in a /api/transit result.

    Each ride gains `live`: status (on_time / late / early / canceled /
    scheduled = no live data), delay in seconds, predicted departure and
    arrival, and any service alerts for its line or stops. Each route gains
    `live`: when the data is from, the predicted arrival, and warnings — a
    cancellation, or a delay that leaves less than the connection time.
    """
    now = time()
    for route in plan.get("routes") or []:
        rides = [s for s in route["segments"] if s.get("kind") == "transit"]
        if not rides:
            continue
        first = datetime.fromisoformat(rides[0]["depart"]).timestamp()
        if first - now > LIVE_HORIZON_S:
            continue                                  # too far ahead for predictions
        stamps, warnings, seen_alerts = [], [], set()
        for ride in rides:
            op = ride.get("op")
            stamp, updates = trip_updates(op) if op else (None, {})
            if stamp:
                stamps.append(stamp)
            board = updates.get(ride.get("trip_id") or "")
            alight = updates.get(ride.get("alight_trip_id") or ride.get("trip_id") or "")
            depart = datetime.fromisoformat(ride["depart"])
            arrive = datetime.fromisoformat(ride["arrive"])
            live = {"status": "scheduled", "delay_s": None}
            if board and board["canceled"]:
                live["status"] = "canceled"
                warnings.append(f"The {ride.get('line') or ride.get('line_name')} at "
                                f"{depart.strftime('%I:%M %p').lstrip('0')} is cancelled")
            elif board:
                d_board = board["delays"].get(ride.get("board_stop_id") or "")
                d_alight = (alight or {}).get("delays", {}).get(ride.get("alight_stop_id") or "", d_board)
                if d_board is not None:
                    live.update({
                        "status": _status(d_board), "delay_s": d_board,
                        "depart_live": (depart + timedelta(seconds=d_board)).isoformat(),
                        "depart_live_text": _clock(depart + timedelta(seconds=d_board)),
                    })
                    if d_alight is not None:
                        live.update({
                            "arrive_live": (arrive + timedelta(seconds=d_alight)).isoformat(),
                            "arrive_live_text": _clock(arrive + timedelta(seconds=d_alight)),
                        })
            # Alerts on this line or its stops, active when you ride it.
            ride_alerts = []
            for alert in service_alerts(op) if op else []:
                if alert["id"] in seen_alerts or not _active(alert, depart.timestamp()):
                    continue
                if (ride.get("route_id") in alert["routes"]
                        or alert["stops"] & set(ride.get("stop_ids") or [])
                        or (not alert["routes"] and not alert["stops"])):
                    seen_alerts.add(alert["id"])
                    ride_alerts.append({k: alert[k] for k in ("header", "description", "url")})
            live["alerts"] = ride_alerts
            ride["live"] = live

        # Connections: does the first ride, as it's actually running, still
        # leave time to make the second?
        for a, b in zip(rides, rides[1:]):
            arrive_a = datetime.fromisoformat(a["live"].get("arrive_live") or a["arrive"])
            depart_b = datetime.fromisoformat(b["live"].get("depart_live") or b["depart"])
            walk = next((s["duration_s"] for s in route["segments"][route["segments"].index(a) + 1:
                         route["segments"].index(b)] if s["kind"] == "walk"), 0)
            # Spare time once you've walked between the two (or, at the same
            # stop, the bare minute it takes to step off and across).
            spare = (depart_b - arrive_a).total_seconds() - (walk or 60)
            if b["live"]["status"] == "canceled":
                continue
            name_a, name_b = a.get("line") or a.get("line_name"), b.get("line") or b.get("line_name")
            if spare < 0:
                b["live"]["missed_connection"] = True
                warnings.append(f"You may miss the {name_b}: the {name_a} now gets in at {_clock(arrive_a)}")
            elif spare < CHANGE_S and a["live"]["status"] in ("late", "early"):
                # Only worth saying when live data made it tight; a tight
                # timetable connection was already priced in by the planner.
                b["live"]["tight_connection"] = True
                warnings.append(f"Tight connection for the {name_b}: the {name_a} now gets in at "
                                f"{_clock(arrive_a)}, leaving about {max(0, round(spare / 60))} min")

        last = rides[-1]["live"]
        tail = route["segments"][-1]["duration_s"] if route["segments"][-1]["kind"] == "walk" else 0
        arrive_live = None
        if last.get("arrive_live"):
            arrive_live = datetime.fromisoformat(last["arrive_live"]) + timedelta(seconds=tail)
        live_any = any(r["live"]["status"] != "scheduled" for r in rides)
        # One line for the top of the plan, from the most delayed ride — not
        # from the last ride, which may simply have no live data yet.
        worst = max((r for r in rides if r["live"].get("delay_s") is not None),
                    key=lambda r: abs(r["live"]["delay_s"]), default=None)
        if any(r["live"]["status"] == "canceled" for r in rides):
            summary = "A ride on this route is cancelled"
        elif worst and abs(worst["live"]["delay_s"]) >= ON_TIME_S:
            minutes = round(abs(worst["live"]["delay_s"]) / 60)
            summary = (f"The {worst.get('line') or worst.get('line_name')} is running {minutes} min "
                       f"{'late' if worst['live']['delay_s'] > 0 else 'early'}")
        elif worst:
            summary = "Running to schedule"
        else:
            summary = None
        route["live"] = {
            "available": live_any,
            "summary": summary,
            "as_of": datetime.fromtimestamp(min(stamps), timezone.utc).isoformat() if stamps else None,
            "as_of_text": _clock(datetime.fromtimestamp(min(stamps), timezone.utc)) if stamps else None,
            "arrive_text": _clock(arrive_live) if arrive_live else None,
            "delay_s": rides[-1]["live"].get("delay_s"),
            "warnings": warnings,
        }
    return plan
