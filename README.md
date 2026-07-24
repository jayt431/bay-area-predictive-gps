# Bay Area Predictive GPS

**Live demo:** https://bay-area-predictive-gps.onrender.com

An AI agent that reasons about a commuter's regular routine and predicts, a day
or more ahead, whether events, weather, or local disruptions will affect a
specific trip. Unlike a reactive tool that reports traffic happening right now,
this looks at what is scheduled and reported ahead of time and gives one clear
recommendation before you leave.

This build is anchored to the **San Francisco Bay Area** so the live data calls
return real, relevant results.

## What is live vs mocked

| Source   | Status | Backing                                             |
|----------|--------|-----------------------------------------------------|
| Weather  | LIVE   | OpenWeather 5 day / 3 hour forecast (free tier)     |
| News     | LIVE   | NewsAPI /everything, scoped to the Bay Area         |
| Events   | mocked | Ticketmaster swap documented below as a next step   |
| Traffic  | mocked | Real path: Google Maps Routes API typical traffic   |

Two of the four sources are real. The agent's whole value is synthesis across
sources, so a mix of live and mocked feeds is a legitimate MVP and still
demonstrates real API integration. Everything degrades gracefully: if a key is
missing, that source returns a clear "key not set" note and the run continues.

## Setup

```bash
pip install -r requirements.txt
```

Set the keys you have. Each is read from an environment variable and never
stored in the code:

```bash
export ANTHROPIC_API_KEY=sk-...        # required for the full agent run
export OPENWEATHER_API_KEY=...         # email-only signup at openweathermap.org
export NEWSAPI_KEY=...                 # email-only signup at newsapi.org
```

Note on OpenWeather: a new key can take up to a couple of hours to activate. If
your first call returns an "invalid key" error, that is expected, just wait.

## Running

```bash
python run_scenarios.py            # full agent run (needs ANTHROPIC_API_KEY)
python run_scenarios.py --dry-run  # no model call: dump exactly what the tools return
```

`--dry-run` needs no Anthropic key and is the fastest way to see the raw data
the agent reasons over. It works with or without the weather and news keys.

## The three scenarios

Each tests judgment, not just plumbing:

1. **Alex, evening commute.** A major Chase Center concert sits on the corridor
   with doors near departure time. The agent should flag it and suggest a timing
   change.
2. **Priya, Bay Bridge morning commute.** Events are quiet here, so live weather
   and news become the deciding variables. Good for watching the agent reason
   over unstructured news headlines.
3. **Sam, Ferry Building market run.** A routine day with only a small, ever
   present market on the route. The agent should notice it and correctly return
   RISK: none rather than flagging every event it sees.

Dates are computed relative to today so the 5-day weather forecast always has
coverage. Events are mocked per route, so the three narratives stay stable while
the live weather and news float with real conditions.

## Architecture

```
run_scenarios.py   Entry point. Builds the scenarios and runs the agent.
agent.py           The tool-use loop. Claude decides what to fetch, reasons,
                   and emits a structured alert. The system prompt holds the
                   judgment rules.
tools.py           Tool schemas Claude reasons over, plus a dispatcher.
mock_data.py       Routines, mocked events + traffic, and the LIVE weather and
                   news calls. The one place data sources live.
```

Every tool takes a `route_id`. The tool resolves coordinates and corridor from
the routine internally, so the model never handles raw latitude and longitude.

## Swapping in live events later

Events are the only remaining mock. To go live, replace the body of
`get_events_near_route` in `mock_data.py` with a call to the Ticketmaster
Discovery API (`https://app.ticketmaster.com/discovery/v2/events.json`), using a
`latlong` plus `radius` search around the route and a `startDateTime` /
`endDateTime` bound for the target day. Keep the returned dictionary shape
identical and nothing else in the project has to change. Note: Eventbrite's
public event-search endpoint was retired in 2020 and is not a viable source, so
Ticketmaster (or PredictHQ for event impact scoring) is the better choice.

## Design notes worth mentioning in an interview

- **Raw Anthropic SDK, not a framework.** The orchestration loop is visible and
  auditable rather than hidden behind an abstraction.
- **Tools return a baseline, not just anomalies.** The agent gets the normal
  traffic level for the route so it can reason about deltas, which is what makes
  "all clear" a real judgment rather than an absence of hits.
- **News is unstructured on purpose.** Having the agent read real headlines and
  decide relevance to a specific corridor shows off the reasoning far better
  than parsing a clean events JSON.
- **Graceful degradation.** A missing key downgrades one source instead of
  breaking the run, so the project is always demoable.
