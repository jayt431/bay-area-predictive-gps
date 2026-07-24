"""
Entry point. Runs three Bay Area routines through the agent.

    python run_scenarios.py            # full run
    python run_scenarios.py --dry-run  # no model call, just dump tool output

Keys used:
    ANTHROPIC_API_KEY   required for the full run
    OPENWEATHER_API_KEY required for live weather
    NEWSAPI_KEY         required for live Bay Area news

Dates are computed relative to today so the live 5-day weather forecast always
has coverage. Events are mocked per route, so the three narratives stay stable:
  1. Alex  - a major Chase Center concert on the evening corridor (event-driven)
  2. Priya - Bay Bridge morning commute (weather/news are the variables)
  3. Sam   - Ferry Building market run, a routine day the agent should clear
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

import mock_data
import tools
from agent import RouteIntelligenceAgent, api_key_present

_today = date.today()
SCENARIOS = [
    ("rt_alex_home", (_today + timedelta(days=1)).isoformat(), "Evening commute, arena event"),
    ("rt_priya_work", (_today + timedelta(days=2)).isoformat(), "Bay Bridge morning commute"),
    ("rt_sam_market", (_today + timedelta(days=4)).isoformat(), "Weekend market run"),
]


def _header(title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def dry_run():
    for route_id, day, label in SCENARIOS:
        r = mock_data.get_routine(route_id)
        _header(f"[DRY RUN] {r['user']} - {label} - {day}")
        print("Events:\n", tools.run_tool("get_events_near_route", {"route_id": route_id, "date": day}))
        print("\nWeather:\n", tools.run_tool("get_weather_forecast", {"route_id": route_id, "date": day}))
        print("\nTraffic baseline:\n", tools.run_tool("get_traffic_baseline", {"route_id": route_id}))
        print("\nNews:\n", tools.run_tool("get_local_news", {"route_id": route_id, "date": day}))


def full_run():
    if not api_key_present():
        print("ANTHROPIC_API_KEY is not set. Set it and re-run, or use --dry-run.")
        return
    agent = RouteIntelligenceAgent()
    for route_id, day, label in SCENARIOS:
        r = mock_data.get_routine(route_id)
        _header(f"{r['user']} - {label} - {day}")
        print(agent.analyze(r, day) + "\n")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        dry_run()
    else:
        full_run()
