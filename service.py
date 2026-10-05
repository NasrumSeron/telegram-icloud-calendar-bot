"""
service.py — the Butler adapter for this calendar bot.

WHAT THIS IS
    A small HTTP server exposing the three endpoints in Butler's CONTRACT.md,
    so Butler (a separate router bot) can create calendar events on your behalf.

WHAT IT IS NOT
    A replacement for bot.py. It is a SECOND, separate entry point. bot.py is
    not imported here and not modified in any way. Your Telegram calendar bot
    keeps working exactly as it does today, whether or not this is running.

    Run bot.py       -> your existing Telegram bot
    Run service.py   -> the door Butler knocks on
    Both at once     -> what docker-compose.yml does. They share nothing but
                        the .env file and the three modules below.

WHY A SEPARATE PROCESS
    bot.py runs python-telegram-bot's async event loop. Bolting a web server
    into that loop means one can break the other. Two processes from the same
    image cost almost nothing and cannot take each other down.

WHAT IT REUSES (the "write layer" — no Telegram code in any of them)
    gemini_parse.parse_event_text   text        -> ParsedEvent
    draft.Draft                     ParsedEvent -> a reviewable draft
    caldav_write.create_calendar_event          -> writes to iCloud

    Exactly the modules caldav_write.py's docstring predicted would be
    "reusable by anything later, Butler included".

Run standalone:  python service.py [port] [host]
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from dotenv import load_dotenv

from caldav_write import create_calendar_event, list_calendar_names
from draft import (
    ALERT_CHOICES,
    ALL_DAY_ALERT_CHOICES,
    DURATION_CHOICES,
    Draft,
)
from gemini_parse import parse_event_text

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s calendar-api  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("calendar-api")

SERVICE_NAME = "calendar"
SERVICE_VERSION = "2.0-butler"

# Input limits (Gate G #14). 4096 = Telegram's message length limit.
MAX_BODY = 16_384
MAX_TEXT = 4096
MAX_NAME = 64
_DRAIN_LIMIT = 65_536       # unread body we swallow so the caller still sees our reply
BAD_REQUEST = "Bad request."
INTERNAL_ERROR = "Calendar hit an internal error."


class BadRequest(Exception):
    """Malformed input. The detail never reaches the caller."""

# EDIT ME: how long an unconfirmed draft survives before it is forgotten.
DRAFT_TTL_SECONDS = 900

# EDIT ME: how long Butler should hold the conversation open for you, in
# seconds. Keep it below DRAFT_TTL_SECONDS.
FOLLOWUP_SECONDS = 600

# Per-person default calendar, same .env variable bot.py already reads.
_defaults_raw = os.environ.get("DEFAULT_CALENDAR_MAP", "").strip()
DEFAULT_CALENDAR_BY_USER: dict[int, str] = {}
for _pair in _defaults_raw.split(","):
    if ":" in _pair:
        _uid, _cal = _pair.split(":", 1)
        try:
            DEFAULT_CALENDAR_BY_USER[int(_uid.strip())] = _cal.strip()
        except ValueError:
            log.warning("Ignoring malformed DEFAULT_CALENDAR_MAP entry: %r", _pair)


# ---------------------------------------------------------------- draft store

_LOCK = threading.Lock()
DRAFTS: dict[str, dict[str, Any]] = {}   # draft_id -> {draft, user_id, created}


def _put_draft(draft: Draft, user_id: int) -> str:
    draft_id = uuid.uuid4().hex[:8]
    with _LOCK:
        _expire_old()
        DRAFTS[draft_id] = {"draft": draft, "user_id": user_id, "created": time.time()}
    return draft_id


def _get_draft(draft_id: str, user_id: int) -> Optional[Draft]:
    with _LOCK:
        _expire_old()
        entry = DRAFTS.get(draft_id)
    # A draft belongs to the person who started it. Butler is single-user today,
    # but this bot already supports two people and will outlive that assumption.
    if not entry or entry["user_id"] != user_id:
        return None
    return entry["draft"]


def _drop_draft(draft_id: str) -> None:
    with _LOCK:
        DRAFTS.pop(draft_id, None)


def _expire_old() -> None:
    cutoff = time.time() - DRAFT_TTL_SECONDS
    for key in [k for k, v in DRAFTS.items() if v["created"] < cutoff]:
        DRAFTS.pop(key, None)


# ---------------------------------------------------------------- calendars

_calendar_cache: list[str] = []


def calendars() -> list[str]:
    global _calendar_cache
    if not _calendar_cache:
        _calendar_cache = list_calendar_names()
        log.info("Loaded %d calendars from iCloud", len(_calendar_cache))
    return _calendar_cache


# ---------------------------------------------------------------- the actions

def _card_and_followup(draft: Draft, draft_id: str, user_id: int) -> dict[str, Any]:
    """Render the draft and describe what Butler should offer next.

    The reply text is this bot's own review card, verbatim — Butler does not
    rewrite it and no LLM sits between it and the screen.
    """
    buttons = _buttons_for(draft, draft_id, user_id)

    return {
        "ok": True,
        "reply": "```\n" + draft.render_card() + "\n```",
        "data": {"draft_id": draft_id, "ready": draft.ready(),
                 "missing": draft.missing()},
        "followup": {
            # Anything you type while this is open is treated as a correction.
            "action": "amend_draft",
            "params": {"draft_id": draft_id},
            "expires_in": FOLLOWUP_SECONDS,
            "buttons": buttons,
        },
    }


def _buttons_for(draft: Draft, draft_id: str, user_id: int) -> list[dict[str, Any]]:
    """The buttons under the card.

    Mirrors bot.py's build_keyboard: a submenu REPLACES the main set while it
    is open. Without this, alert / duration / calendar were unreachable once a
    draft existed — a bug found on the first deploy of this adapter. Typing cannot fix
    them either, because they live on Draft, not on the ParsedEvent that Gemini
    re-parses.
    """
    d = {"draft_id": draft_id}
    back = {"label": "< Back", "action": "open_menu",
            "params": {**d, "menu": ""}}

    if draft.open_menu == "calendar":
        return [{"label": name, "action": "set_calendar",
                 "params": {**d, "calendar": name}}
                for name in _calendars_for(user_id)] + [back]

    if draft.open_menu == "alert":
        choices = ALL_DAY_ALERT_CHOICES if draft.is_all_day else ALERT_CHOICES
        return [{"label": label, "action": "set_alert",
                 "params": {**d, "value": value}}
                for label, value in choices] + [back]

    if draft.open_menu == "duration":
        return [{"label": label, "action": "set_duration",
                 "params": {**d, "minutes": minutes}}
                for label, minutes in DURATION_CHOICES] + [back]

    # --- the main set -------------------------------------------------------
    buttons: list[dict[str, Any]] = []

    if draft.ready():
        buttons.append({"label": "Add to calendar", "action": "confirm_event",
                        "params": d})

    buttons.append({"label": f"Calendar: {draft.calendar or 'choose'}",
                    "action": "open_menu", "params": {**d, "menu": "calendar"}})
    buttons.append({"label": f"Alert: {draft.describe_alert()}",
                    "action": "open_menu", "params": {**d, "menu": "alert"}})

    # An all-day event has no duration to set.
    if not draft.is_all_day:
        buttons.append({"label": f"Duration: {draft.describe_duration()}",
                        "action": "open_menu", "params": {**d, "menu": "duration"}})
    else:
        buttons.append({"label": "Make it timed", "action": "open_menu",
                        "params": {**d, "menu": "duration"}})

    if "location" in draft.missing():
        buttons.append({"label": "No location", "action": "skip_location",
                        "params": d})

    buttons.append({"label": "Cancel", "action": "cancel_draft", "params": d})
    return buttons


def _calendars_for(user_id: int) -> list[str]:
    """Calendar names, with this person's usual one first."""
    names = list(calendars())
    preferred = DEFAULT_CALENDAR_BY_USER.get(user_id)
    if preferred and preferred in names:
        names.remove(preferred)
        names.insert(0, preferred)
    return names[:5]


