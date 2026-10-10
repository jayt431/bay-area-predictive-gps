# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## PROJECT-NOTES.md

`PROJECT-NOTES.md` is the plain-English companion to this file — written for
Jaret rather than for an agent, so he can upload it to a Claude conversation and
talk through the project to learn it and explain it to other people. It covers
what the system does, how a trip flows through it, what is real versus mocked,
every problem diagnosed and how, the decisions and their trade-offs, a
vocabulary section, and answers to questions someone might ask.

**Keep it current.** When a meaningful change lands — a feature, a source going
from mocked to live, a bug diagnosed, an open item closing — update
PROJECT-NOTES.md in the same session, while the reasoning is fresh, and bump its
"Last updated" line. It is not a changelog; it is an explanation.

## Commands

```bash
# One-time setup. The system Python has none of these, and on Apple Silicon a
# stray x86 anaconda on PATH will not execute at all — use this venv (gitignored).
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Web app (the main product — map GPS UI), at http://127.0.0.1:5050
set -a && . ./.env && set +a && ./venv/bin/python app.py

# Standalone agent scenarios (the original CLI harness)
set -a && . ./.env && set +a && ./venv/bin/python run_scenarios.py --dry-run
set -a && . ./.env && set +a && ./venv/bin/python run_scenarios.py   # needs ANTHROPIC_API_KEY

# Rebuild the transit schedules (Muni, BART, Caltrain) from 511 and verify.
# Normally automatic: .github/workflows/transit-schedules.yml does this nightly.
set -a && . ./.env && set +a && ./venv/bin/python gtfs_build.py
./venv/bin/python gtfs_build.py --check

# Production (Render) runs: gunicorn app:app   (see Procfile)
```

Two things about that command line, both of which cost real time to discover:

- **`set -a` is not optional.** `.env` holds bare `KEY=value` lines with no
  `export`, so a plain `source .env` creates shell variables that the Python
  process never sees. The app then starts with no keys and the map reports no
  Mapbox token. `set -a` marks subsequent assignments for export; `set +a`
  stops.
- **The dev port is 5050, not 5000.** macOS AirPlay Receiver (ControlCenter)
  holds 5000. Override with `PORT=xxxx`.

`debug=True` in the dev entrypoint also keeps Jinja reloading templates. Without
it, edits to `index.html` are served from a copy compiled at startup and appear
to have no effect.
On Render, the same vars are set in the dashboard's Environment tab.

Env vars:
- `MAPBOX_TOKEN` — required for the map UI (public `pk.` token; URL-restricted to
  the onrender.com domain on the live site)
- `ANTHROPIC_API_KEY` — enables the real AI trip brief; without it the brief and
  agent fall back gracefully
- `OPENWEATHER_API_KEY` — live weather (free tier)
- `NEWSAPI_KEY` — live Bay Area news (free developer tier)
- `TRAFFIC_511_TOKEN` — live 511 SF Bay traffic events; without it the
  disruption pool falls back to the mock list in `mock_data._DISRUPTIONS`
- `DATABASE_URL` — Postgres for the scheduler. Absent, it uses a local SQLite
  file (`SCHEDULE_DB_PATH`, default `schedule.db`), so the scheduler runs
  locally with no signup
- `RESEND_API_KEY` / `ALERT_FROM_EMAIL` — sends the pre-trip alert email;
  without a key the alert is computed and reported, not sent
- `CRON_SECRET` — required in the `X-Cron-Secret` header on
  `POST /api/alerts/run` when set
- `SCHEDULE_PASSCODE` — gates the scheduler endpoints. **Unset means open**,
  which keeps local development frictionless and makes setting it in production
  mandatory

Every source degrades gracefully; a missing key downgrades one feature, never
breaks the app. **After each meaningful change: commit + push** (backs up to
GitHub and auto-deploys to Render).

## The web app (primary product)

`app.py` (Flask) serves the map GPS at `/` from `templates/index.html`, a single
self-contained page using Mapbox GL JS + Turf (both CDN). See the Phase 5 notes
below for the full UX. It is a **single-user** model (fixed home base), distinct
from the three personas the CLI agent still uses.

