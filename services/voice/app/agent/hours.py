"""Is the restaurant open.

An agent that cheerfully takes an order at 2am for a kitchen that closed at
ten is worse than one that does not answer. Hours live in the database with
one-off exceptions, so a holiday closure is a row rather than a code change.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import asyncpg


def _covers(now: time, opens: time, closes: time) -> bool:
    """Handles windows that run past midnight, where closes < opens."""
    if closes > opens:
        return opens <= now < closes
    return now >= opens or now < closes


async def status(
    conn: asyncpg.Connection,
    restaurant_id: str,
    tz: str,
    at: datetime | None = None,
    service: str = "dining",
) -> dict:
    """Open or closed now, and when that next changes."""
    zone = ZoneInfo(tz)
    now = (at or datetime.now(zone)).astimezone(zone)
    today: date = now.date()

    exception = await conn.fetchrow(
        "SELECT is_closed, opens_at, closes_at, note FROM service_exceptions"
        " WHERE restaurant_id=$1 AND on_date=$2",
        restaurant_id,
        today,
    )
    if exception is not None:
        if exception["is_closed"]:
            return {
                "open": False,
                "reason": exception["note"] or "closed today",
                "opens_at": None,
            }
        if exception["opens_at"] and exception["closes_at"]:
            is_open = _covers(now.time(), exception["opens_at"], exception["closes_at"])
            return {
                "open": is_open,
                "reason": exception["note"],
                "opens_at": exception["opens_at"].strftime("%-I:%M %p"),
                "closes_at": exception["closes_at"].strftime("%-I:%M %p"),
            }

    # Postgres day_of_week is 0 = Sunday, matching the seed.
    dow = (now.weekday() + 1) % 7
    windows = await conn.fetch(
        "SELECT opens_at, closes_at FROM service_hours"
        " WHERE restaurant_id=$1 AND day_of_week=$2 AND service=$3"
        " ORDER BY opens_at",
        restaurant_id,
        dow,
        service,
    )
    for w in windows:
        if _covers(now.time(), w["opens_at"], w["closes_at"]):
            return {
                "open": True,
                "closes_at": w["closes_at"].strftime("%-I:%M %p"),
            }

    # Closed. Find the next opening so the agent can say something useful
    # instead of just "we're closed".
    for ahead in range(0, 8):
        day = (dow + ahead) % 7
        rows = await conn.fetch(
            "SELECT opens_at FROM service_hours"
            " WHERE restaurant_id=$1 AND day_of_week=$2 AND service=$3"
            " ORDER BY opens_at",
            restaurant_id,
            day,
            service,
        )
        for row in rows:
            if ahead == 0 and row["opens_at"] <= now.time():
                continue
            when = (now + timedelta(days=ahead)).strftime("%A")
            label = "today" if ahead == 0 else ("tomorrow" if ahead == 1 else when)
            return {
                "open": False,
                "opens_at": f"{label} at {row['opens_at'].strftime('%-I:%M %p')}",
            }

    return {"open": False, "opens_at": None}