def action_draft_event(params: dict, user_id: int, original: str) -> dict:
    text = str(params.get("text") or original).strip()
    if not text:
        return {"ok": False, "error": "There was no text to read an event from."}

    parsed = parse_event_text(text)
    draft = Draft(event=parsed)

    # Pre-fill the calendar if this person has a usual one AND it still exists.
    preferred = DEFAULT_CALENDAR_BY_USER.get(user_id)
    if preferred and preferred in calendars():
        draft.calendar = preferred

    draft_id = _put_draft(draft, user_id)
    log.info("draft %s created for user %s: %r", draft_id, user_id, parsed.title)
    return _card_and_followup(draft, draft_id, user_id)


def action_amend_draft(params: dict, user_id: int, original: str) -> dict:
    draft_id = str(params.get("draft_id") or "")
    draft = _get_draft(draft_id, user_id)
    if draft is None:
        return {"ok": False, "error": "That draft has expired. Send the event again."}

    text = str(params.get("text") or original).strip()
    if not text:
        return {"ok": False, "error": "Nothing to change."}

    # Typing "change the alert" used to go to Gemini, which re-parses the EVENT
    # and cannot touch alert / calendar / duration — they live on Draft. So the
    # message appeared to do nothing. Now it opens the right menu instead.
    # This check must come FIRST, or a missing location would swallow it.
    menu = _menu_intent(text)
    if menu:
        draft.open_menu = menu
        out = _card_and_followup(draft, draft_id, user_id)
        out["reply"] += f"\n_Pick a {menu} below — typing can't change that one._"
        return out

    if draft.awaiting == "location" or "location" in draft.missing():
        # A bare reply while the location is missing is the location itself.
        # Anything that looks like a time change still goes to the parser.
        if not _looks_like_a_time_change(text):
            draft.event.location = text
            draft.awaiting = None
            log.info("draft %s: location set to %r", draft_id, text)
            return _card_and_followup(draft, draft_id, user_id)

    # Reuse the same "is this a correction or a new event?" logic bot.py uses.
    draft.event = parse_event_text(text, pending=draft.event)
    log.info("draft %s amended: %r", draft_id, draft.event.title)
    return _card_and_followup(draft, draft_id, user_id)


