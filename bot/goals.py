"""Cycle arithmetic for recurring throwing goals. No Discord and no database, so it's easy to check.

Cycle 0 starts at the goal's starts_at, a time on the clock in the bot's time zone, and cycle k
starts k * every_count units after that. Nothing about a cycle is stored, so cycles before the goal
was created work like any other: a goal that starts in the past can be counted from then.

Daylight saving. Days, weeks and months are counted on the clock: a cycle that starts at 20:00 on
Sunday starts at 20:00 every Sunday, and the week the clocks change is an hour shorter or longer.
Hours and minutes are counted as real elapsed time. A clock time that doesn't exist (02:30 on the
night the clocks go forward) means the moment an hour later, and one that happens twice (01:30 when
they go back) means the first; either way cycle starts never go backwards or repeat.

Times are compared as UTC throughout (_utc). Python compares two datetimes in the same time zone by
their clock readings alone, which gets the repeated hour wrong.
"""

import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UNITS = ("hour", "day", "week", "month")
OFFSET_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
# The shortest a cycle of one unit can be, in days: for a first guess at which cycle a time is in.
_SHORTEST_DAYS = {"day": 1, "week": 7, "month": 28}


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc)


def _on_clock(naive: datetime, tz: ZoneInfo) -> datetime:
    """The real moment a clock time means. Going through UTC turns a time that doesn't exist into
    the one an hour later, written the way that day's clocks show it."""
    return naive.replace(tzinfo=tz).astimezone(timezone.utc).astimezone(tz)


def _elapsed(moment: datetime, delta: timedelta, tz: ZoneInfo) -> datetime:
    """moment + delta of real time. (Adding to a datetime with a time zone moves its clock instead.)"""
    return (moment.astimezone(timezone.utc) + delta).astimezone(tz)


def _add_months(naive: datetime, months: int) -> datetime:
    """The same day and time `months` later; the 31st becomes the last day of a shorter month."""
    year, month = divmod(naive.year * 12 + naive.month - 1 + months, 12)
    return naive.replace(year=year, month=month + 1, day=min(naive.day, calendar.monthrange(year, month + 1)[1]))


def cycle_start(goal: dict, cycle: int, tz: ZoneInfo) -> datetime:
    """Always worked out from cycle 0, never from the cycle before, so nothing drifts: a monthly
    goal that starts on the 31st is back on the 31st after February."""
    anchor = datetime.fromisoformat(goal["starts_at"])
    count, unit = goal["every_count"] * cycle, goal["every_unit"]
    if unit == "hour":
        return _elapsed(_on_clock(anchor, tz), timedelta(hours=count), tz)
    if unit == "month":
        return _on_clock(_add_months(anchor, count), tz)
    return _on_clock(anchor + timedelta(days=count * (7 if unit == "week" else 1)), tz)


def cycle_bounds(goal: dict, cycle: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[start, end) of a cycle."""
    return cycle_start(goal, cycle, tz), cycle_start(goal, cycle + 1, tz)


def current_cycle(goal: dict, now: datetime, tz: ZoneInfo) -> int | None:
    """Which cycle `now` falls in. None before the goal starts."""
    now = _utc(now)
    since = now - _utc(cycle_start(goal, 0, tz))
    if since < timedelta(0):
        return None
    if goal["every_unit"] == "hour":
        return since // timedelta(hours=goal["every_count"])
    # A guess that can only be too high (cycles are at least this long, bar an hour), then walk to
    # the cycle that really contains now.
    cycle = since // timedelta(days=_SHORTEST_DAYS[goal["every_unit"]] * goal["every_count"])
    while cycle > 0 and _utc(cycle_start(goal, cycle, tz)) > now:
        cycle -= 1
    while _utc(cycle_start(goal, cycle + 1, tz)) <= now:
        cycle += 1
    return cycle


def parse_offset(offset: str) -> tuple[int, str]:
    """ "2d" -> (2, "d")."""
    return int(offset[:-1]), offset[-1]


def reminder_times(goal: dict, cycle: int, tz: ZoneInfo) -> list[datetime]:
    """When this cycle's reminders go out, earliest first: each of the goal's offsets before the
    cycle ends. Days are counted on the clock, hours and minutes as elapsed time. An offset as long
    as the cycle or longer has no time in it and is left out."""
    start, end = cycle_bounds(goal, cycle, tz)
    times = {}  # by UTC time, so two offsets that land on the same moment give one reminder
    for offset in goal["reminders"]:
        count, unit = parse_offset(offset)
        if unit == "d":
            at = _on_clock(end.replace(tzinfo=None) - timedelta(days=count), tz)
        else:
            at = _elapsed(end, -timedelta(**{OFFSET_UNITS[unit]: count}), tz)
        if _utc(start) < _utc(at) < _utc(end):
            times[_utc(at)] = at
    return [times[t] for t in sorted(times)]


def due_reminder(goal: dict, now: datetime, tz: ZoneInfo) -> datetime | None:
    """The latest reminder time in the current cycle that has passed and hasn't been dealt with."""
    cycle = current_cycle(goal, now, tz)
    if cycle is None:
        return None
    done = datetime.fromisoformat(goal["last_reminder_at"]) if goal["last_reminder_at"] else None
    passed = [
        t for t in reminder_times(goal, cycle, tz)
        if _utc(t) <= _utc(now) and (done is None or _utc(t) > _utc(done))
    ]
    return passed[-1] if passed else None
