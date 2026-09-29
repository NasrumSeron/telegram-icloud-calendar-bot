"""
Offline check of the iCalendar text we generate -- no network, no iCloud
credentials, no Gemini. Runs anywhere in about a second.

It builds events with build_event_ical(), parses the result back with the
icalendar library, and asserts the parts that are easy to get subtly wrong:
whether the alarm exists, what its trigger says, and whether all-day events
use plain dates instead of times.

This is the "prove it before you send it" step. The live iCloud test
(test_alarms.py) answers a different question -- whether Apple actually
honours what we send.

Usage:
    python tests/test_ical_offline.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import icalendar

from caldav_write import build_event_ical

# A stand-in for ParsedEvent with just the fields build_event_ical() reads.
# Deliberately not importing the real one, so this test needs no Gemini
# libraries installed to run.
@dataclass
class FakeEvent:
    title: str
    start_datetime: str
    end_datetime: Optional[str] = None
    location: Optional[str] = None
    all_day: bool = False
    notes: Optional[str] = None


def parse(ical_text: str):
    cal = icalendar.Calendar.from_ical(ical_text)
    vevent = cal.walk("vevent")[0]
    alarms = vevent.walk("valarm")
    return vevent, alarms


checks_run = 0
failures = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks_run
    checks_run += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


def main() -> None:
    print("=" * 70)
    print("1. Timed event, default alarm (should be 30 min before)")
    timed = FakeEvent(
        title="Lunch with John",
        start_datetime="2026-09-11T13:00:00+08:00",
        location="Toast Box",
        notes="assumed 1 hour duration, none stated",
    )
    text = build_event_ical(timed)
    vevent, alarms = parse(text)

    check("one alarm attached", len(alarms) == 1, f"got {len(alarms)}")
    if alarms:
        trigger = alarms[0].get("trigger").dt
        check("trigger is 30 min BEFORE the start", trigger == timedelta(minutes=-30), f"got {trigger}")
        check("alarm action is DISPLAY", str(alarms[0].get("action")) == "DISPLAY")
    start = vevent.get("dtstart").dt
    end = vevent.get("dtend").dt
    check("start kept as an exact moment in time", isinstance(start, datetime))
    check(
        "start is the same instant as 13:00 Singapore",
        start == datetime(2026, 9, 11, 5, 0, tzinfo=timezone.utc),
        f"got {start}",
    )
    check("no end time given, so 1 hour assumed", end - start == timedelta(hours=1), f"got {end - start}")
    check("location carried through", str(vevent.get("location")) == "Toast Box")
    check("notes carried through as the description", "assumed 1 hour" in str(vevent.get("description")))
    check("written as UTC, so no timezone block is needed", "DTSTART:20260911T050000Z" in text)

    print("\n2. All-day event, default alarm (should be 09:00 on the day)")
    allday = FakeEvent(
        title="Mum's birthday",
        start_datetime="2026-10-05T00:00:00+08:00",
        all_day=True,
    )
    text = build_event_ical(allday)
    vevent, alarms = parse(text)

    check("one alarm attached", len(alarms) == 1, f"got {len(alarms)}")
    if alarms:
        trigger = alarms[0].get("trigger").dt
        check("trigger is 9 hours after midnight (= 09:00)", trigger == timedelta(hours=9), f"got {trigger}")
    start = vevent.get("dtstart").dt
    end = vevent.get("dtend").dt
    check("all-day start is a plain date, not a time", not isinstance(start, datetime))
    check("all-day end is the next day", (end - start) == timedelta(days=1), f"got {end - start}")
    check("marked as a date value in the raw text", "DTSTART;VALUE=DATE:20261005" in text)

    print("\n3. Alarms can be switched off per event")
    text = build_event_ical(timed, alarm_offset=None)
    _, alarms = parse(text)
    check("no alarm when alarm_offset=None", len(alarms) == 0, f"got {len(alarms)}")

    print("\n4. A custom alarm offset is respected")
    text = build_event_ical(timed, alarm_offset=timedelta(minutes=-5))
    _, alarms = parse(text)
    check("trigger is 5 min before", alarms and alarms[0].get("trigger").dt == timedelta(minutes=-5))

    print("\n5. Every event still gets the required boilerplate")
    check("has a UID", "UID:" in text)
    check("has a DTSTAMP", "DTSTAMP:" in text)
    check("declares iCalendar 2.0", "VERSION:2.0" in text)

    print("\n" + "=" * 70)
    if failures:
        print(f"{len(failures)} of {checks_run} checks FAILED: {failures}")
        raise SystemExit(1)
    print(f"All {checks_run} checks passed. The iCalendar we generate is well-formed.")
    print("Next: test_alarms.py, which answers the separate question of whether")
    print("iCloud and your iPhone actually act on the alarms we send.")


if __name__ == "__main__":
    main()
