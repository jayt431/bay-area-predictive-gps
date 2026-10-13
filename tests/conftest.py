"""
Shared test setup.

Tests never touch the network or real data. Every key is removed before the
app is imported — store.py reads its database path at import time, so this
has to happen here, first — and the schedule database is a throwaway file.
With no MAPBOX_TOKEN, transit walks fall back to straight-line estimates,
which keeps planner results deterministic.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

for key in ("MAPBOX_TOKEN", "TRAFFIC_511_TOKEN", "TICKETMASTER_API_KEY", "SCHEDULE_PASSCODE",
            "CRON_SECRET", "DATABASE_URL", "RESEND_API_KEY", "ANTHROPIC_API_KEY",
            "OPENWEATHER_API_KEY", "NEWSAPI_KEY"):
    os.environ.pop(key, None)
os.environ["SCHEDULE_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

# The demo home in SoMa, where every planner test starts.
HOME = (37.7785, -122.4056)


def next_weekday(weekday: int, after: date | None = None) -> date:
    """The next given weekday (Mon=0) at least two days out — inside the
    committed schedules' range, whenever the tests run, without depending on
    today's service quirks."""
    d = (after or date.today()) + timedelta(days=2)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


@pytest.fixture
def wednesday() -> str:
    return next_weekday(2).isoformat()


@pytest.fixture
def client():
    import app
    return app.app.test_client()
