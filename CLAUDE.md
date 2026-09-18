# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt

# Web app (the main product — map GPS UI). Keys are read from a local .env.
source .env && python app.py          # dev server at http://127.0.0.1:5000
# Production (Render) runs: gunicorn app:app   (see Procfile)

# Standalone agent scenarios (the original CLI harness)
python run_scenarios.py               # full agent run (requires ANTHROPIC_API_KEY)
python run_scenarios.py --dry-run     # dumps raw tool output, no model call needed
```

Local dev loads keys from a gitignored `.env` (`source .env && python app.py`).
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
- **Routing**: real driving route via Mapbox Directions; route summary panel
  shows ETA, distance, and on-route disruption count.
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
