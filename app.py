"""
Flask API for the Predictive Route Intelligence Agent.

Endpoints:
  GET  /api/routes          - list available routes
  POST /api/analyze         - analyze a route for a date, return alert
  GET  /api/health          - health check

POST /api/analyze body: { "route_id": "rt_alex_home", "date": "YYYY-MM-DD" }

If ANTHROPIC_API_KEY is set, runs the full agent and parses its structured
alert. Without it, returns the raw tool data so the app is always demoable.
"""

from __future__ import annotations

import copy
import hmac
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, render_template, request

import alerts
import calendar_sync
import realtime
import transit
import events
import mock_data
import places
import push
import scheduler
import store
import tools
import agent
from agent import RouteIntelligenceAgent, api_key_present

app = Flask(__name__)
store.init_db()


@app.before_request
def _open_db_scope():
    """One database connection per request instead of one per call. Lazy, so
    the many endpoints that never touch the database open nothing."""
    store.begin_scope()


@app.teardown_request
def _close_db_scope(exc=None):
    store.end_scope()


@app.route("/")
def index():
    return render_template("index.html", mapbox_token=os.environ.get("MAPBOX_TOKEN", ""))


# ---- Installable app -------------------------------------------------------
# The manifest and service worker make the site installable ("Add to Home
# Screen") and able to receive push notifications. The service worker must be
# served from the site root: its scope is the folder it's served from.

