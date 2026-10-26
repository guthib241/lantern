"""Reminders: a plain timer that pings you on your phone and the desktop at the time you asked.

"remind me at 6 pm to submit the video", "in 20 minutes: call Priya", "every day at 9:00 standup".
The model turns the user's words into `when` (it sees the current time); Lantern parses and checks
it here, in code: absolute ISO 8601 times, "in N minutes/hours/days", or a clock time like "18:30" or
"6pm" (today, or tomorrow if that time has passed). Times without a timezone are local.

A reminder repeats the user's own words. It is a timer, never advice (no health, no nagging).
Storage is in memory.py (encrypted at rest); the scheduler is Core.check_reminders; delivery is a
"reminder" event that the channel hub sends to every phone channel and the desktop shows.
"""
from __future__ import annotations

import datetime as dt
import re

REPEATS = ("", "daily", "weekly")
MAX_AHEAD = dt.timedelta(days=366)
MAX_TEXT = 300
LATE_AFTER = 120.0   # seconds: delivered later than this (Lantern was off) gets a note

_UNITS = {"m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
          "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
          "d": 86400, "day": 86400, "days": 86400}
_RELATIVE = re.compile(r"^in\s+(\d+(?:\.\d+)?)\s*([a-z]+)$")
_CLOCK = re.compile(r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")


class ReminderError(ValueError):
    pass


def _local(moment: dt.datetime) -> dt.datetime:
    """Aware datetime in the computer's local timezone (naive input is taken as local)."""
    return moment.astimezone() if moment.tzinfo else moment.replace(tzinfo=None).astimezone()


def parse_when(when: str, now: dt.datetime | None = None) -> dt.datetime:
    """When a reminder is due, as an aware local datetime. Raises ReminderError with a fix-it hint."""
    now = _local(now or dt.datetime.now())
    text = (when or "").strip().lower()
    if not text:
        raise ReminderError("say when: an ISO time like 2026-10-27T18:00, 'in 20 minutes' or '18:30'")
    if m := _RELATIVE.match(text):
        unit = _UNITS.get(m.group(2))
        if unit is None:
            raise ReminderError(f"unknown unit '{m.group(2)}': use minutes, hours or days")
        due = now + dt.timedelta(seconds=float(m.group(1)) * unit)
    elif m := _CLOCK.match(text):
        hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if half:
            if not 1 <= hour <= 12:
                raise ReminderError(f"'{when}' isn't a clock time")
            hour = hour % 12 + (12 if half == "pm" else 0)
        if hour > 23 or minute > 59:
            raise ReminderError(f"'{when}' isn't a clock time")
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= now:
            due += dt.timedelta(days=1)
    else:
        try:
            due = _local(dt.datetime.fromisoformat(when.strip()))
        except ValueError:
            raise ReminderError(f"can't read the time '{when}': use ISO 8601 (2026-10-27T18:00), "
                                "'in 20 minutes' or '18:30'") from None
    if due <= now - dt.timedelta(seconds=60):
        raise ReminderError(f"{due:%a %d %b %H:%M} is in the past")
    if due - now > MAX_AHEAD:
        raise ReminderError("that's more than a year away")
    return due


def next_due(due_at: float, repeat: str, now: float) -> float | None:
    """For a repeating reminder, the next time after `now` (wall-clock, so DST keeps 9:00 at 9:00)."""
    if not repeat:
        return None
    step = dt.timedelta(days=1 if repeat == "daily" else 7)
    nxt = dt.datetime.fromtimestamp(due_at)   # naive local
    while nxt.timestamp() <= now:
        nxt += step
    return nxt.timestamp()


def check_text(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise ReminderError("the reminder needs some words")
    if len(text) > MAX_TEXT:
        raise ReminderError(f"keep reminders under {MAX_TEXT} characters")
    return text


def describe(due_at: float, now: float | None = None) -> str:
    """'today 18:00', 'tomorrow 09:30', 'Fri 31 Oct 18:00'."""
    due = dt.datetime.fromtimestamp(due_at)
    today = dt.datetime.fromtimestamp(now if now is not None else dt.datetime.now().timestamp()).date()
    if due.date() == today:
        return f"today {due:%H:%M}"
    if due.date() == today + dt.timedelta(days=1):
        return f"tomorrow {due:%H:%M}"
    return f"{due:%a %d %b %H:%M}"
