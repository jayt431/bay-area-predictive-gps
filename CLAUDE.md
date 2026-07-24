# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt

python run_scenarios.py            # full agent run (requires ANTHROPIC_API_KEY)
python run_scenarios.py --dry-run  # dumps raw tool output, no model call needed
```

Required env vars:
- `ANTHROPIC_API_KEY` — needed for the full run only
- `OPENWEATHER_API_KEY` — live weather (free tier, new keys can take ~2 hours to activate)
- `NEWSAPI_KEY` — live Bay Area news (free developer tier)

Missing keys degrade that source gracefully; the run never hard-fails.

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