Endpoints:
| Route | Purpose |
|-------|---------|
| `GET /` | The map GPS UI |
| `GET /api/config` | Home base (`mock_data.HOME`) |
| `GET /api/disruptions` | Bay Area disruption pool. Live 511 SF Bay when `TRAFFIC_511_TOKEN` is set, else mocked |
| `GET /api/parking?lat&lon` | Mocked parking zones (legacy; UI now uses meters) |
| `GET /api/meters?lat&lon` | **Real** SF metered streets (DataSF), grouped by street |
| `GET /api/transit?dest_lat&dest_lon` | Direct-ride transit options from home, step by step (own planner over 511 GTFS). `{routes, error?}` |
| `GET /api/schedule` | Upcoming trips, calendar status, alert email |
| `POST /api/schedule` | Add a manual trip (one-off `arrive_at`, or `days` + `time_of_day`) |
| `DELETE /api/schedule/<id>` | Remove a trip |
| `POST /api/schedule/calendar` | Connect an .ics feed and sync; `url: null` disconnects and clears its trips |
| `POST /api/schedule/sync` | Re-sync the connected calendar |
| `POST /api/schedule/settings` | Set the alert email |
| `POST /api/schedule/unlock` | Validate a passcode without changing anything |
| `POST /api/schedule/test-email` | Send one fixed email to the alert address and return Resend's exact result |
| `POST /api/alerts/run` | Brief every trip starting soon and email it. `?dry_run=1` returns the emails instead of sending |
| `POST /api/brief` | Pre-trip alert. Claude when `ANTHROPIC_API_KEY` set, else rule-based `_fallback_brief` |
| `GET /map-test` | Standalone Mapbox pin test (persona data) |

**AI trip brief:** the frontend assembles the concrete trip (destination, ETA,
on-route disruptions, parking) and POSTs it to `/api/brief`. The server then
adds the two live sources — weather and news — via `_live_conditions()`, fetched
in parallel, before reasoning. With a key, `agent.trip_brief_text()` makes one
Claude call (no tool loop — data is already gathered) and returns a
`RISK/HEADLINE/WHY/RECOMMENDATION` alert; without a key, `_fallback_brief()`
derives the same shape by rule. The UI tag reads "◆ AI brief" vs "◆ predicted"
accordingly.

The forecast is taken at the **destination** (`dest_lat`/`dest_lon`, sent by
the frontend with the trip), not the home base — Bay Area weather is local
enough that the two genuinely differ over a cross-city trip. A client that
sends no destination coordinates falls back to `HOME`.

Two details in `_weather_for_trip()` are easy to get wrong: the forecast only
returns steps from now forward, so the window must look ahead rather than
behind, and the steps are 3 hours apart, so the window is widened to at least
3 hours or a short drive falls between two steps and matches nothing. News
relevance is left entirely to the model — the prompt hands it raw headlines and
says most will be irrelevant. The rule-based fallback uses only the structured
weather alerts (rain likely, strong wind) and ignores headlines, because
relevance is a judgment it cannot make.

## Where each secret has to be set

Values live in a password manager, never here. This table is the map of
**locations**, because the thing that actually breaks is rotating a value and
missing one of the places it lives. Those failures are silent: a dead
`TRAFFIC_511_TOKEN` in Render doesn't error, the disruption pool just quietly
falls back to the mock list — the same shape of invisible outage as the DataSF
domain migration, which left parking looking like a dead button for weeks.

