"""
Run this to check Gemini's parsing quality against a 10-phrase gold-standard
set, BEFORE we wire parsing into the actual bot.

Usage:
    python tests/live/test_gemini_parsing.py            # run all phrases
    python tests/live/test_gemini_parsing.py 1 9 10     # run only phrases #1, #9, #10
                                              # (numbers match the [N] shown in output)

Each phrase's expected date is computed fresh from TODAY (whenever you run
this), not hardcoded — so this stays valid whenever you resume, no manual
recalculation needed. For each phrase it prints what Gemini actually
returned next to what was expected, and a PASS/CHECK marker for the date
part (title/location wording always needs a human eyeball).

EDIT ME: TEST_PHRASES below is a plain list — add, remove, or reword entries
yourself with a text editor. If you add a phrase, also add a matching entry
to EXPECTED_FN (or just leave its expected fn as None to skip auto-checking
and eyeball it manually).
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..", "..")))

if not _os.environ.get("RUN_LIVE_TESTS"):
    print("SKIPPED: live test (real network / Gemini / iCloud). Set RUN_LIVE_TESTS=1 and fill .env to run it.")
    raise SystemExit(0)

import sys
import time
from datetime import date, datetime, timedelta

from gemini_parse import SINGAPORE_TZ, parse_event_text

TODAY = datetime.now(SINGAPORE_TZ).date()

# EDIT ME: pause between test phrases so this script alone can't trip a
# per-minute rate limit. Raise this if you still see 429 errors partway
# through a run.
SECONDS_BETWEEN_REQUESTS = 8


def upcoming(base: date, target_weekday: int, *, include_today: bool = True) -> date:
    """Next date on/after `base` matching target_weekday (0=Mon..6=Sun).

    Used for "this X" phrasing: the soonest occurrence, including today if it
    matches ("this Thursday" said on a Thursday means today).
    """
    days_ahead = (target_weekday - base.weekday()) % 7
    if days_ahead == 0 and not include_today:
        days_ahead = 7
    return base + timedelta(days=days_ahead)


def next_week_weekday(base: date, target_weekday: int) -> date:
    """CONFIRMED CONVENTION (2026-08-27): "next X" always means X in the
    calendar week AFTER the current one (Mon-Sun weeks) -- even if X hasn't
    happened yet in the current week. E.g. said on a Wednesday, "next Friday"
    is next week's Friday (9 days away), not this week's (2 days away).
    """
    this_monday = base - timedelta(days=base.weekday())
    next_monday = this_monday + timedelta(days=7)
    return next_monday + timedelta(days=target_weekday)


def in_n_weeks_weekday(base: date, n: int, target_weekday: int) -> date:
    """CONFIRMED CONVENTION (2026-08-28): "in N weeks on <weekday>" is
    calendar-week anchored, same as "next X" -- advance N calendar weeks
    from THIS week's Monday, then take target_weekday within that week.
    NOT a literal "base + N*7 days, nearest weekday" calculation (that gave
    a different, wrong answer in testing).
    """
    this_monday = base - timedelta(days=base.weekday())
    target_monday = this_monday + timedelta(weeks=n)
    return target_monday + timedelta(days=target_weekday)


def end_of_month(base: date) -> date:
    if base.month == 12:
        return date(base.year, 12, 31)
    return date(base.year, base.month + 1, 1) - timedelta(days=1)


# Each entry: (phrase, human-readable expected description, expected_date or None)
# expected_date is just the DATE part -- times/locations still need your eyeball.
TEST_PHRASES = [
    (
        "Lunch with John next Friday at 1pm at Toast Box",
        "Friday of NEXT calendar week, 13:00, location Toast Box",
        next_week_weekday(TODAY, 4),  # 4 = Friday
    ),
    (
        "Dentist appointment tomorrow 10am",
        "tomorrow, 10:00",
        TODAY + timedelta(days=1),
    ),
    (
        "Team meeting on Sept 3 from 2 to 3pm",
        "Sept 3 (this year if not passed yet, else next year), 14:00-15:00",
        date(TODAY.year, 9, 3) if date(TODAY.year, 9, 3) >= TODAY else date(TODAY.year + 1, 9, 3),
    ),
    (
        "Mom's birthday on 5 October",
        "Oct 5 (this year if not passed yet, else next year), all-day",
        date(TODAY.year, 10, 5) if date(TODAY.year, 10, 5) >= TODAY else date(TODAY.year + 1, 10, 5),
    ),
    (
        "Flight to Bangkok next Monday 6:45am",
        "Monday of NEXT calendar week, 06:45",
        next_week_weekday(TODAY, 0),  # 0 = Monday
    ),
    (
        "Call with client this Thursday 4pm for 30 mins",
        "soonest Thursday (today counts), 16:00-16:30",
        upcoming(TODAY, 3, include_today=True),  # 3 = Thursday
    ),
    (
        "Weekend trip to Malacca from Saturday to Sunday",
        "soonest Sat->Sun (today counts if it's already Sat), all-day",
        upcoming(TODAY, 5, include_today=True),  # 5 = Saturday
    ),
    (
        "Pick up parcel at post office sometime tomorrow afternoon",
        "tomorrow, NO exact time -- should flag ambiguity in notes, not invent one",
        TODAY + timedelta(days=1),
    ),
    (
        "Coffee with Sarah in 2 weeks on Tuesday",
        "Tuesday of the week 2 calendar-weeks from this one",
        in_n_weeks_weekday(TODAY, 2, 1),  # 1 = Tuesday
    ),
    (
        "Reminder to submit grades by end of this month",
        "last day of the current month, treated as all-day/deadline",
        end_of_month(TODAY),
    ),
]


def main() -> None:
    # Optional: pass phrase numbers to run only those, e.g. `python test_gemini_parsing.py 1 9 10`
    if len(sys.argv) > 1:
        wanted = {int(n) for n in sys.argv[1:]}
        selected = [(i, p) for i, p in enumerate(TEST_PHRASES, start=1) if i in wanted]
    else:
        selected = list(enumerate(TEST_PHRASES, start=1))

    print(f"Running against TODAY = {TODAY.isoformat()} ({TODAY.strftime('%A')}, Asia/Singapore)")
    print(f"Running {len(selected)} of {len(TEST_PHRASES)} phrases: {[i for i, _ in selected]}\n")
    for idx, (i, (phrase, expected_desc, expected_date)) in enumerate(selected):
        if idx > 0:
            print(f"\n(pausing {SECONDS_BETWEEN_REQUESTS}s to avoid tripping a per-minute rate limit...)", flush=True)
            time.sleep(SECONDS_BETWEEN_REQUESTS)
        print(f"{'=' * 70}")
        print(f"[{i}] Input: {phrase}")
        print(f"    Expected: {expected_desc}  (date: {expected_date.isoformat()})")
        try:
            result = parse_event_text(phrase)
            got_date = result.start_datetime[:10]  # YYYY-MM-DD prefix
            marker = "PASS (date matches)" if got_date == expected_date.isoformat() else "CHECK (date differs -- read carefully)"
            print(f"    -> {marker}")
            print("    " + result.model_dump_json(indent=2).replace("\n", "\n    "))
        except Exception as exc:  # noqa: BLE001 - test script, want to see all failures
            print(f"    ERROR parsing this phrase: {exc}")
            if "429" in str(exc):
                print(
                    "\nHit a 429 (quota exceeded) -- stopping the run here instead of "
                    "burning more requests against an already-tripped limit. Check the "
                    "error text above for a 'quota_metric' field: one mentioning "
                    "per-minute limits clears in ~60s (just re-run); one mentioning "
                    "daily/free-tier limits needs a wait until reset "
                    "(midnight Pacific Time -- check aistudio.google.com/rate-limit for specifics).",
                    flush=True,
                )
                break


if __name__ == "__main__":
    main()
