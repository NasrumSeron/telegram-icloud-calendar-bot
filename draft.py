"""
The in-progress event a person is building in chat: what they said, plus the
choices they've made with buttons since.

This lives in its own file for the same reason gemini_parse.py and
caldav_write.py do -- it has no Telegram code in it at all, so the fiddly
logic (what's missing, how long is it, when does the alarm fire) can be
tested on its own, without pretending to be a chat.

bot.py owns the conversation. This file owns the state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from gemini_parse import ParsedEvent

# EDIT ME: assumed length when the message doesn't say ("lunch at 1pm").
DEFAULT_DURATION_MINUTES = 60

# EDIT ME: default alarm for a timed event, in minutes before the start.
# None means no alarm.
DEFAULT_ALERT_MINUTES = 30

# EDIT ME: default alarm for an all-day event -- hours after midnight,
# so 9 means 09:00 on the day itself. None means no alarm.
DEFAULT_ALL_DAY_ALERT_HOUR = 9

# EDIT ME: the choices offered by the Duration button.
DURATION_CHOICES = [
    ("30 min", 30),
    ("1 hour", 60),
    ("90 min", 90),
    ("2 hours", 120),
    ("3 hours", 180),
    ("All day", None),  # None here means "make it an all-day event"
]

# EDIT ME: the choices offered by the Alert button for a TIMED event.
ALERT_CHOICES = [
    ("No alert", None),
    ("10 min before", 10),
    ("30 min before", 30),
    ("1 hour before", 60),
    ("2 hours before", 120),
    ("1 day before", 1440),
]

# EDIT ME: the choices offered by the Alert button for an ALL-DAY event.
# Values are hours relative to midnight at the start of the day, so
# -6 means 18:00 the evening before.
ALL_DAY_ALERT_CHOICES = [
    ("No alert", None),
    ("09:00 on the day", 9),
    ("18:00 day before", -6),
    ("09:00 day before", -15),
]


@dataclass
class Draft:
    """One person's unconfirmed event, mid-build."""

    event: ParsedEvent
    calendar: Optional[str] = None
    duration_minutes: Optional[int] = None  # None = fall back to the default
    alert_minutes: Optional[int] = None  # timed events: minutes before
    all_day_alert_hour: Optional[int] = None  # all-day events: hours from midnight
    alert_chosen: bool = False  # has the person actively picked an alert?
    location_skipped: bool = False  # did they explicitly say "no location"?
    awaiting: Optional[str] = None  # "location" while we wait for them to type one
    card_message_id: Optional[int] = None  # the card we keep editing in place
    open_menu: Optional[str] = None  # which submenu is showing: "duration"/"alert"/None

    # ---------- derived values ----------

    @property
    def start(self) -> datetime:
        return datetime.fromisoformat(self.event.start_datetime)

    @property
    def is_all_day(self) -> bool:
        return self.event.all_day

    def duration(self) -> timedelta:
        """How long the event runs. Uses, in order: an explicit button choice,
        an end time Gemini worked out, then the default."""
        if self.duration_minutes is not None:
            return timedelta(minutes=self.duration_minutes)
        if self.event.end_datetime:
            return datetime.fromisoformat(self.event.end_datetime) - self.start
        return timedelta(minutes=DEFAULT_DURATION_MINUTES)

    def end(self) -> datetime:
        return self.start + self.duration()

    def alarm_offset(self) -> Optional[timedelta]:
        """The alarm, expressed the way caldav_write wants it: a timedelta
        relative to the start. Negative = before the start."""
        if self.is_all_day:
            hour = self.all_day_alert_hour if self.alert_chosen else DEFAULT_ALL_DAY_ALERT_HOUR
            return None if hour is None else timedelta(hours=hour)
        minutes = self.alert_minutes if self.alert_chosen else DEFAULT_ALERT_MINUTES
        return None if minutes is None else timedelta(minutes=-minutes)

    def missing(self) -> list[str]:
        """Fields the person asked to be prompted for. Confirm stays blocked
        until this list is empty.

        Duration and alert are deliberately NOT in here -- they always have a
        sensible default, and can be changed with one tap if the default is
        wrong. Calendar and location were chosen (2026-09-07) as the two worth
        stopping for.
        """
        gaps = []
        if not self.calendar:
            gaps.append("calendar")
        if not self.event.location and not self.location_skipped:
            gaps.append("location")
        return gaps

    def ready(self) -> bool:
        return not self.missing() and self.awaiting is None

    # ---------- rendering ----------

    def describe_alert(self) -> str:
        offset = self.alarm_offset()
        if offset is None:
            return "none"
        if self.is_all_day:
            hours = offset.total_seconds() / 3600
            if hours >= 0:
                return f"{int(hours):02d}:00 on the day"
            fire = self.start + offset
            return fire.strftime("%H:%M the day before") if hours > -24 else fire.strftime("%a %d %b, %H:%M")
        minutes = int(-offset.total_seconds() // 60)
        if minutes % 1440 == 0:
            return f"{minutes // 1440} day before"
        if minutes % 60 == 0:
            return f"{minutes // 60} hour{'s' if minutes > 60 else ''} before"
        return f"{minutes} min before"

    def describe_duration(self) -> str:
        if self.is_all_day:
            return "all day"
        total = int(self.duration().total_seconds() // 60)
        hours, minutes = divmod(total, 60)
        if hours and minutes:
            return f"{hours}h {minutes}m"
        if hours:
            return f"{hours} hour{'s' if hours > 1 else ''}"
        return f"{minutes} min"

    def render_card(self) -> str:
        """The review card: every field at once, filled or flagged."""
        lines = [f"Title:     {self.event.title}"]

        if self.is_all_day:
            lines.append(f"When:      {self.start.strftime('%a %d %b %Y')} (all day)")
        else:
            end = self.end()
            same_day = end.date() == self.start.date()
            end_str = end.strftime("%I:%M %p") if same_day else end.strftime("%a %d %b, %I:%M %p")
            lines.append(
                f"When:      {self.start.strftime('%a %d %b %Y, %I:%M %p')} – {end_str}"
                f"  ({self.describe_duration()})"
            )

        lines.append(f"Where:     {self.event.location or ('(none)' if self.location_skipped else '— needed —')}")
        lines.append(f"Alert:     {self.describe_alert()}")
        lines.append(f"Calendar:  {self.calendar or '— needed —'}")

        if self.event.notes:
            lines.append(f"Note:      {self.event.notes}")

        lines.append("")
        if self.awaiting == "location":
            lines.append("Send me the location as a message, or tap Skip.")
        elif self.missing():
            lines.append(f"Still needed: {', '.join(self.missing())}. Use the buttons below.")
        else:
            lines.append("Looks right? Tap Add to calendar.")
        lines.append("You can also just type a correction, e.g. \"make it 2pm\".")
        return "\n".join(lines)

    def to_event(self) -> ParsedEvent:
        """A ParsedEvent reflecting every choice made, ready to be written."""
        return ParsedEvent(
            title=self.event.title,
            start_datetime=self.event.start_datetime,
            end_datetime=None if self.is_all_day else self.end().isoformat(),
            location=self.event.location,
            all_day=self.is_all_day,
            notes=self.event.notes,
        )
