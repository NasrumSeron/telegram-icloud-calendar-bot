"""
Telegram front end for the calendar bot.

v2 flow (the review card):

  You type an event in plain English
    -> Gemini parses it (one API call -- the only slow step)
    -> the bot shows a REVIEW CARD: every field at once, filled in or
       flagged as needed
    -> you adjust anything with buttons (duration, alert, calendar,
       location) or by typing a correction in plain English
    -> "Add to calendar" writes it, with your chosen calendar and alarm

Everything after the first message is local: button taps don't cost a Gemini
call, so the back-and-forth is instant.

Confirm is deliberately blocked until a calendar and a location are settled
(2026-09-07 decision). Duration and alert always have sensible defaults and
never block -- change them with one tap if the default is wrong.

Who does what:
  gemini_parse.py -- text -> ParsedEvent
  draft.py        -- the unconfirmed event and everything derived from it
  caldav_write.py -- ParsedEvent -> a real iCloud event
  this file       -- the conversation only
"""

import asyncio
import logging
import os
from typing import Optional

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

# EDIT ME (in .env): comma-separated Telegram user IDs allowed to use this bot.
_allowed_ids_raw = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
ALLOWED_USER_IDS = {int(x.strip()) for x in _allowed_ids_raw.split(",") if x.strip()}

# EDIT ME (in .env): each person's usual calendar, so their own is offered first.
# Format: "111111111:Bot Events,222222222:Family". You are still asked to tap it --
# this only decides which button sits at the front of the row.
_defaults_raw = os.environ.get("DEFAULT_CALENDAR_MAP", "").strip()
DEFAULT_CALENDAR_BY_USER: dict[int, str] = {}
for _pair in _defaults_raw.split(","):
    if ":" in _pair:
        _uid, _cal = _pair.split(":", 1)
        try:
            DEFAULT_CALENDAR_BY_USER[int(_uid.strip())] = _cal.strip()
        except ValueError:
            logger.warning("Ignoring malformed DEFAULT_CALENDAR_MAP entry: %r", _pair)

# One unconfirmed draft per chat. Private Telegram chats have chat_id == user_id,
# so each person's draft is already isolated. Lost on restart, which is fine --
# only unconfirmed drafts are ever at risk, never a written event.
DRAFTS: dict[int, Draft] = {}

# The account's calendar names, fetched once and reused for the buttons.
# /calendars refreshes it if you add a calendar on your phone.
CALENDAR_CACHE: list[str] = []


async def get_calendars(force: bool = False) -> list[str]:
    global CALENDAR_CACHE
    if force or not CALENDAR_CACHE:
        CALENDAR_CACHE = await asyncio.to_thread(list_calendar_names)
        logger.info("Loaded %d calendars from iCloud", len(CALENDAR_CACHE))
    return CALENDAR_CACHE


# ---------------------------------------------------------------- keyboards