def _menu_intent(text: str) -> Optional[str]:
    """Is this typed message trying to change a button-only field?

    Deliberately narrow. These words must be about the SETTING, not about the
    event, so "remind me to call the dean" is not caught — that has no alert
    word plus a change word. False positives here would hijack a real edit.
    """
    low = text.lower()
    change_words = ("change", "set", "make it", "switch", "use", "move to",
                    "no ", "turn off", "instead", "different", "put it")
    if not any(w in low for w in change_words):
        return None

    if any(w in low for w in ("alert", "alarm", "remind", "notification", "notify")):
        return "alert"
    if "calendar" in low or "cal " in low:
        return "calendar"
    if any(w in low for w in ("duration", "how long", "all day", "all-day")):
        return "duration"
    return None


def _looks_like_a_time_change(text: str) -> bool:
    lowered = text.lower()
    clues = ("am", "pm", ":", "tomorrow", "today", "next", "week", "month",
             "monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday", "hour", "min", "all day", "move", "make it")
    return any(clue in lowered for clue in clues)


def action_open_menu(params: dict, user_id: int, original: str) -> dict:
    """Swap the buttons for a submenu (or back to the main set with menu='')."""
    draft_id = str(params.get("draft_id") or "")
    draft = _get_draft(draft_id, user_id)
    if draft is None:
        return {"ok": False, "error": "That draft has expired. Send the event again."}

    menu = str(params.get("menu") or "") or None
    if menu not in (None, "calendar", "alert", "duration"):
        return {"ok": False, "error": f"There's no '{menu}' menu."}

    draft.open_menu = menu
    return _card_and_followup(draft, draft_id, user_id)


def action_set_calendar(params: dict, user_id: int, original: str) -> dict:
    draft_id = str(params.get("draft_id") or "")
    draft = _get_draft(draft_id, user_id)
    if draft is None:
        return {"ok": False, "error": "That draft has expired. Send the event again."}

    name = str(params.get("calendar") or "")
    if name not in calendars():
        return {"ok": False, "error": f"No calendar named '{name}' on this account."}

    draft.calendar = name
    draft.open_menu = None          # close the submenu after choosing
    return _card_and_followup(draft, draft_id, user_id)