| Variable | `.env` (local) | Render (prod) | Elsewhere |
|----------|----------------|---------------|-----------|
| `MAPBOX_TOKEN` | yes | yes | URL restriction set in the Mapbox dashboard |
| `OPENWEATHER_API_KEY` | yes | yes | — |
| `NEWSAPI_KEY` | yes | yes | — |
| `TRAFFIC_511_TOKEN` | yes | yes | **also** a GitHub Actions repo secret, for the nightly schedule refresh |
| `DATABASE_URL` | normally commented out | yes | value comes from Supabase → Connect → **Session pooler** |
| `SCHEDULE_PASSCODE` | no, on purpose | **yes — required** | the passcode typed into the Schedule panel |
| `CRON_SECRET` | no | yes | **also** a GitHub Actions repo secret, must match |
| `APP_URL` | no | no | GitHub Actions repo secret only |
| `RESEND_API_KEY`, `ALERT_FROM_EMAIL` | no | **not yet set** — `/api/health` reports `email_configured: false` | the Resend account; with the default `onboarding@resend.dev` sender it only delivers to the account owner's address |
| `ANTHROPIC_API_KEY` | deliberately unset | deliberately unset | deferred on cost; the rule-based brief is the intended behaviour |

Two deliberate asymmetries, both easy to mistake for mistakes:

- **`SCHEDULE_PASSCODE` is unset locally** so development needs no passcode.
  That is also why it is *mandatory* in production — unset means open.
- **`DATABASE_URL` is commented out locally** so local runs use a SQLite file
  instead of writing test data into the live Supabase database. Uncomment it
  only to work against real data.

Optional overrides with working defaults in code: `SCHEDULE_DB_PATH`,
`GEOCODE_BBOX`, `GEOCODE_PROXIMITY`, `GEOCODE_COUNTRY`,
`GEOCODE_MIN_RELEVANCE`.

Git authentication is an SSH key (`~/.ssh/id_ed25519`), not a token, so no
credential is stored in `.git/config`.

## The scheduler

Three files, added when the scheduler arrived because a background job must
read the schedule while the browser is closed — the database trigger this doc
predicted:

| File | Role |
|------|------|
| `store.py` | Persistence. Postgres via `DATABASE_URL`, else SQLite. Rows carry a `user_id` (currently `'local'`) so multi-user is a migration, not a rewrite. One connection per request, not per call — see below |
| `calendar_sync.py` | Fetches and parses .ics feeds, expands RRULE, geocodes locations |
| `scheduler.py` | Calendar import and "what trips are coming up" |
| `alerts.py` | Routes and briefs a trip server-side, then emails it |

**The schedule is gated, the map is not.** The map is a public demo; the
schedule is personal — where you go and when. So `requires_passcode` sits on
the seven `/api/schedule*` endpoints only, and `/`, `/api/config`,
`/api/disruptions` and the rest stay open.

One shared passcode, no accounts: there is one user, and a login system would
be more surface than the thing it protects. The passcode travels in an
`X-Schedule-Passcode` header, which cannot be sent cross-origin without CORS,
so there is no CSRF surface and no server-side session. The browser keeps it in
`localStorage`; a 401 re-shows the lock panel. Ten wrong attempts from one IP
start a 15-minute cooldown, because a short shared secret is otherwise
guessable — the cooldown rejects the correct passcode too, by design.

`POST /api/alerts/run` accepts either `CRON_SECRET` (for the nightly job) or
the passcode (so a run can be triggered by hand from the UI).

**One connection per request.** `store.connection()` reuses a thread-local
connection for the length of a request (`before_request`/`teardown_request` in
`app.py`), opening one lazily on first use. Per-call connections cost nothing
against a local SQLite file — which is what this was first tested on — but
against a remote Postgres each one is a fresh TCP and TLS handshake, and
`GET /api/schedule` alone makes four calls. Outside a request scope (CLI, the
sync job, tests) it falls back to a short-lived connection, so no caller needs
to know whether a scope exists.

**The panel is a month calendar, expanded client-side.** `GET /api/schedule`
returns the raw rows under `all`; the browser places one-offs on their
`arrive_at` date and repeating trips on each matching weekday. Repeating trips
store no start date, so they are drawn from today forward only. The add form
sends the same two shapes the endpoint always accepted (`arrive_at`, or `days` +
`time_of_day`), so the redesign needed no schema change. `DAYS_AHEAD` (62) is
sized so imported events fill the month view.