@app.route("/manifest.webmanifest")
def manifest():
    return app.response_class(json.dumps({
        "name": "Bay Area Predictive GPS",
        "short_name": "Bay GPS",
        "description": "Knows about the game, the closure and the delay before you leave.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0A0A0B",
        "theme_color": "#0A0A0B",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    resp = app.send_static_file("sw.js")
    resp.headers["Content-Type"] = "application/javascript"
    # Browsers check for a new service worker on load; never let a cache
    # hold back an update.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/push/key")
def push_key():
    """The public VAPID key the browser needs to subscribe. Public by design."""
    return jsonify({"configured": push.configured(), "public_key": push.public_key()})


@app.route("/api/health")
def health():
    """Readiness at a glance: which sources are keyed, and whether the
    scheduler is actually configured for production.

    Booleans and a backend name only — never a value — so this stays safe to
    curl from anywhere. It exists because "is DATABASE_URL set on the server?"
    was otherwise unanswerable without opening the dashboard, and getting that
    wrong means the schedule silently resets on every restart.
    """
    return jsonify({
        "status": "ok",
        "anthropic_key": api_key_present(),
        "weather_key": bool(os.environ.get("OPENWEATHER_API_KEY")),
        "news_key": bool(os.environ.get("NEWSAPI_KEY")),
        "traffic_511": bool(os.environ.get("TRAFFIC_511_TOKEN")),
        # How many agencies' schedules are loaded, and the earliest date a
        # major one (Muni, BART, Caltrain, AC, SamTrans, VTA, Golden Gate)
        # stops covering. The nightly refresh should keep that moving forward.
        "transit_agencies": transit.agencies(),
        # 511 requests in the last hour (the token allows 60), and how many
        # of them were live transit rather than traffic.
        "requests_511": realtime.usage(),
        # Big events loaded for the next two weeks, and any source that failed.
        "events": events.status(),
        "transit_data_until": transit.data_until(),
        # "sqlite" in production means scheduled trips vanish on restart.
        "schedule_storage": "postgres" if store.using_postgres() else "sqlite",
        # False in production means anyone can read and edit the schedule.
        "schedule_locked": bool(os.environ.get("SCHEDULE_PASSCODE")),
        "cron_protected": bool(os.environ.get("CRON_SECRET")),
        "email_configured": bool(os.environ.get("RESEND_API_KEY")),
        "push_configured": push.configured(),
        # When the clock last ran and what it did (counts only). Stale means
        # the outside scheduler has stopped calling /api/tick.
        "last_tick": _last_tick(),
    })


def _last_tick() -> dict | None:
    try:
        return json.loads(store.get_setting(LAST_TICK_KEY) or "null")
    except Exception:
        return None


@app.route("/api/transit")
def transit_directions():
    """Transit options to a destination, step by step, with up to two
    transfers, planned over every Bay Area agency's 511 schedules. Starts
    from `origin_lat`/`origin_lon` when given, else the demo home.

    Optional `leave_at` or `arrive_by` (Pacific, "2026-10-20T08:30"); without
    either the trip leaves now. Live data is added only for trips leaving
    soon — realtime.annotate skips anything further out by itself.
    """
    try:
        lat, lon = float(request.args["dest_lat"]), float(request.args["dest_lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"routes": [], "error": "dest_lat and dest_lon are required"}), 400
    leave_at, arrive_by = request.args.get("leave_at"), request.args.get("arrive_by")
    for value in (leave_at, arrive_by):
        if value:
            try:
                datetime.fromisoformat(value)
            except ValueError:
                return jsonify({"routes": [], "error": "times must look like 2026-10-20T08:30"}), 400
    # Where from: whatever the page says (home, current location, a searched
    # place), else the public demo starting point.
    try:
        origin = (float(request.args["origin_lat"]), float(request.args["origin_lon"]))
    except (KeyError, TypeError, ValueError):
        origin = (mock_data.HOME["lat"], mock_data.HOME["lon"])
    plan = transit.plan(origin, (lat, lon),
                        arrive_by=arrive_by or None, depart_at=None if arrive_by else leave_at)
    # Live delays and alerts on top. A deep copy, because the planner caches
    # its result and live data must never be cached as if it were schedule.
    return jsonify(realtime.annotate(copy.deepcopy(plan)))


@app.route("/api/routes")
def get_routes():
    routes = [
        {
            "route_id": r["route_id"],
            "user": r["user"],
            "label": r["label"],
            "origin": r["origin"],
            "destination": r["destination"],
            "usual_departure": r["usual_departure"],
            "lat": r["lat"],
            "lon": r["lon"],
        }
        for r in mock_data.ROUTINES.values()
    ]
    return jsonify(routes)


@app.route("/api/pins")
def get_pins():
    """Map pins (red/yellow disruptions) for a route on a date."""
    route_id = request.args.get("route_id", "").strip()
    date = request.args.get("date", "").strip()
    if not route_id or not date:
        return jsonify({"error": "route_id and date query params are required"}), 400
    try:
        origin = mock_data.get_routine(route_id)
    except KeyError:
        return jsonify({"error": f"unknown route_id: {route_id}"}), 404
    return jsonify({
        "route_id": route_id,
        "date": date,
        "origin": {"lat": origin["lat"], "lon": origin["lon"]},
        "pins": mock_data.get_alert_pins(route_id, date),
    })


@app.route("/api/config")
def config():
    """Single-user config: the fixed home base the map starts from."""
    return jsonify({"home": mock_data.HOME})


@app.route("/api/disruptions")
def disruptions():
    """The Bay Area pool of possible disruptions. The frontend matches these
    against the drawn route and keeps only the ones that fall near it.

    `source` and `note` make the fallback visible: a mocked pool with a note
    explaining why is diagnosable, a mocked pool that looks real is not.
    """
    pool = mock_data.get_disruptions()
    live = bool(pool) and pool[0].get("source") == "511.org"
    return jsonify({
        "disruptions": pool,
        "source": "511.org" if live else "mock",
        "note": None if live else mock_data.last_511_error(),
    })


@app.route("/api/meters")
def meters():
    """Real metered street parking near a point (SF DataSF), grouped by street."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "numeric lat and lon query params are required"}), 400
    return jsonify(mock_data.get_metered_streets(lat, lon))


@app.route("/api/civic")
def civic():
    """Live civic/mobility events near a point (SF 311). One source, two views:
    the GPS on-route detection and the Area Feed both consume this."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "numeric lat and lon query params are required"}), 400
    radius = request.args.get("radius", 1500, type=int)
    return jsonify(mock_data.get_civic_events(lat, lon, radius=radius))


# The map app is single-user out of a fixed home base, so news is scoped to the
# city that home sits in rather than to a per-route corridor.
_BRIEF_NEWS_AREA = "San Francisco"

try:  # stdlib on 3.9+, but needs system tzdata present
    from zoneinfo import ZoneInfo

    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover - fall back to a fixed offset
    _PACIFIC = None


def _local_now() -> datetime:
    """Now in Bay Area local time. The server clock is UTC on Render, and the
    trip window has to be expressed in the same local hours the forecast uses."""
    if _PACIFIC is not None:
        return datetime.now(_PACIFIC)
    return datetime.utcnow() - timedelta(hours=8)


def _weather_point(ctx: dict) -> tuple[float, float, str]:
    """Where to forecast: the destination, which is what the driver is heading
    into. Bay Area weather is local enough that the destination and the home
    base can genuinely differ over a cross-city trip. Falls back to home when
    the client sends no destination coordinates."""
    try:
        return (float(ctx["dest_lat"]), float(ctx["dest_lon"]),
                ctx.get("destination") or "your destination")
    except (KeyError, TypeError, ValueError):
        home = places.home()
        return home["lat"], home["lon"], home["label"]


def _weather_for_trip(lat: float, lon: float, now: datetime, eta_min: int,
                     area: str) -> dict:
    """Forecast covering the hours the trip actually runs in.

    Two constraints make the window non-obvious: the forecast only returns
    steps from now forward, so the window must look ahead rather than behind,
    and the steps are 3 hours apart, so a window as short as a real drive can
    fall between two of them and match nothing.
    """
    span = max(3, (eta_min // 60) + 1)
    forecast = mock_data.get_weather_at(
        lat, lon, now.strftime("%Y-%m-%d"), now.hour, min(now.hour + span, 23), area
    )
    if not forecast.get("error") and forecast.get("temp_f") is None and now.hour + span > 23:
        # Trip runs past midnight; the step covering it belongs to tomorrow.
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        forecast = mock_data.get_weather_at(
            lat, lon, tomorrow, 0, (now.hour + span) - 24, area
        )
    return forecast


def _live_conditions(ctx: dict) -> tuple[dict, dict]:
    """Live weather + news for the trip about to start, fetched in parallel.

    Never raises and never blocks the brief for long: each source already has
    its own request timeout and returns a dict carrying an `error` key when it
    cannot answer, which the prompt renders as "unavailable".
    """
    now = _local_now()
    date = now.strftime("%Y-%m-%d")
    # A trip planned for later ("leave at 5:30", "arrive by 9") gets the
    # forecast for when it actually runs, not for right now.
    if ctx.get("depart_at"):
        try:
            planned = datetime.fromisoformat(ctx["depart_at"])
            if planned > now:
                now = planned
        except ValueError:
            pass
    try:
        eta_min = int(float(ctx.get("eta_min") or 0))
    except (TypeError, ValueError):
        eta_min = 0

    lat, lon, area = _weather_point(ctx)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            w = pool.submit(_weather_for_trip, lat, lon, now, eta_min, area)
            n = pool.submit(mock_data.get_news_for_area, _BRIEF_NEWS_AREA, date)
            return w.result(timeout=15), n.result(timeout=15)
    except Exception as exc:
        note = f"live conditions unavailable: {exc}"
        return {"error": note, "alerts": []}, {"error": note, "articles": []}


@app.route("/api/brief", methods=["POST"])
def brief():
    """Pre-trip alert for the current route. Uses Claude when ANTHROPIC_API_KEY
    is set; otherwise returns a rule-based fallback so the UI always works.

    The frontend sends the concrete trip (destination, ETA, on-route
    disruptions, parking); the server adds live weather and news for the trip
    window before reasoning over the whole picture."""
    ctx = request.get_json(silent=True) or {}
    # The map sends the raw transit plan; describe it with the same words the
    # emailed alert uses.
    if ctx.get("transit_plan"):
        ctx["transit"] = alerts.transit_summary(ctx.pop("transit_plan"))
    return jsonify(compute_brief(ctx))


def compute_brief(ctx: dict) -> dict:
    """Enrich a trip with live conditions and reason over it.

    Shared by the map (POST /api/brief) and the scheduled-alert job, so an
    emailed alert and the on-screen one are produced by the same code.
    """
    ctx["weather"], ctx["news"] = _live_conditions(ctx)
    if api_key_present():
        try:
            return {"source": "ai", "alert": _parse_alert(agent.trip_brief_text(ctx))}
        except Exception as exc:
            # Never break the caller on an API hiccup — fall back.
            return {"source": "fallback", "note": str(exc), "alert": _fallback_brief(ctx)}
    return {"source": "fallback", "alert": _fallback_brief(ctx)}


def _as_sentence(text: str) -> str:
    """Close a fragment with a period so the next sentence can be appended.

    Disruption reasons come from the data layer and don't reliably end in
    punctuation, which ran them into the sentence that followed.
    """
    text = (text or "").strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _fallback_brief(ctx: dict) -> dict:
    """Deterministic brief from the assembled trip, no model call."""
    # Only what is likely to affect the trip scores. Red is on the route; a
    # yellow is a note — off the route, a blocked sidewalk, or a long-term work
    # zone with no hours — and never raises the risk on its own, because "no
    # risk" has to mean no risk. The one yellow that counts is a big event's
    # crowd, which spills well past its venue.
    disruptions = ctx.get("disruptions") or []
    reds = [d for d in disruptions if d.get("severity") == "red"]
    crowds = [d for d in disruptions if d.get("severity") == "yellow" and d.get("type") == "event"]
    notes = [d for d in disruptions if d.get("severity") != "red" and d not in crowds]
    dest = ctx.get("destination", "your destination")
    mode = alerts.normalize_mode(ctx.get("mode"))
    trip_word = {"drive": "drive", "walk": "walk", "bike": "ride", "transit": "trip"}[mode]

    if reds:
        risk = "high"
        headline = f"{reds[0]['name']} is likely to affect your trip to {dest}."
        why = f"{len(reds)} disruption(s) sit on your route, including {reds[0]['name']}. " + _as_sentence(reds[0].get("reason", ""))
        rec = "Consider leaving earlier or taking an alternate route."
    elif crowds:
        risk = "low"
        headline = f"{crowds[0]['name']} is near your route to {dest}."
        why = _as_sentence(crowds[0].get("reason", ""))
        rec = "Allow a few extra minutes around the venue."
    else:
        risk = "none"
        headline = f"Clear {trip_word} to {dest}."
        why = "Nothing on your route is expected to slow you down."
        rec = "No action needed."
    if notes and not reds:
        more = f" and {len(notes) - 1} more" if len(notes) > 1 else ""
        why = _as_sentence(why) + f" Noted, but not expected to affect you: {notes[0]['name']}{more}."

    parking = ctx.get("parking") or []
    if parking:
        why = _as_sentence(why) + f" Metered parking is available near the destination (e.g. {parking[0].get('name')})."

    # Weather only moves the needle when the forecast raised a real alert (rain
    # likely or strong wind). News relevance is a judgment call, so the
    # rule-based path leaves headlines to the model rather than guessing.
    weather_alerts = (ctx.get("weather") or {}).get("alerts") or []
    if weather_alerts:
        why = _as_sentence(why) + " " + " ".join(weather_alerts)
        if risk == "none":
            # On foot or on a bike, rain or wind is the trip, not a footnote.
            risk = "medium" if mode in ("walk", "bike") else "low"
            headline = f"Clear route to {dest}, but check the weather."
        if rec == "No action needed.":
            rec = {"drive": "Allow a little extra time for the conditions.",
                   "transit": "Bring an umbrella for the walk to and from the stops."
                   }.get(mode, "Bring a rain layer, or consider driving instead.")
    # Traffic against what's usual for that hour: ten minutes or more is a
    # real problem, five is worth a word. Leave-by comes from the same route.
    delta = ctx.get("traffic_delta_min")
    if mode == "drive" and delta is not None and delta >= 5:
        why = _as_sentence(why) + f" Traffic is {delta} min slower than usual for this time."
        if delta >= 10:
            risk = "high" if risk == "high" else "medium"
            if risk != "high":
                headline = f"Heavy traffic on the way to {dest}: about {delta} min slower than usual."
            rec = (f"Leave by {ctx['leave_by_text']}." if ctx.get("leave_by_text")
                   else f"Leave about {delta} min earlier than usual.")
        elif risk == "none":
            risk = "low"
    if mode == "drive" and ctx.get("leave_by_text") and rec == "No action needed.":
        rec = f"Leave by {ctx['leave_by_text']}."

    # Live transit trouble: a cancelled ride or a missed connection is a real
    # threat to the trip; a long delay or a service alert is worth a mention.
    plan = ctx.get("transit") or {}
    live = plan.get("live") or []
    if live:
        serious = any(("cancelled" in l) or ("miss the" in l) for l in live)
        if serious:
            risk = "high" if risk == "high" else "medium"
            headline = live[0] + "."
            rec = "Take the next option, or leave earlier."
        elif risk == "none":
            risk = "low"
        why = _as_sentence(why) + " " + " ".join(_as_sentence(l) for l in live[:3])
    # For transit the plan itself is the most useful line: when to leave and
    # what to board.
    if plan.get("leave_text") and rec == "No action needed.":
        first_ride = next((s for s in plan.get("steps") or [] if s.startswith("Take")), None)
        rec = f"Leave by {plan['leave_text']}." + (f" {first_ride}." if first_ride else "")
    return {"risk": risk, "headline": headline, "why": why, "recommendation": rec, "raw": None}


# ---------------------------------------------------------------------------
# Scheduler access
#
# The map is a public demo; the schedule is personal — where you go and when.
# So the gate sits on the scheduler endpoints only, and the map stays open.
#
# One shared passcode, no accounts: there is one user, and a login system would
# be more surface than the thing it protects. The passcode travels in a custom
# header, which cannot be sent cross-origin without CORS, so this needs no CSRF
# token. With `SCHEDULE_PASSCODE` unset the endpoints stay open, which keeps
# local development frictionless — it must therefore be set in production.
# ---------------------------------------------------------------------------

PASSCODE_HEADER = "X-Schedule-Passcode"
_MAX_FAILURES = 10
_FAILURE_WINDOW_S = 900          # 15 minutes
_failures: dict[str, list] = {}  # ip -> [count, window_started_at]


def _schedule_passcode() -> str:
    # Read per call rather than at import, so tests and a restart pick up a change.
    return os.environ.get("SCHEDULE_PASSCODE", "")


def _client_ip() -> str:
    # Render sits behind a proxy, so the real client is first in the chain.
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "unknown"


def _throttled(ip: str) -> bool:
    """A short shared passcode is guessable without a limit on attempts."""
    entry = _failures.get(ip)
    if not entry:
        return False
    count, started = entry
    if time.time() - started > _FAILURE_WINDOW_S:
        _failures.pop(ip, None)
        return False
    return count >= _MAX_FAILURES


def _record_failure(ip: str) -> None:
    entry = _failures.get(ip)
    if not entry or time.time() - entry[1] > _FAILURE_WINDOW_S:
        _failures[ip] = [1, time.time()]
    else:
        entry[0] += 1


def _passcode_ok() -> bool:
    expected = _schedule_passcode()
    if not expected:
        return True
    supplied = request.headers.get(PASSCODE_HEADER, "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def requires_passcode(view):
    """Gate a scheduler endpoint behind the shared passcode."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _schedule_passcode():
            return view(*args, **kwargs)
        ip = _client_ip()
        if _throttled(ip):
            return jsonify({"error": "too many attempts; try again later"}), 429
        if not _passcode_ok():
            _record_failure(ip)
            # `locked` tells the UI to ask for the passcode rather than show an error.
            return jsonify({"error": "locked", "locked": True}), 401
        _failures.pop(ip, None)
        return view(*args, **kwargs)
    return wrapper


@app.route("/api/schedule/unlock", methods=["POST"])
def unlock_schedule():
    """Check a passcode without changing anything, so the UI can validate it."""
    if not _schedule_passcode():
        return jsonify({"unlocked": True, "required": False})
    ip = _client_ip()
    if _throttled(ip):
        return jsonify({"error": "too many attempts; try again later"}), 429
    if not _passcode_ok():
        _record_failure(ip)
        return jsonify({"error": "incorrect passcode", "locked": True}), 401
    _failures.pop(ip, None)
    return jsonify({"unlocked": True, "required": True})


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

@app.route("/api/schedule")
@requires_passcode
def get_schedule():
    """Everything the scheduler panel needs in one call."""
    url = store.get_setting(scheduler.ICS_URL_KEY)
    return jsonify({
        "trips": scheduler.upcoming(),
        "all": store.list_trips(),
        "calendar": {
            "connected": bool(url),
            # The feed URL is a bearer credential; the browser gets a shape, not the secret.
            "hint": (url.split("/")[2] if url and "/" in url else None),
        },
        "alert_email": store.get_setting(scheduler.ALERT_EMAIL_KEY),
    })


@app.route("/api/schedule", methods=["POST"])
@requires_passcode
def add_schedule():
    """Add a manual trip: either a one-off `arrive_at` or `days` + `time_of_day`."""
    body = request.get_json(silent=True) or {}
    try:
        lat, lon = float(body["dest_lat"]), float(body["dest_lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "dest_lat and dest_lon are required"}), 400
    if not (body.get("arrive_at") or (body.get("days") and body.get("time_of_day"))):
        return jsonify({"error": "need arrive_at, or days plus time_of_day"}), 400

    trip = store.save_trip({
        "label": (body.get("label") or body.get("destination") or "Trip").strip()[:120],
        "destination": (body.get("destination") or "").strip()[:250],
        "dest_lat": lat, "dest_lon": lon,
        "arrive_at": body.get("arrive_at"),
        "days": body.get("days"),
        "time_of_day": body.get("time_of_day"),
        "source": "manual",
        "mode": alerts.normalize_mode(body.get("mode")),
    })
    return jsonify({"trip": trip}), 201


@app.route("/api/schedule/<trip_id>", methods=["DELETE"])
@requires_passcode
def remove_schedule(trip_id):
    return jsonify({"deleted": store.delete_trip(trip_id)})


@app.route("/api/schedule/calendar", methods=["POST"])
@requires_passcode
def connect_calendar():
    """Connect, re-sync, or disconnect an .ics feed.

    Sending url: null disconnects and clears everything that came from it, so
    a disconnect leaves no orphaned calendar trips behind.
    """
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        store.set_setting(scheduler.ICS_URL_KEY, None)
        removed = store.delete_by_source("ics")
        return jsonify({"connected": False, "removed": removed})
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "that does not look like a calendar URL"}), 400

    # Verify before saving, so a bad URL never becomes stored state.
    if calendar_sync.fetch_ics(url) is None:
        return jsonify({"error": "could not fetch or parse that calendar feed"}), 400
    store.set_setting(scheduler.ICS_URL_KEY, url)
    return jsonify({"connected": True, "sync": scheduler.sync_calendar()})


@app.route("/api/schedule/sync", methods=["POST"])
@requires_passcode
def sync_calendar_now():
    result = scheduler.sync_calendar()
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/schedule/settings", methods=["POST"])
@requires_passcode
def schedule_settings():
    body = request.get_json(silent=True) or {}
    if "alert_email" in body:
        email = (body.get("alert_email") or "").strip()
        store.set_setting(scheduler.ALERT_EMAIL_KEY, email or None)
    return jsonify({"alert_email": store.get_setting(scheduler.ALERT_EMAIL_KEY)})


@app.route("/api/events")
def get_events():
    """Big events whose busy window overlaps `from`..`to` (Pacific ISO times;
    default: the next three hours), each with a plain-English `reason`."""
    now = datetime.now()
    try:
        start = datetime.fromisoformat(request.args["from"]) if request.args.get("from") else now
        end = datetime.fromisoformat(request.args["to"]) if request.args.get("to") else start + timedelta(hours=3)
    except ValueError:
        return jsonify({"events": [], "error": "from/to must look like 2026-10-20T18:30"}), 400
    found = [{**e, "reason": events.describe(e)} for e in events.during(start, end)]
    return jsonify({"events": found, **events.status()})


@app.route("/api/places")
@requires_passcode
def get_places():
    """Your home and saved places. Gated: home is the most personal thing in
    the app, and the map around it is a public demo."""
    return jsonify(places.get_places())


@app.route("/api/places", methods=["PUT"])
@requires_passcode
def put_places():
    """Replace home and saved places in one go; invalid entries are dropped."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected {home, saved}"}), 400
    return jsonify(places.save_places(body))


@app.route("/api/push/subscribe", methods=["POST"])
@requires_passcode
def push_subscribe():
    body = request.get_json(silent=True) or {}
    if not push.add(body.get("subscription"), body.get("label") or ""):
        return jsonify({"error": "not a push subscription from a known push service"}), 400
    return jsonify({"devices": len(push.subscriptions())})


@app.route("/api/push/unsubscribe", methods=["POST"])
@requires_passcode
def push_unsubscribe():
    body = request.get_json(silent=True) or {}
    push.remove(str(body.get("endpoint") or ""))
    return jsonify({"devices": len(push.subscriptions())})


@app.route("/api/push/test", methods=["POST"])
@requires_passcode
def push_test():
    """Notify every device now, and say exactly what happened."""
    result = push.send({"title": "Bay Area GPS", "body": "Notifications work. Trip alerts will arrive here.",
                        "url": "/", "tag": "test"})
    return jsonify(result), (200 if result["sent"] else 400)


@app.route("/api/schedule/test-email", methods=["POST"])
@requires_passcode
def send_test_email():
    """Send one fixed email to the alert address and report exactly what happened.

    The nightly job only emails when a trip is due, so without this the first
    sign that delivery is broken is an alert that never arrives.
    """
    email = store.get_setting(scheduler.ALERT_EMAIL_KEY)
    delivery = alerts.send_email(
        email, "Test alert from Bay Area Predictive GPS",
        "This is a test. Pre-trip alerts for your scheduled trips will arrive at this address.")
    return jsonify(delivery), (200 if delivery.get("sent") else 400)


@app.route("/api/alerts/run", methods=["POST"])
def run_alerts():
    """Brief every trip starting soon. Called by a scheduler, not a browser.

    Protected by `CRON_SECRET` when set. Each occurrence is alerted once —
    the send is recorded per trip and start time, so a cron that fires twice
    (or retries) does not notify twice. Sends a push to every device with
    notifications on, and email when that's configured.
    """
    if not _cron_ok():
        return jsonify({"error": "unauthorized"}), 401

    hours = request.args.get("within_hours", 24, type=float)
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    email = store.get_setting(scheduler.ALERT_EMAIL_KEY)
    results = []

    for trip in scheduler.upcoming(within_hours=hours):
        if store.get_setting(_alert_marker(trip)):
            results.append({"trip": trip["label"], "skipped": "already alerted"})
            continue
        results.append(_alert_trip(trip, email, dry_run))

    return jsonify({"checked_within_hours": hours, "alerts": results})


def _cron_ok() -> bool:
    """CRON_SECRET in X-Cron-Secret, or — so a run can be started by hand from
    the UI — the schedule passcode. Open when CRON_SECRET isn't set (local)."""
    secret = os.environ.get("CRON_SECRET")
    if not secret:
        return True
    from_cron = hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), secret)
    return from_cron or bool(_schedule_passcode() and _passcode_ok())


def _alert_marker(trip: dict) -> str:
    """Recorded once an occurrence's alert is delivered, so no run sends it twice."""
    return f"alerted:{trip['id']}:{trip['next_at']}"


def _alert_trip(trip: dict, email: str | None, dry_run: bool = False) -> dict:
    """Route, brief and deliver one trip occurrence. Marks it alerted only
    when something actually reached you."""
    context = alerts.build_context(trip)
    if not context:
        return {"trip": trip["label"], "error": "could not build a route"}

    brief = compute_brief(context)
    alert = brief.get("alert") or {}
    subject, body = alerts.format_email(trip, alert, context)
    outcome = {"trip": trip["label"], "next_at": trip["next_at"],
               "risk": alert.get("risk"), "source": brief.get("source"),
               "subject": subject}
    notification = alerts.push_payload(trip, alert, context)
    if dry_run:
        outcome["body"] = body
        outcome["push"] = notification
        return outcome
    # Every channel that's set up: a push to each device, and email.
    # Delivered by either counts — the alert reached you.
    pushed = push.send(notification) if push.configured() else None
    emailed = alerts.send_email(email, subject, body) if (email and os.environ.get("RESEND_API_KEY")) else None
    sent = bool((pushed and pushed["sent"]) or (emailed and emailed.get("sent")))
    outcome["delivery"] = {"sent": sent, "push": pushed, "email": emailed}
    if not sent:
        outcome["delivery"]["reason"] = (
            (pushed or {}).get("reason") or (emailed or {}).get("reason")
            or "no channel set up: turn on notifications or add an alert email")
    else:
        store.set_setting(_alert_marker(trip), datetime.utcnow().isoformat(timespec="seconds"))
    return outcome


# ---------------------------------------------------------------------------
# The clock
#
# An outside scheduler (cron-job.org, free) POSTs /api/tick every 10 minutes.
# GitHub Actions cron, used before, started hours late and turns itself off
# after 60 quiet days; a frequent, punctual tick is what lets an alert go out
# at a chosen time instead of whenever a nightly job happens to run. Every
# tick is cheap when there's nothing to do: a few reads from the settings
# table. The pings also keep the free Render service and Supabase awake.
#
# The reply is counts only, never trip names: the scheduler keeps a history
# of responses on a third-party site.
# ---------------------------------------------------------------------------

SYNC_EVERY = timedelta(hours=6)          # re-read the connected calendar
RETRY_EVERY = timedelta(hours=1)         # an alert that failed to deliver
LAST_TICK_KEY = "last_tick"
SYNC_TRIED_KEY = "calendar_auto_sync_at"


def _when(stamp: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


@app.route("/api/tick", methods=["POST"])
def tick():
    """Do whatever is due: re-sync the calendar every few hours, and send each
    trip's alert at its time (`alerts.alert_due_at`). Safe to call as often as
    you like — each occurrence is alerted once, and a failed delivery is
    retried at most hourly rather than every tick."""
    if not _cron_ok():
        return jsonify({"error": "unauthorized"}), 401
    now = scheduler.local_now()
    result = {"at": now.isoformat(timespec="minutes"), "calendar_synced": None,
              "sent": 0, "failed": 0, "waiting": 0}

    if store.get_setting(scheduler.ICS_URL_KEY):
        tried = _when(store.get_setting(SYNC_TRIED_KEY))
        if not tried or now - tried >= SYNC_EVERY:
            # Recorded before trying, so a dead feed is retried every few
            # hours rather than on every tick.
            store.set_setting(SYNC_TRIED_KEY, now.isoformat(timespec="minutes"))
            result["calendar_synced"] = bool(scheduler.sync_calendar().get("ok"))

    email = store.get_setting(scheduler.ALERT_EMAIL_KEY)
    for trip in scheduler.upcoming(within_hours=24, now=now):
        if now < alerts.alert_due_at(datetime.fromisoformat(trip["next_at"])):
            result["waiting"] += 1
            continue
        if store.get_setting(_alert_marker(trip)):
            continue
        attempt = f"alert_try:{trip['id']}:{trip['next_at']}"
        tried = _when(store.get_setting(attempt))
        if tried and now - tried < RETRY_EVERY:
            continue
        store.set_setting(attempt, now.isoformat(timespec="minutes"))
        outcome = _alert_trip(trip, email)
        result["sent" if (outcome.get("delivery") or {}).get("sent") else "failed"] += 1

    store.set_setting(LAST_TICK_KEY, json.dumps(result))
    return jsonify(result)


@app.route("/map-test")
def map_test():
    """Throwaway page to verify the Mapbox token and dark style render."""
    return render_template("map_test.html", mapbox_token=os.environ.get("MAPBOX_TOKEN", ""))


@app.route("/api/analyze", methods=["POST"])
def analyze():
    body = request.get_json(silent=True) or {}
    route_id = body.get("route_id", "").strip()
    date = body.get("date", "").strip()

    if not route_id or not date:
        return jsonify({"error": "route_id and date are required"}), 400

    try:
        routine = mock_data.get_routine(route_id)
    except KeyError:
        return jsonify({"error": f"unknown route_id: {route_id}"}), 404

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400

    if api_key_present():
        return jsonify(_full_run(routine, date))
    else:
        return jsonify(_dry_run(route_id, date))


def _full_run(routine: dict, date: str) -> dict:
    agent = RouteIntelligenceAgent(verbose=False)
    alert_text = agent.analyze(routine, date)
    return {"mode": "agent", "route_id": routine["route_id"], "date": date, "alert": _parse_alert(alert_text)}


def _dry_run(route_id: str, date: str) -> dict:
    return {
        "mode": "dry_run",
        "note": "ANTHROPIC_API_KEY not set — returning raw tool data only.",
        "route_id": route_id,
        "date": date,
        "data": {
            "events": tools.run_tool("get_events_near_route", {"route_id": route_id, "date": date}),
            "weather": tools.run_tool("get_weather_forecast", {"route_id": route_id, "date": date}),
            "traffic_baseline": tools.run_tool("get_traffic_baseline", {"route_id": route_id}),
            "news": tools.run_tool("get_local_news", {"route_id": route_id, "date": date}),
        },
    }


def _parse_alert(text: str) -> dict:
    """Pull the structured fields out of the agent's final text block."""
    fields = {"risk": None, "headline": None, "why": None, "recommendation": None, "raw": text}
    patterns = {
        "risk": r"RISK:\s*(.+)",
        "headline": r"HEADLINE:\s*(.+)",
        "why": r"WHY:\s*([\s\S]+?)(?=RECOMMENDATION:|$)",
        "recommendation": r"RECOMMENDATION:\s*([\s\S]+?)$",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            fields[key] = match.group(1).strip()
    return fields


if __name__ == "__main__":
    # macOS AirPlay Receiver listens on 5000, so the port is overridable.
    # debug=True also keeps Jinja reloading templates; without it, edits to
    # index.html are invisible until a restart.
    app.run(debug=True, port=int(os.environ.get("PORT", "5050")))
