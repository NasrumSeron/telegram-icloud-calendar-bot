"""
Writes a parsed event to your iCloud calendar via CalDAV.

v2 changes (calendar choice + alarms):
  1. `calendar_name` is now a real argument, not just an env var -- the
     calendar picker (S3) and Butler (later, calling this as a tool) both
     need to choose a calendar per event, not per deployment.
  2. Events now carry an ALARM, so your iPhone actually notifies you.
     Timed events: 30 min before. All-day events: 09:00 on the day.
  3. The iCalendar text is now built here, explicitly, instead of letting
     the caldav library assemble it from keyword arguments. Two reasons:
     alarms need more control than the shortcut arguments give (especially
     for all-day events), and building it ourselves means `build_event_ical()`
     is a pure function -- no network, no credentials -- so it can be tested
     completely offline. See test_ical_offline.py.

This module deliberately has NO dependency on Gemini or Telegram. It takes
an object with the ParsedEvent fields and writes it. That's what makes it
reusable by anything later, Butler included.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import caldav
import icalendar
from dotenv import load_dotenv

if TYPE_CHECKING:  # import for type hints only -- keeps this module standalone
    from gemini_parse import ParsedEvent

load_dotenv()

# EDIT ME: how long to wait for iCloud before giving up.
CALDAV_TIMEOUT_SECONDS = 30

# EDIT ME: if an event has no end time and isn't all-day, assume this long.
DEFAULT_DURATION = timedelta(hours=1)

# EDIT ME: how long before a timed event its alarm should fire.
# Set to None to create timed events with no alarm at all.
ALARM_MINUTES_BEFORE = 30

# EDIT ME: what time of day an all-day event's alarm should fire (24h clock).
# Set to None to create all-day events with no alarm at all.
ALL_DAY_ALARM_HOUR = 9

# Sentinel so callers can pass alarm=None meaning "no alarm" and still let
# "not specified" fall back to the constants above. `None` alone can't say both.
_USE_DEFAULT = object()


def _get_client() -> caldav.DAVClient:
    return caldav.DAVClient(
        url="https://caldav.icloud.com",
        username=os.environ["ICLOUD_APPLE_ID"],
        password=os.environ["ICLOUD_APP_SPECIFIC_PASSWORD"],
        timeout=CALDAV_TIMEOUT_SECONDS,
    )


def list_calendar_names() -> list[str]:
    """Every calendar name visible to this account. Used to confirm
    ICLOUD_CALENDAR_NAME matches, and (S3) to build the picker buttons."""
    client = _get_client()
    principal = client.principal()
    return [cal.name for cal in principal.calendars()]


def _find_calendar(client: caldav.DAVClient, calendar_name: str) -> caldav.Calendar:
    principal = client.principal()
    calendars = principal.calendars()
    for cal in calendars:
        if cal.name == calendar_name:
            return cal
    available = [cal.name for cal in calendars]
    raise ValueError(
        f"No calendar named '{calendar_name}' found. "
        f"Available calendars on this account: {available}. "
        f"Check the name matches exactly (case-sensitive)."
    )


def build_event_ical(
    event: "ParsedEvent",
    *,
    alarm_offset=_USE_DEFAULT,
    uid: Optional[str] = None,
    dtstamp: Optional[datetime] = None,
) -> str:
    """Turn a ParsedEvent into iCalendar text. Pure function: no network,
    no credentials, fully testable offline.

    alarm_offset: a timedelta relative to the event's start, or None for no
    alarm. NEGATIVE means before the start (e.g. timedelta(minutes=-30) is
    the usual "remind me half an hour ahead"); POSITIVE means after it, which
    is how an all-day event gets a 09:00 alarm -- an all-day event starts at
    midnight, so +9h lands at 9am. Leave it unset to use the module
    constants above.
    """
    start_dt = datetime.fromisoformat(event.start_datetime)

    cal = icalendar.Calendar()
    cal.add("prodid", "-//telegram-calendar-bot//EN")
    cal.add("version", "2.0")

    ical_event = icalendar.Event()
    ical_event.add("uid", uid or str(uuid.uuid4()))
    ical_event.add("dtstamp", dtstamp or datetime.now(timezone.utc))
    ical_event.add("summary", event.title)

    if event.all_day:
        # All-day events use plain dates, not datetimes -- otherwise iCloud
        # shows a timed event at whatever midnight happens to be.
        dtstart: date | datetime = start_dt.date()
        if event.end_datetime:
            dtend: date | datetime = datetime.fromisoformat(event.end_datetime).date()
        else:
            dtend = dtstart + timedelta(days=1)  # all-day events need dtend = next day
        default_alarm = (
            timedelta(hours=ALL_DAY_ALARM_HOUR) if ALL_DAY_ALARM_HOUR is not None else None
        )
    else:
        # Written as UTC ("...Z") rather than with a TZID. Same instant either
        # way, and it avoids having to ship a full VTIMEZONE block that some
        # servers are fussy about. Your phone still shows Singapore time.
        dtstart = start_dt.astimezone(timezone.utc)
        end_source = (
            datetime.fromisoformat(event.end_datetime)
            if event.end_datetime
            else start_dt + DEFAULT_DURATION
        )
        dtend = end_source.astimezone(timezone.utc)
        default_alarm = (
            timedelta(minutes=-ALARM_MINUTES_BEFORE) if ALARM_MINUTES_BEFORE is not None else None
        )

    ical_event.add("dtstart", dtstart)
    ical_event.add("dtend", dtend)

    if event.location:
        ical_event.add("location", event.location)
    if event.notes:
        ical_event.add("description", event.notes)

    offset = default_alarm if alarm_offset is _USE_DEFAULT else alarm_offset
    if offset is not None:
        alarm = icalendar.Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", event.title)
        alarm.add("trigger", offset)  # relative to DTSTART by default
        ical_event.add_component(alarm)

    cal.add_component(ical_event)
    return cal.to_ical().decode("utf-8")


def create_calendar_event(
    event: "ParsedEvent",
    calendar_name: Optional[str] = None,
    *,
    alarm_offset=_USE_DEFAULT,
) -> str:
    """Write `event` to iCloud. Returns the new event's URL.

    calendar_name: which calendar to write to. Defaults to
    ICLOUD_CALENDAR_NAME from .env, so existing callers keep working.
    """
    calendar_name = calendar_name or os.environ.get("ICLOUD_CALENDAR_NAME", "Bot Events")
    ical_text = build_event_ical(event, alarm_offset=alarm_offset)

    client = _get_client()
    calendar = _find_calendar(client, calendar_name)
    new_event = calendar.add_event(ical_text)
    return new_event.url


if __name__ == "__main__":
    print("Calendars visible on this account:")
    for name in list_calendar_names():
        print(f"  - {name!r}")
