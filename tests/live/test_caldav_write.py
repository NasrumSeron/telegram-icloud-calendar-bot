"""
Run this to test the CalDAV write in isolation, BEFORE wiring it into the
actual bot. It does two things:

1. Lists every calendar visible on your account (so you can confirm
   ICLOUD_CALENDAR_NAME in .env matches exactly).
2. Creates ONE clearly-labeled test event on that calendar, tomorrow, so you
   can check your iPhone and confirm it shows up correctly -- then delete it
   yourself once confirmed (this script doesn't delete anything).

Usage:
    python tests/live/test_caldav_write.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))

if not _os.environ.get("RUN_LIVE_TESTS"):
    print("SKIPPED: live test (real network / Gemini / iCloud). Set RUN_LIVE_TESTS=1 and fill .env to run it.")
    raise SystemExit(0)

import os
from datetime import datetime, timedelta

from caldav_write import create_calendar_event, list_calendar_names
from gemini_parse import ParsedEvent, SINGAPORE_TZ


def main() -> None:
    print("=" * 70)
    print("Calendars visible on this account:")
    names = list_calendar_names()
    for name in names:
        print(f"  - {name!r}")

    target = os.environ.get("ICLOUD_CALENDAR_NAME", "Bot Events")
    if target not in names:
        print(f"\nWARNING: '{target}' (from ICLOUD_CALENDAR_NAME) is not in the list above.")
        print("Fix the name in .env (case-sensitive, exact match) before continuing.")
        return
    print(f"\nTarget calendar '{target}' found. Proceeding to create a test event.\n")

    tomorrow = datetime.now(SINGAPORE_TZ) + timedelta(days=1)
    test_event = ParsedEvent(
        title="TEST EVENT -- safe to delete (bot testing)",
        start_datetime=tomorrow.replace(hour=10, minute=0, second=0, microsecond=0).isoformat(),
        end_datetime=tomorrow.replace(hour=11, minute=0, second=0, microsecond=0).isoformat(),
        location="Nowhere in particular",
        all_day=False,
        notes="Created by test_caldav_write.py -- delete once you've confirmed it looks right on your iPhone.",
    )

    print("=" * 70)
    print("Creating test event:")
    print(test_event.model_dump_json(indent=2))

    url = create_calendar_event(test_event)
    print(f"\nSUCCESS. Event created at: {url}")
    print("Check your iPhone's Calendar app (or icloud.com/calendar) to confirm it appears")
    print("correctly on the target calendar, tomorrow 10-11am, with the right title/location.")
    print("Delete it yourself once confirmed -- this script won't clean up after itself.")


if __name__ == "__main__":
    main()