def action_set_alert(params: dict, user_id: int, original: str) -> dict:
    draft_id = str(params.get("draft_id") or "")
    draft = _get_draft(draft_id, user_id)
    if draft is None:
        return {"ok": False, "error": "That draft has expired. Send the event again."}

    raw = params.get("value")
    value = None if raw in (None, "", "None") else int(raw)

    # Two different fields, because an all-day alarm is an hour-of-day and a
    # timed one is minutes-before. draft.alarm_offset() reads whichever applies.
    if draft.is_all_day:
        draft.all_day_alert_hour = value
    else:
        draft.alert_minutes = value

    # alert_chosen is what tells Draft to stop using the module default —
    # without it, picking "No alert" would silently fall back to 30-min-before.
    draft.alert_chosen = True
    draft.open_menu = None
    return _card_and_followup(draft, draft_id, user_id)


def action_set_duration(params: dict, user_id: int, original: str) -> dict:
    draft_id = str(params.get("draft_id") or "")
    draft = _get_draft(draft_id, user_id)
    if draft is None:
        return {"ok": False, "error": "That draft has expired. Send the event again."}

    raw = params.get("minutes")
    minutes = None if raw in (None, "", "None") else int(raw)

    if minutes is None:
        # "All day" in DURATION_CHOICES. Switching kinds invalidates whichever
        # alert was chosen, so drop back to that kind's default.
        draft.event.all_day = True
        draft.duration_minutes = None
        draft.alert_chosen = False
    else:
        was_all_day = draft.is_all_day
        draft.event.all_day = False
        draft.duration_minutes = minutes
        if was_all_day:
            draft.alert_chosen = False

    draft.open_menu = None
    return _card_and_followup(draft, draft_id, user_id)


def action_skip_location(params: dict, user_id: int, original: str) -> dict:
    draft_id = str(params.get("draft_id") or "")
    draft = _get_draft(draft_id, user_id)
    if draft is None:
        return {"ok": False, "error": "That draft has expired. Send the event again."}

    draft.location_skipped = True
    draft.awaiting = None
    return _card_and_followup(draft, draft_id, user_id)


def action_confirm_event(params: dict, user_id: int, original: str) -> dict:
    draft_id = str(params.get("draft_id") or "")
    draft = _get_draft(draft_id, user_id)
    if draft is None:
        return {"ok": False, "error": "That draft has expired. Send the event again."}

    if not draft.ready():
        return {"ok": False,
                "error": f"Still need: {', '.join(draft.missing())}."}

    url = create_calendar_event(
        draft.to_event(),
        calendar_name=draft.calendar,
        alarm_offset=draft.alarm_offset(),
    )
    _drop_draft(draft_id)
    log.info("draft %s written to %r", draft_id, draft.calendar)

    when = (draft.start.strftime("%a %d %b")
            if draft.is_all_day
            else draft.start.strftime("%a %d %b, %-I:%M %p"))
    return {
        "ok": True,
        "reply": (f"Added *{draft.event.title}* — {when}\n"
                  f"Calendar: {draft.calendar}   Alert: {draft.describe_alert()}"),
        "data": {"event_url": str(url)},
        # No followup key: the conversation is over, Butler releases its lock.
    }


def action_cancel_draft(params: dict, user_id: int, original: str) -> dict:
    _drop_draft(str(params.get("draft_id") or ""))
    return {"ok": True, "reply": "Dropped it. Nothing was added to your calendar."}


ACTIONS = {
    "draft_event": action_draft_event,
    "amend_draft": action_amend_draft,
    "open_menu": action_open_menu,
    "set_calendar": action_set_calendar,
    "set_alert": action_set_alert,
    "set_duration": action_set_duration,
    "skip_location": action_skip_location,
    "confirm_event": action_confirm_event,
    "cancel_draft": action_cancel_draft,
}