**Calendar sync is over secret .ics URLs, not OAuth.** Google, Apple and
Outlook all publish one, so a single parser covers every provider with no
consent screen, no client secret and no accounts. The trade-offs are real and
worth re-reading before anyone proposes "just add Google login": the feed is
read-only, the URL is a bearer credential (kept server-side and never returned
to the browser), and Google caches its ICS output so edits can take hours to
appear. OAuth is the answer when that lag matters.

**Imports refuse weak geocodes.** Mapbox always returns something: without a
bbox, "zzzqqq not a real place" resolves to a village in Poland, and venue
names land on similar-sounding streets ("Chase Center" → "Chase Court,
Fremont", relevance 0.65) because v5 geocoding has thin POI coverage. The map's
search box hides this by showing five suggestions for a human to choose from;
an unattended import cannot, so anything below `GEOCODE_MIN_RELEVANCE` (0.8) is
skipped and reported rather than guessed. In practice calendar events need a
street address, not just a venue name.

Every sync re-geocodes instead of trusting stored coordinates — reusing them
made a bad match permanent. Within one run, lookups are deduped by location
string, so a recurring meeting costs one call, not one per occurrence.

**Alerts run without a browser.** `alerts.build_context()` does server-side
what the map does client-side — Mapbox Directions for the route,
point-to-polyline distance for disruption matching at the same 0.5/2 km
thresholds — then hands the result to `app.compute_brief()`, the same function
`POST /api/brief` uses, so an emailed alert and the on-screen one always agree.
Each occurrence is alerted once; the send is recorded per trip and start time,
so a cron that retries does not email twice.

**Scheduling the job.** Render's free tier has no cron, so
`.github/workflows/trip-alerts.yml` calls `POST /api/alerts/run` nightly.
GitHub Actions cron is free but imprecise — delayed under load, sometimes
skipped, and auto-disabled after 60 days without repo activity. Fine for "the
night before", not for anything time-critical. The first runs on `0 2 * * *`
started six and a half hours late (about 1:30 AM Pacific), so it now runs at
`:23` past, off GitHub's busiest top-of-hour slot.

`/api/alerts/run` answers 200 even when nothing was delivered, so the workflow
counts deliveries itself and fails the run if any alert did not send — a green
run used to mean only "the server answered". The repository is public and so
are Actions logs: the step prints counts only, never the response body, which
names trips and their times.

## 511 SF Bay (the disruption pool)

`get_disruptions()` feeds the red/yellow on-route alerts — the map's headline
feature. It pulls Open511 traffic events from `api.511.org/traffic/events`.

Two constraints shaped the design, and both are easy to trip over:

- **60 requests/hour, per token.** One call returns the whole nine-county
  region and the frontend already filters the pool against the drawn route, so
  we fetch once per TTL and serve every visitor from `_511_CACHE`. The 120s TTL
  caps us at 30 calls/hour. Lowering it approaches the ceiling fast.
- **Highways, not surface streets.** 511 catches a crash on the Bay Bridge
  approach; it will not catch a blocked street in the Mission. It complements
  the SF 311 civic feed rather than replacing it.

Mapping notes: Open511 geography is GeoJSON, so coordinates are `[lon, lat]` —
reversed from the `(lat, lon)` order used everywhere else in `mock_data`. A
LineString covers a stretch of road and is reduced to its midpoint. 511 serves
this endpoint with a UTF-8 BOM, so the body is decoded `utf-8-sig` before
`json.loads`. Their `severity` rides under the key `impact`, because the
frontend computes `severity` itself from distance and would overwrite it.

Degradation is layered: no token → mock pool; fetch fails with a warm cache →
last good response; fetch fails cold → mock pool. The map is never empty.

**511 requires acknowledgement as the data provider.** The alert panel carries a
"Traffic data · 511 SF Bay" credit that unhides only when the loaded pool
actually came from them.

## Architecture

The agent uses the raw Anthropic SDK in a manual tool-use loop — no framework.

**Data flow:** `run_scenarios.py` builds three `(route_id, date, label)` scenarios and passes each to `RouteIntelligenceAgent.analyze()` in `agent.py`. The agent sends the route context to Claude, which calls tools to gather data, then emits a structured `RISK / HEADLINE / WHY / RECOMMENDATION` alert. The loop is capped at `MAX_TURNS = 6` in `agent.py`.

**`route_id` is the universal key.** Every tool accepts a `route_id` (e.g. `rt_alex_home`) and resolves coordinates, corridor, departure time, and news area internally from `mock_data.ROUTINES`. Claude never sees raw lat/lon.

**Data layer (`mock_data.py`) is hybrid:**
| Source | Status | Notes |
|--------|--------|-------|
| Weather | Live | OpenWeather `/data/2.5/forecast`, 5-day/3-hour, filtered to commute window |
| News | Live | NewsAPI `/v2/everything`, scoped per `routine["news_area"]` + disruption keywords |
| Events | Mocked | Keyed by `route_id` in `_EVENTS_BY_ROUTE`; Ticketmaster swap documented in README |
| Traffic | Mocked | Keyed by `route_id` and departure time in `_TRAFFIC_BASELINE` |

**`tools.py`** owns the tool schemas Claude reasons over and a `_HANDLERS` dispatcher that routes each tool call to the matching `mock_data` function. The schemas are what prevent Claude from ever receiving raw coordinates.

## Swapping in a live data source

Replace the relevant function body in `mock_data.py` and keep the returned dict shape identical — nothing else needs to change. The tool schemas in `tools.py` and the agent loop in `agent.py` are source-agnostic.

## Model

Configured in `agent.py` as `MODEL = "claude-sonnet-5"`. The system prompt in that same file holds all judgment rules (baseline comparison, timing overlap, signal-vs-noise filtering).

## Product vision

This project is the foundation of a proactive GPS and route intelligence app for the Bay Area. The core painpoint: reactive tools like Google Maps and Waze tell you about disruptions after you've already left. This agent tells you the night before or morning of, based on your actual schedule.

Long-term goals:
- Sync to a user's calendar to know their planned routes for the day
- Send proactive alerts 12-24 hours ahead flagging events, weather, protests, sports games, and construction
- Web app with a route input form and alert card UI (demoable, shareable)
- Anonymized movement data layer across the Bay Area for B2B licensing (urban planning, retail, real estate)

## Roadmap

**Phase 1 — Foundation (current)**
- [x] Core agent with live weather + news, mocked events + traffic
- [x] Git + GitHub set up, code pushed to `github.com/jayt431/bay-area-predictive-gps`
- [x] Fixed `timing_overlap` bug: events now tagged with both geographic and timing relevance

**Phase 2 — Web backend**
- Wrap the agent in a Flask or FastAPI endpoint
- POST `{ route_id, date }` → returns structured alert JSON

**Phase 3 — Frontend**
- Simple form: origin, destination, departure time
- Alert card displaying RISK, headline, and recommendation
- Browser-accessible, screenshot/demo ready

**Phase 4 — Ship**
- [x] Deployed to Render (free tier)
- [x] Live at https://bay-area-predictive-gps.onrender.com
- [ ] GitHub README with demo recording for portfolio

**Phase 5 — Map-first GPS (built, ongoing)**
The homepage (`templates/index.html`, served at `/`) is now a single-user GPS
on a full-screen dark Mapbox map. Requires `MAPBOX_TOKEN` (env var; also set on
Render). Uses Mapbox GL JS + Turf (both via CDN). Skipped Figma — designing
directly in code.

- **Single user, fixed home base** (`mock_data.HOME`, `GET /api/config`). The
  three personas remain only for the agent + `/map-test`.
- **Search**: type any Bay Area address (Mapbox Geocoding) or tap a suggested
  chip. **Saved destinations** persist via browser `localStorage` (`bapg_saved`).
- **Routing**: real route via Mapbox Directions; route summary panel
  shows ETA, distance, and on-route disruption count.
- **Travel mode**: Drive / Walk / Bike tabs on the route card. All three
  profiles (`driving`, `walking`, `cycling`) are fetched in parallel on each
  pick, so every tab shows its own time and switching is instant; the choice
  persists in `localStorage` (`bapg_mode`). The mode decides more than the line:
  parking is drive-only (and a late meters response is dropped if the mode
  changed meanwhile), walking draws dashed, the brief receives `mode`, and the
  nav labels follow it. Scheduled trips store `mode` too (column added by
  `store._add_mode_column`, default `drive`; a calendar re-sync keeps the
  existing value), and `alerts.route()` routes with it.
- **Transit** (`transit.py`, `GET /api/transit`): **our own planner over 511
  GTFS**, not Google. Google's Routes API was built first and removed before
  any key was set: the Maps Platform Terms (§3.2.3(e)) forbid using its
  services "with or near a non-Google Map", and this is a Mapbox app. Open
  GTFS data can be drawn on any map.
  - **Data**: `gtfs_build.py` downloads Muni, BART and Caltrain (`SF,BA,CT`)
    from 511 with `TRAFFIC_511_TOKEN` and writes `transit_data/index.json.gz`
    (stops, lines, services, shapes, patterns) and `times.bin.gz` (every stop
    time as little-endian int32 seconds after service-day midnight) — about
    1.6 MB, **committed**, because Render's free tier restarts after 15 idle
    minutes with no persistent disk. Trips sharing a stop sequence are one
    *pattern*; each pattern stores, per stop, the nearest shape vertex
    (forward search) so a ride is cut from the real line shape. Platforms take
    their parent station's name. Loaded lazily on first transit request.
  - **Refresh**: `.github/workflows/transit-schedules.yml` rebuilds nightly
    (3:41 AM PT) and commits only when the output changed — the build is
    byte-reproducible (gzip with `mtime=0`, no filename), so an unchanged
    feed is no diff. The push redeploys Render. Before committing it runs
    `gtfs_build.py --check`: every operator present, a Powell → Ferry
    Building trip still plans, schedules not within 14 days of expiring. A
    red run leaves the committed schedules live. `transit_data/feeds.json`
    lists each agency's feed version and end date. Needs the
    `TRAFFIC_511_TOKEN` repo secret.
  - **Expiry**: `/api/health` reports `transit_data_until`; past it the
    planner answers "expired" instead of planning on stale schedules.
  - **Search**: stops within 800 m straight-line of each end (walk estimate
    ×1.3 detour at 1.25 m/s). Two searches, ranked together by `_choose`:
    - `_direct`: every pattern passing a stop near the origin and later one
      near the destination; first trip after you can walk there (or, for
      `arrive_by`, the last that arrives in time). Gives one option per line.
    - `_raptor`: round-based RAPTOR, up to `MAX_RIDES` = 3 (two transfers),
      `CHANGE_S` = 2 min to connect, walking transfers between stops within
      300 m (precomputed per stop at load). Per-day trip lists
      (`_day_trips`, today's plus yesterday's after-midnight trips shifted)
      are sorted by first-stop time; the data was checked to have **no
      overtaking within a day**, which is what makes binary-searching the
      next trip valid. Arrive-by runs the same code backwards through
      `_View`, which reverses stop order and negates times. A walk label never
      overwrites a stop a ride reached in the same round (unwinding expects a
      ride there). RAPTOR can also return a single ride whose walk passes a
      nearby stop on the way to a farther station, which `_direct` misses.
    - Transfers are shown only if they beat the best single ride by 5 min (or
      there is none). Rides that beat walking the whole way by under 3 min are
      dropped; when nothing is left, the error says walking is about as fast.
      Back-to-back walks merge into one. Errors are specific (no stop near,
      no route, expired) and the Transit tab shows a short form. The page
      inserts a "Change at … · N min wait" row between consecutive rides.
  - **Walks** use real Mapbox walking routes for the options shown (cached),
    straight lines if Mapbox fails.
  - Output keeps one shape for page, email and brief: segments of `walk` /
    `transit` with `line` (short name; empty for BART and Caltrain, whose
    badge falls back to the agency), `line_name`, colour, vehicle, headsign,
    stops, times. The frontend shapes it like a Mapbox route
    (`transitAsRoute`); rides draw in their line colour, walks dashed
    (`route-walk` layer, filtered on `kind`).
  - **Disruptions match only street-level parts** — walks, buses, streetcars,
    each as its own line (`streetLines` in the page, `street_lines` in
    `alerts.route`) — because street incidents don't delay BART or a ferry and
    joining the parts would draw a false line across the tunnel. Scheduled
    transit trips plan with `arrive_by` = the trip's time, giving a "Leave by"
    for the email and fallback brief. Schedules only; live delays (511
    real-time) are next, then transfers.
- **Disruptions**: pool in `mock_data` (`GET /api/disruptions`); frontend keeps
  those within 0.5km (red) / 2km (yellow) of the route line (Turf). Native GL
  circle/symbol layers, not HTML markers (fixes zoom jank). Bell + alert panel.
- **Parking** (`GET /api/parking`, auto-shows on route): "Show on map" zooms in
  and draws curbside colored lines for street parking (mocked zones snapped to
  the nearest real road) and outlines for **real garages** — pulled from the
  map's own tile data via `querySourceFeatures` on `poi_label` (parking is
  tagged class `motorist` + maki `parking`), building footprint outlined.
  Availability green/yellow/red; a "!" marker flags elevated break-in risk.