def build_keyboard(draft: Draft, calendars: list[str], user_id: int) -> InlineKeyboardMarkup:
    """The buttons under the card. Which buttons depends on the draft's state:
    a submenu replaces the main set while it's open."""

    if draft.open_menu == "duration":
        rows = [
            [InlineKeyboardButton(label, callback_data=f"dur:{'allday' if mins is None else mins}")]
            for label, mins in DURATION_CHOICES
        ]
        rows.append([InlineKeyboardButton("< Back", callback_data="menu:main")])
        return InlineKeyboardMarkup(_pack(rows))

    if draft.open_menu == "alert":
        choices = ALL_DAY_ALERT_CHOICES if draft.is_all_day else ALERT_CHOICES
        prefix = "adalert" if draft.is_all_day else "alert"
        rows = [
            [InlineKeyboardButton(label, callback_data=f"{prefix}:{'none' if v is None else v}")]
            for label, v in choices
        ]
        rows.append([InlineKeyboardButton("< Back", callback_data="menu:main")])
        return InlineKeyboardMarkup(_pack(rows))

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(f"Duration: {draft.describe_duration()}", callback_data="menu:duration"),
            InlineKeyboardButton(f"Alert: {draft.describe_alert()}", callback_data="menu:alert"),
        ]
    ]

    # Calendar buttons -- this person's usual one first, current pick ticked.
    ordered = _calendars_for(user_id, calendars)
    cal_row: list[InlineKeyboardButton] = []
    for name in ordered:
        label = f"[x] {name}" if name == draft.calendar else name
        cal_row.append(InlineKeyboardButton(label, callback_data=f"cal:{calendars.index(name)}"))
        if len(cal_row) == 2:
            rows.append(cal_row)
            cal_row = []
    if cal_row:
        rows.append(cal_row)

    if not draft.event.location and not draft.location_skipped:
        rows.append(
            [
                InlineKeyboardButton("Add location", callback_data="loc:add"),
                InlineKeyboardButton("No location", callback_data="loc:skip"),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton("Add to calendar", callback_data="confirm"),
            InlineKeyboardButton("Cancel", callback_data="cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _pack(rows: list[list[InlineKeyboardButton]]) -> list[list[InlineKeyboardButton]]:
    """Put submenu choices two per row so the menu isn't a long thin column."""
    flat = [b for row in rows[:-1] for b in row]
    packed = [flat[i : i + 2] for i in range(0, len(flat), 2)]
    packed.append(rows[-1])  # keep Back on its own row
    return packed


def _calendars_for(user_id: int, calendars: list[str]) -> list[str]:
    preferred = DEFAULT_CALENDAR_BY_USER.get(user_id)
    if preferred and preferred in calendars:
        return [preferred] + [c for c in calendars if c != preferred]
    return calendars


# ---------------------------------------------------------------- card


async def show_card(
    draft: Draft,
    chat_id: int,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    new_message: bool = False,
) -> None:
    """Draw (or redraw, in place) the review card."""
    calendars = await get_calendars()
    text = draft.render_card()
    markup = build_keyboard(draft, calendars, user_id)

    if new_message or draft.card_message_id is None:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        draft.card_message_id = sent.message_id
        return

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=draft.card_message_id, text=text, reply_markup=markup
        )
    except BadRequest as exc:
        # "Message is not modified" just means nothing visibly changed -- harmless.
        if "not modified" not in str(exc).lower():
            raise


# ---------------------------------------------------------------- auth


def is_allowed(user_id: int) -> bool:
    # Fails closed: an empty allowlist allows nobody (main() refuses to start anyway).
    return user_id in ALLOWED_USER_IDS


# ---------------------------------------------------------------- messages


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if not is_allowed(user_id):
        logger.warning("Rejected message from unauthorized user_id=%s", user_id)
        await update.message.reply_text("This bot is private.")
        return

    draft = DRAFTS.get(chat_id)

    # 1. We asked them for a location and they answered.
    if draft is not None and draft.awaiting == "location":
        draft.event.location = text
        draft.awaiting = None
        await show_card(draft, chat_id, user_id, context)
        return

    # 2. Typed shortcuts, kept from v1 so old habits still work.
    low = text.lower()
    if draft is not None and low in ("yes", "y"):
        await do_confirm(draft, chat_id, user_id, context)
        return
    if draft is not None and low in ("no", "n", "cancel"):
        DRAFTS.pop(chat_id, None)
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=draft.card_message_id, text="Discarded — nothing was added."
        )
        return

    # 3. Anything else: a correction to the current draft, or a brand new event.
    #    Gemini decides which, given the existing event as context.
    thinking = await update.message.reply_text(
        "Updating..." if draft else "Got it, thinking... (can take up to ~1-2 min on the free tier)"
    )
    try:
        parsed = await asyncio.to_thread(
            parse_event_text, text, None, draft.event if draft else None
        )
    except Exception:
        logger.exception("Failed to parse event text")
        await thinking.edit_text(
            "Sorry, couldn't parse that. Try rephrasing, or check the logs "
            "(docker compose logs) for the exact error."
        )
        return
    await thinking.delete()

    if draft is None:
        draft = Draft(event=parsed)
        DRAFTS[chat_id] = draft
        await show_card(draft, chat_id, user_id, context, new_message=True)
    else:
        # Keep the button choices they already made; only the parsed fields change.
        draft.event = parsed
        await show_card(draft, chat_id, user_id, context)


# ---------------------------------------------------------------- buttons


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if not is_allowed(user_id):
        await query.answer("This bot is private.", show_alert=True)
        return

    draft = DRAFTS.get(chat_id)
    if draft is None:
        await query.answer("That event has expired — send it again.", show_alert=True)
        return

    data = query.data
    action, _, value = data.partition(":")

    if action == "menu":
        draft.open_menu = None if value == "main" else value

    elif action == "dur":
        if value == "allday":
            draft.event.all_day = True
            draft.duration_minutes = None
        else:
            draft.event.all_day = False
            draft.duration_minutes = int(value)
        draft.open_menu = None

    elif action == "alert":
        draft.alert_minutes = None if value == "none" else int(value)
        draft.alert_chosen = True
        draft.open_menu = None

    elif action == "adalert":
        draft.all_day_alert_hour = None if value == "none" else int(value)
        draft.alert_chosen = True
        draft.open_menu = None

    elif action == "cal":
        calendars = await get_calendars()
        try:
            draft.calendar = calendars[int(value)]
        except (ValueError, IndexError):
            await query.answer("That calendar is gone — try /calendars to refresh.", show_alert=True)
            return

    elif action == "loc":
        if value == "skip":
            draft.location_skipped = True
            draft.awaiting = None
        else:
            draft.awaiting = "location"

    elif action == "cancel":
        DRAFTS.pop(chat_id, None)
        await query.answer("Discarded.")
        await query.edit_message_text("Discarded — nothing was added.")
        return

    elif action == "confirm":
        if not draft.ready():
            await query.answer(f"Still needed: {', '.join(draft.missing())}", show_alert=True)
            return
        await query.answer()
        await do_confirm(draft, chat_id, user_id, context)
        return

    await query.answer()
    await show_card(draft, chat_id, user_id, context)


# ---------------------------------------------------------------- writing


async def do_confirm(
    draft: Draft, chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not draft.ready():
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Not yet — still needed: {', '.join(draft.missing())}. Use the buttons on the card.",
        )
        return

    event = draft.to_event()
    calendar = draft.calendar
    try:
        await asyncio.to_thread(
            create_calendar_event, event, calendar, alarm_offset=draft.alarm_offset()
        )
    except Exception:
        logger.exception("Failed to write event to calendar")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Something went wrong writing that to your calendar. Nothing was added — "
            "try again, or check the logs (docker compose logs) for the exact error.",
        )
        return

    DRAFTS.pop(chat_id, None)
    summary = (
        f"Added to {calendar}:\n"
        f"{event.title} — "
        + (draft.start.strftime("%a %d %b (all day)") if draft.is_all_day
           else draft.start.strftime("%a %d %b, %I:%M %p"))
        + (f"\nAlert: {draft.describe_alert()}" if draft.alarm_offset() else "")
    )
    if draft.card_message_id:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=draft.card_message_id, text=summary
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text=summary)


# ---------------------------------------------------------------- commands


async def cmd_calendars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    names = await get_calendars(force=True)
    await update.message.reply_text("Calendars on this account:\n" + "\n".join(f"- {n}" for n in names))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "Send me an event in plain English, e.g.\n"
        '  "lunch with John next Friday 1pm at Toast Box"\n\n'
        "I'll show a card with everything I understood. Use the buttons to set the "
        "duration, alert and calendar, or just type a correction like \"make it 2pm\".\n\n"
        "/calendars — refresh the list of calendars\n"
        "/help — this message"
    )


def main() -> None:
    if not ALLOWED_USER_IDS:
        raise SystemExit(
            "TELEGRAM_ALLOWED_USER_ID is empty, so this bot refuses to start (it would be open "
            "to anyone). Find your numeric Telegram user ID (e.g. message @userinfobot), put it "
            'in .env as TELEGRAM_ALLOWED_USER_ID=<id> (comma-separate more people, e.g. '
            '"111,222"), then start the bot again.'
        )
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("calendars", cmd_calendars))
    app.add_handler(CommandHandler(["help", "start"], cmd_help))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting (long polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