CAPABILITIES = {
    "name": SERVICE_NAME,
    "description": (
        "Creates events on the user's iCloud calendars from natural language, with a "
        "review step before anything is written. Handles anything with a date, "
        "time or deadline attached — meetings, appointments, lunches, blocked-out "
        "time, reminders tied to a specific day. Does NOT track money, news, "
        "habits or files."
    ),
    "keywords": ["calendar", "event", "meeting", "appointment", "schedule",
                 "reschedule", "book", "lunch", "dinner", "birthday"],
    "actions": [
        {
            "name": "draft_event",
            "description": (
                "Read an event out of a message and show it for review before "
                "writing it. This is the only action a person asks for directly."
            ),
            "params": {"text": "The user's original message, verbatim."},
        },
        # Everything below is reachable only as a follow-up to a draft.
        # internal=true keeps them out of Butler's routing menu — otherwise the
        # routing model sees six actions and starts picking the wrong ones.
        {"name": "amend_draft", "internal": True,
         "description": "Apply a typed correction to an open draft.",
         "params": {"draft_id": "The draft being corrected.",
                    "text": "What the user typed."}},
        {"name": "open_menu", "internal": True,
         "description": "Show a submenu of choices (calendar, alert, duration).",
         "params": {"draft_id": "The draft.",
                    "menu": "calendar, alert, duration, or empty to go back."}},
        {"name": "set_calendar", "internal": True,
         "description": "Choose which calendar an open draft writes to.",
         "params": {"draft_id": "The draft.", "calendar": "Calendar name."}},
        {"name": "set_alert", "internal": True,
         "description": "Set an open draft's alarm.",
         "params": {"draft_id": "The draft.",
                    "value": "Minutes before (timed) or hour of day (all-day); null for none."}},
        {"name": "set_duration", "internal": True,
         "description": "Set how long an open draft runs, or make it all-day.",
         "params": {"draft_id": "The draft.",
                    "minutes": "Length in minutes, or null for an all-day event."}},
        {"name": "skip_location", "internal": True,
         "description": "Mark an open draft as having no location.",
         "params": {"draft_id": "The draft."}},
        {"name": "confirm_event", "internal": True,
         "description": "Write an open draft to iCloud.",
         "params": {"draft_id": "The draft to write."}},
        {"name": "cancel_draft", "internal": True,
         "description": "Throw away an open draft.",
         "params": {"draft_id": "The draft to discard."}},
    ],
}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._json(200, {"ok": True, "name": SERVICE_NAME,
                                    "version": SERVICE_VERSION})
        if self.path == "/capabilities":
            return self._json(200, CAPABILITIES)
        return self._json(404, {"ok": False, "error": "no such endpoint"})

    def _body(self) -> dict:
        """Parse the JSON object body, or raise BadRequest (never returns a non-dict)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest()
        if length < 0:
            raise BadRequest()
        if length > MAX_BODY:
            if length <= _DRAIN_LIMIT:
                self.rfile.read(length)
            raise BadRequest()
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, RecursionError):      # JSONDecodeError + bad UTF-8
            raise BadRequest()
        if not isinstance(body, dict):
            raise BadRequest()
        return body

    def do_POST(self):  # noqa: N802
        if self.path != "/invoke":
            return self._json(404, {"ok": False, "error": "no such endpoint"})

        action = ""
        try:
            body = self._body()
            action = str(body.get("action") or "")
            if len(action) > MAX_NAME:
                raise BadRequest()
            handler = ACTIONS.get(action)
            if handler is None:
                return self._json(200, {"ok": False, "error": f"unknown action '{action}'"})

            params = body.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise BadRequest()
            try:
                user_id = int(body.get("user_id") or 0)
            except (TypeError, ValueError, OverflowError):
                raise BadRequest()
            original = str(body.get("original_message") or "")
            if len(original) > MAX_TEXT or any(
                    isinstance(v, str) and len(v) > MAX_TEXT for v in params.values()):
                raise BadRequest()

            result = handler(params, user_id, original)
        except BadRequest:
            return self._json(200, {"ok": False, "error": BAD_REQUEST})
        except Exception:  # noqa: BLE001
            # Never let a traceback reach Telegram, but always log it in full.
            log.exception("action %s failed", action)
            return self._json(200, {"ok": False, "error": INTERNAL_ERROR})

        return self._json(200, result)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"

    for required in ("GEMINI_API_KEY", "ICLOUD_APPLE_ID", "ICLOUD_APP_SPECIFIC_PASSWORD"):
        if not os.environ.get(required):
            raise SystemExit(f"{required} is not set — check your .env file.")

    log.info("calendar API listening on http://%s:%s", host, port)
    log.info("actions: %s", ", ".join(ACTIONS))
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
