"""Cycle arithmetic for recurring throwing goals. No Discord and no database, so it's easy to check.

Cycle 0 starts at midnight (in the bot's time zone) on the goal's first_day, and each cycle lasts
cycle_days. Days are counted on the calendar, so a cycle is still whole days across a clock change.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def current_cycle(goal: dict, now: datetime, tz: ZoneInfo) -> int | None:
    """Which cycle `now` falls in. None before the goal's first day."""
    days = (now.astimezone(tz).date() - date.fromisoformat(goal["first_day"])).days
    return days // goal["cycle_days"] if days >= 0 else None


def cycle_bounds(goal: dict, cycle: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of a cycle."""
    first = date.fromisoformat(goal["first_day"]) + timedelta(days=cycle * goal["cycle_days"])
    return datetime.combine(first, time(), tz), datetime.combine(first + timedelta(days=goal["cycle_days"]), time(), tz)


def last_day(goal: dict, cycle: int) -> date:
    return date.fromisoformat(goal["first_day"]) + timedelta(days=(cycle + 1) * goal["cycle_days"] - 1)


def reminder_times(goal: dict, cycle: int, tz: ZoneInfo) -> list[datetime]:
    """When this cycle's reminders go out, earliest first: remind_hour on each of the days that are
    remind_days_before the cycle's last day (0 is the last day itself)."""
    final = last_day(goal, cycle)
    return sorted(
        datetime.combine(final - timedelta(days=d), time(goal["remind_hour"]), tz)
        for d in set(goal["remind_days_before"])
        if 0 <= d < goal["cycle_days"]
    )


def due_reminder(goal: dict, now: datetime, tz: ZoneInfo) -> datetime | None:
    """The latest reminder time in the current cycle that has passed and hasn't been dealt with."""
    cycle = current_cycle(goal, now, tz)
    if cycle is None:
        return None
    done = datetime.fromisoformat(goal["last_reminder_at"]) if goal["last_reminder_at"] else None
    passed = [t for t in reminder_times(goal, cycle, tz) if t <= now and (done is None or t > done)]
    return passed[-1] if passed else None
