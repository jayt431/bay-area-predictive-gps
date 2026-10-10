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

import hmac
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, render_template, request

import alerts
import calendar_sync
import transit
import mock_data
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
        # The date the bundled Muni/BART/Caltrain schedules stop covering.
        # Past it, transit answers "expired" until gtfs_build.py is rerun.
        "transit_data_until": transit.data_until(),
        # "sqlite" in production means scheduled trips vanish on restart.
        "schedule_storage": "postgres" if store.using_postgres() else "sqlite",
        # False in production means anyone can read and edit the schedule.
        "schedule_locked": bool(os.environ.get("SCHEDULE_PASSCODE")),
        "cron_protected": bool(os.environ.get("CRON_SECRET")),
        "email_configured": bool(os.environ.get("RESEND_API_KEY")),
    })


@app.route("/api/transit")
def transit_directions():
    """Direct-ride transit options from home to a destination, step by step,
    planned over the bundled 511 schedules (Muni, BART, Caltrain)."""
    try:
        lat, lon = float(request.args["dest_lat"]), float(request.args["dest_lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"routes": [], "error": "dest_lat and dest_lon are required"}), 400
    home = mock_data.HOME
    return jsonify(transit.plan((home["lat"], home["lon"]), (lat, lon)))


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


@app.route("/api/parking")
def parking():
    """Mocked parking zones around a destination (lat/lon query params)."""
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "numeric lat and lon query params are required"}), 400
    return jsonify({"zones": mock_data.get_parking(lat, lon)})


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
        home = mock_data.HOME
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
    disruptions = ctx.get("disruptions") or []
    reds = [d for d in disruptions if d.get("severity") == "red"]
    yellows = [d for d in disruptions if d.get("severity") == "yellow"]
    dest = ctx.get("destination", "your destination")
    mode = alerts.normalize_mode(ctx.get("mode"))
    trip_word = {"drive": "drive", "walk": "walk", "bike": "ride", "transit": "trip"}[mode]

    if reds:
        risk = "high"
        headline = f"{reds[0]['name']} is likely to affect your trip to {dest}."
        why = f"{len(reds)} disruption(s) sit on your route, including {reds[0]['name']}. " + _as_sentence(reds[0].get("reason", ""))
        rec = "Consider leaving earlier or taking an alternate route."
    elif yellows:
        risk = "low"
        headline = f"Minor possible friction on the way to {dest}."
        why = f"{len(yellows)} item(s) are near your route but may not affect it, e.g. {yellows[0]['name']}."
        rec = "No action needed, but keep an eye out near the flagged area."
    else:
        risk = "none"
        headline = f"Clear {trip_word} to {dest}."
        why = "No disruptions were detected on your route."
        rec = "No action needed."

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
    # For transit the plan itself is the most useful line: when to leave and
    # what to board.
    plan = ctx.get("transit") or {}
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
    (or retries) does not email twice.
    """
    secret = os.environ.get("CRON_SECRET")
    if secret:
        from_cron = hmac.compare_digest(request.headers.get("X-Cron-Secret", ""), secret)
        # The passcode also opens this, so the UI can trigger a run by hand.
        if not (from_cron or (_schedule_passcode() and _passcode_ok())):
            return jsonify({"error": "unauthorized"}), 401

    hours = request.args.get("within_hours", 24, type=float)
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    email = store.get_setting(scheduler.ALERT_EMAIL_KEY)
    results = []

    for trip in scheduler.upcoming(within_hours=hours):
        marker = f"alerted:{trip['id']}:{trip['next_at']}"
        if store.get_setting(marker):
            results.append({"trip": trip["label"], "skipped": "already alerted"})
            continue

        context = alerts.build_context(trip)
        if not context:
            results.append({"trip": trip["label"], "error": "could not build a route"})
            continue

        brief = compute_brief(context)
        alert = brief.get("alert") or {}
        subject, body = alerts.format_email(trip, alert, context)
        outcome = {"trip": trip["label"], "next_at": trip["next_at"],
                   "risk": alert.get("risk"), "source": brief.get("source"),
                   "subject": subject}
        if dry_run:
            outcome["body"] = body
        else:
            outcome["delivery"] = alerts.send_email(email, subject, body)
            if outcome["delivery"].get("sent"):
                store.set_setting(marker, datetime.utcnow().isoformat(timespec="seconds"))
        results.append(outcome)

    return jsonify({"checked_within_hours": hours, "alerts": results})


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
