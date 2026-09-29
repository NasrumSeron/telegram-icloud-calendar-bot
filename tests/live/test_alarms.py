"""
Stage S1 live test: does iCloud actually honour an alarm we set, and does
your iPhone actually notify you?

This is the one question the offline test can't answer, and the whole
"reminders" feature rests on it. So the test is built to answer it FAST --
both test events are timed so their alarms fire a few minutes from when you
run this, instead of tomorrow.

What it creates (both clearly labelled, both safe to delete afterwards):

  A. A TIMED event starting ~35 minutes from now, with the normal 30-minutes-
     before alarm -> you should get a notification in about 5 minutes.
  B. An ALL-DAY event tomorrow, with its alarm deliberately shifted so it
     fires in about 7 minutes instead of at 09:00 -> this checks that all-day
     alarms work at all, without waiting until tomorrow morning.

Usage:
    python tests/live/test_alarms.py                  # uses ICLOUD_CALENDAR_NAME from .env
    python tests/live/test_alarms.py "Bot Events"     # or name the calendar explicitly

In Docker:
    docker compose run --rm -e RUN_LIVE_TESTS=1 calendar-bot python tests/live/test_alarms.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))

if not _os.environ.get("RUN_LIVE_TESTS"):
    print("SKIPPED: live test (real network / Gemini / iCloud). Set RUN_LIVE_TESTS=1 and fill .env to run it.")
    raise SystemExit(0)

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from caldav_write import build_event_ical, create_calendar_event, list_calendar_names

SGT = ZoneInfo("Asia/Singapore")

# EDIT ME: how many minutes from now each test notification should arrive.
TIMED_ALARM_IN_MINUTES = 5
ALL_DAY_ALARM_IN_MINUTES = 7


@dataclass
class TestEvent:
    """Same fields as ParsedEvent, defined locally so this test doesn't need
    the Gemini libraries just to write a calendar entry."""

    title: str
    start_datetime: str
    end_datetime: Optional[str] = None
    location: Optional[str] = None
    all_day: bool = False
    notes: Optional[str] = None


def main() -> None:
    calendar_name = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ICLOUD_CALENDAR_NAME", "Bot Events")

    print("=" * 70)
    print("Calendars visible on this account:")
    names = list_calendar_names()
    for name in names:
        print(f"  - {name!r}")
    if calendar_name not in names:
        print(f"\nSTOP: '{calendar_name}' is not in that list. Pass the exact name as an")
        print("argument, or fix ICLOUD_CALENDAR_NAME in .env. Nothing was created.")
        return
    print(f"\nWriting both test events to '{calendar_name}'.\n")

    now = datetime.now(SGT)

    # ---- A. timed event, standard 30-min-before alarm ----
    start_a = (now + timedelta(minutes=30 + TIMED_ALARM_IN_MINUTES)).replace(second=0, microsecond=0)
    event_a = TestEvent(
        title="ALARM TEST 1 (timed) -- safe to delete",
        start_datetime=start_a.isoformat(),
        end_datetime=(start_a + timedelta(hours=1)).isoformat(),
        location="Nowhere in particular",
        notes="Created by test_alarms.py. Delete once you've seen the notification.",
    )

    # ---- B. all-day event, alarm shifted so it fires in a few minutes ----
    fire_b = (now + timedelta(minutes=ALL_DAY_ALARM_IN_MINUTES)).replace(second=0, microsecond=0)
    target_day = (now + timedelta(days=1)).date()
    midnight_b = datetime.combine(target_day, datetime.min.time(), tzinfo=SGT)
    if fire_b >= midnight_b:  # running just before midnight -- push the event a day out
        target_day = target_day + timedelta(days=1)
        midnight_b = datetime.combine(target_day, datetime.min.time(), tzinfo=SGT)
    offset_b = fire_b - midnight_b  # negative: fires before the all-day event starts

    event_b = TestEvent(
        title="ALARM TEST 2 (all-day) -- safe to delete",
        start_datetime=midnight_b.isoformat(),
        all_day=True,
        notes="Created by test_alarms.py. Alarm shifted for testing; real all-day events alarm at 09:00.",
    )

    print("-" * 70)
    print(f"A. Timed event  : {start_a.strftime('%a %d %b, %I:%M %p')}  (1 hour)")
    print(f"   Alarm should fire at ~{(start_a - timedelta(minutes=30)).strftime('%I:%M %p')}"
          f"  (about {TIMED_ALARM_IN_MINUTES} min from now)")
    print("\n   iCalendar being sent:")
    print("   " + build_event_ical(event_a).replace("\r\n", "\n").strip().replace("\n", "\n   "))

    url_a = create_calendar_event(event_a, calendar_name)
    print(f"\n   Created: {url_a}")

    print("-" * 70)
    print(f"B. All-day event: {target_day.strftime('%a %d %b')}")
    hours, remainder = divmod(int(-offset_b.total_seconds()), 3600)
    print(f"   Alarm should fire at ~{fire_b.strftime('%I:%M %p')}"
          f"  (about {ALL_DAY_ALARM_IN_MINUTES} min from now;"
          f" trigger set to {hours}h{remainder // 60:02d}m before the event's midnight start)")
    print("\n   iCalendar being sent:")
    print("   " + build_event_ical(event_b, alarm_offset=offset_b).replace("\r\n", "\n").strip().replace("\n", "\n   "))

    url_b = create_calendar_event(event_b, calendar_name, alarm_offset=offset_b)
    print(f"\n   Created: {url_b}")

    print("=" * 70)
    print("Now check your iPhone. Three things, in order of importance:\n")
    print(f"  1. Did a notification appear in ~{TIMED_ALARM_IN_MINUTES} min (timed) and"
          f" ~{ALL_DAY_ALARM_IN_MINUTES} min (all-day)?")
    print("     THIS is the answer we need. Notifications firing = reminders are viable.")
    print("  2. Open each event on the phone -- does it show an alert/reminder line?")
    print("     If yes but no notification arrived, the alarm saved fine and the problem")
    print("     is phone-side (Calendar notifications off in iOS Settings), not our code.")
    print("  3. Both events on the right calendar, right day, right time?\n")
    print("Then delete both test events yourself -- this script doesn't clean up.")
    print("\nIf NEITHER notification arrives and neither event shows an alert line,")
    print("iCloud is dropping client-set alarms, and reminders wait for a scheduler.")


if __name__ == "__main__":
    main()