Parking status: **garages real** (map POIs), **metered streets real** (DataSF,
violet dots + panel), break-in caution still mocked. Meter dots auto-render on
routing so parking is visible while driving; curb lines + garage outlines draw
on arrival.

- **Committed trip / navigation.** "▶ Start trip" enters a nav view over the
  route. Two modes share one camera: **step-through** (Prev/Next or click a step
  → eased fly to each maneuver, facing `bearing_after`) and **auto-drive**
  ("▶ Drive") — a continuous fly-through along the route geometry (Turf `along`),
  with eased bearing smoothing to kill jitter, a 1×/2×/3× speed toggle, and a
  compressed ~60s-at-1× timeline (a simulation, labeled "Simulating drive" —
  there is no real GPS on desktop; swap in device GPS for a real mobile build).
- **Camera control.** A single press on the map canvas releases follow (so one
  drag grabs it, not three), the position dot keeps advancing, a "Re-center"
  button appears, and re-centering plays a guarded eased snap-back (the
  per-frame `jumpTo` is suppressed via a `recentering` flag so it isn't cut off).

- **Live weather + news in the brief.** `/api/brief` now enriches the trip with
  the live OpenWeather forecast for the trip window and live Bay Area headlines
  before the model reasons, so the two real sources reach the user-facing
  surface instead of only the CLI agent.

- **Live disruption pool (511 SF Bay).** The red/yellow route alerts now come
  from real Bay Area traffic events when `TRAFFIC_511_TOKEN` is set, region-wide
  rather than SF-only. See the 511 section above.

Next ideas: LEMMINO custom map style (Mapbox Studio); real break-in data (SFPD
incidents); live events via Ticketmaster; eventual accounts + DB.

## Future directions (not yet started)

- **Database (Postgres + PostGIS).** No DB today — routines are hardcoded and
  live data is fetched and discarded. Add one when user accounts arrive: it's
  the natural trigger. Needed for accounts, saved routes, calendar-synced
  schedules, notification history, and the B2B movement-data layer (spatial
  queries are why PostGIS specifically). Render offers free Postgres.
- **Lovable + Supabase hybrid.** Possible way to accelerate the frontend and
  get accounts/DB for free: build the React UI in Lovable on top of Supabase
  (Postgres + auth), and keep the Python agent as a separate service the
  frontend calls. Tradeoff: two systems instead of one, and less hands-on
  learning. Revisit after the map experience is solid, not before.
