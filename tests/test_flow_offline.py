"""
Offline walk-through of the whole conversation, with Telegram, Gemini and
iCloud all faked. No network, no credentials, no waiting.

The point is to prove the logic before it ever runs live: that Confirm is
blocked while something's missing, that the buttons change what they claim
to change, that typed corrections don't wipe out choices already made, and
that the write is called exactly once with the right calendar and alarm.

Usage:
    python tests/test_flow_offline.py
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))

import asyncio
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# Fake credentials so importing bot.py doesn't fail -- nothing here is used.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ICLOUD_APPLE_ID", "test@example.com")
os.environ.setdefault("ICLOUD_APP_SPECIFIC_PASSWORD", "test-pw")
os.environ["TELEGRAM_ALLOWED_USER_ID"] = "111,222"
os.environ["DEFAULT_CALENDAR_MAP"] = "111:Bot Events,222:Family"

import bot  # noqa: E402
from gemini_parse import ParsedEvent  # noqa: E402

CALENDARS = ["Bot Events", "Family", "Work"]

checks = 0
failures = []


def check(label, condition, detail=""):
    global checks
    checks += 1
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"   {detail}" if not condition else ""))
    if not condition:
        failures.append(label)


def make_context():
    ctx = SimpleNamespace()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=500))
    ctx.bot.edit_message_text = AsyncMock()
    return ctx


def make_message_update(user_id, chat_id, text):
    msg = MagicMock()
    msg.text = text
    reply = SimpleNamespace(message_id=900, edit_text=AsyncMock(), delete=AsyncMock())
    msg.reply_text = AsyncMock(return_value=reply)
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
        message=msg,
    )


def make_button_update(user_id, chat_id, data):
    query = MagicMock()
    query.data = data
    query.from_user = SimpleNamespace(id=user_id)
    query.message = SimpleNamespace(chat_id=chat_id)
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return SimpleNamespace(callback_query=query), query


def last_card(ctx):
    """The most recent card text the bot drew."""
    if ctx.bot.edit_message_text.await_args:
        return ctx.bot.edit_message_text.await_args.kwargs["text"]
    return ctx.bot.send_message.await_args.kwargs["text"]


def buttons(chat_id, user_id):
    labels = []
    for row in bot.build_keyboard(bot.DRAFTS[chat_id], CALENDARS, user_id).inline_keyboard:
        labels.extend(b.text for b in row)
    return labels


async def scenario_timed_event():
    print("\n1. A normal timed event, with a location already in the message")
    bot.DRAFTS.clear()
    bot.CALENDAR_CACHE[:] = CALENDARS
    ctx = make_context()

    parsed = ParsedEvent(
        title="Lunch with John",
        start_datetime="2026-09-11T13:00:00+08:00",
        location="Toast Box",
    )
    with patch.object(bot, "parse_event_text", return_value=parsed):
        await bot.handle_message(make_message_update(111, 111, "lunch with john friday 1pm at toast box"), ctx)

    draft = bot.DRAFTS[111]
    card = last_card(ctx)
    check("a draft was created", draft is not None)
    check("card shows the title", "Lunch with John" in card)
    check("card shows the location from the message", "Toast Box" in card)
    check("no location prompt, since one was given", "Add location" not in buttons(111, 111))
    check("calendar flagged as needed", "calendar" in draft.missing(), f"missing={draft.missing()}")
    check("location NOT flagged, it was given", "location" not in draft.missing())
    check("duration defaults to 1 hour", draft.describe_duration() == "1 hour", draft.describe_duration())
    check("alert defaults to 30 min before", draft.describe_alert() == "30 min before", draft.describe_alert())
    check("this person's usual calendar is offered first", buttons(111, 111)[2] == "Bot Events")

    print("\n2. Confirming too early is refused, and writes nothing")
    upd, query = make_button_update(111, 111, "confirm")
    with patch.object(bot, "create_calendar_event") as write:
        await bot.handle_button(upd, ctx)
    check("write never called", write.call_count == 0)
    check("told what's still needed", "calendar" in str(query.answer.await_args))
    check("draft survived", 111 in bot.DRAFTS)

    print("\n3. Buttons change what they say they change")
    upd, _ = make_button_update(111, 111, "menu:duration")
    await bot.handle_button(upd, ctx)
    check("duration submenu opened", "All day" in buttons(111, 111))
    upd, _ = make_button_update(111, 111, "dur:120")
    await bot.handle_button(upd, ctx)
    check("duration now 2 hours", draft.describe_duration() == "2 hours", draft.describe_duration())
    check("submenu closed again", "Cancel" in buttons(111, 111))
    check("end time followed the duration", draft.end().hour == 15, str(draft.end()))

    upd, _ = make_button_update(111, 111, "alert:60")
    await bot.handle_button(upd, ctx)
    check("alert now 1 hour before", draft.describe_alert() == "1 hour before", draft.describe_alert())
    check("alarm offset is -1h", draft.alarm_offset() == timedelta(hours=-1), str(draft.alarm_offset()))

    upd, _ = make_button_update(111, 111, "cal:2")  # "Work"
    await bot.handle_button(upd, ctx)
    check("calendar set to Work", draft.calendar == "Work", str(draft.calendar))
    check("chosen calendar is ticked", "[x] Work" in buttons(111, 111))
    check("nothing left missing", draft.ready())

    print("\n4. A typed correction keeps the choices already made")
    corrected = ParsedEvent(
        title="Lunch with John",
        start_datetime="2026-09-11T14:00:00+08:00",
        location="Toast Box",
    )
    with patch.object(bot, "parse_event_text", return_value=corrected):
        await bot.handle_message(make_message_update(111, 111, "make it 2pm"), ctx)
    draft = bot.DRAFTS[111]
    check("start time updated to 2pm", draft.start.hour == 14, str(draft.start))
    check("calendar choice survived the correction", draft.calendar == "Work")
    check("duration choice survived", draft.describe_duration() == "2 hours")
    check("alert choice survived", draft.describe_alert() == "1 hour before")

    print("\n5. Confirming writes exactly one event, with the right arguments")
    upd, _ = make_button_update(111, 111, "confirm")
    with patch.object(bot, "create_calendar_event", return_value="https://example/1.ics") as write:
        await bot.handle_button(upd, ctx)
    check("write called once", write.call_count == 1, str(write.call_args_list))
    if write.call_count:
        args, kwargs = write.call_args
        written, calendar = args[0], args[1]
        check("written to the chosen calendar", calendar == "Work", calendar)
        check("alarm passed through", kwargs["alarm_offset"] == timedelta(hours=-1), str(kwargs))
        check("end time reflects the 2-hour choice", written.end_datetime.startswith("2026-09-11T16:00"), written.end_datetime)
    check("draft cleared after writing", 111 not in bot.DRAFTS)
    check("confirmation names the calendar", "Added to Work" in last_card(ctx), last_card(ctx))


async def scenario_missing_location():
    print("\n6. An event with no location stops and asks")
    bot.DRAFTS.clear()
    ctx = make_context()
    parsed = ParsedEvent(title="Dentist", start_datetime="2026-09-12T10:00:00+08:00")
    with patch.object(bot, "parse_event_text", return_value=parsed):
        await bot.handle_message(make_message_update(222, 222, "dentist tomorrow 10am"), ctx)

    draft = bot.DRAFTS[222]
    check("location flagged as needed", "location" in draft.missing(), str(draft.missing()))
    check("card says it's needed", "— needed —" in last_card(ctx))
    check("location buttons offered", "Add location" in buttons(222, 222))
    check("second person's usual calendar offered first", buttons(222, 222)[2] == "Family")

    upd, _ = make_button_update(222, 222, "loc:add")
    await bot.handle_button(upd, ctx)
    check("bot is waiting for a typed location", draft.awaiting == "location")
    check("card asks for it", "Send me the location" in last_card(ctx))

    await bot.handle_message(make_message_update(222, 222, "Tampines Mall"), ctx)
    check("typed location captured, not sent to Gemini", draft.event.location == "Tampines Mall")
    check("no longer waiting", draft.awaiting is None)

    upd, _ = make_button_update(222, 222, "cal:1")
    await bot.handle_button(upd, ctx)
    check("ready once calendar picked", draft.ready())

    print("\n7. 'No location' is a valid answer too")
    bot.DRAFTS.clear()
    ctx = make_context()
    with patch.object(bot, "parse_event_text", return_value=ParsedEvent(title="Call mum", start_datetime="2026-09-12T20:00:00+08:00")):
        await bot.handle_message(make_message_update(111, 111, "call mum tomorrow 8pm"), ctx)
    draft = bot.DRAFTS[111]
    upd, _ = make_button_update(111, 111, "loc:skip")
    await bot.handle_button(upd, ctx)
    check("location no longer blocks", "location" not in draft.missing())
    check("card shows it as none", "(none)" in last_card(ctx))


async def scenario_all_day():
    print("\n8. All-day events use their own alert choices")
    bot.DRAFTS.clear()
    ctx = make_context()
    parsed = ParsedEvent(title="Mum's birthday", start_datetime="2026-10-05T00:00:00+08:00", all_day=True)
    with patch.object(bot, "parse_event_text", return_value=parsed):
        await bot.handle_message(make_message_update(111, 111, "mum's birthday 5 october"), ctx)
    draft = bot.DRAFTS[111]

    check("shown as all day", "(all day)" in last_card(ctx))
    check("defaults to 09:00 on the day", draft.alarm_offset() == timedelta(hours=9), str(draft.alarm_offset()))

    upd, _ = make_button_update(111, 111, "menu:alert")
    await bot.handle_button(upd, ctx)
    check("all-day alert menu offered", "18:00 day before" in buttons(111, 111), str(buttons(111, 111)))
    upd, _ = make_button_update(111, 111, "adalert:-6")
    await bot.handle_button(upd, ctx)
    check("alarm now 6h before midnight", draft.alarm_offset() == timedelta(hours=-6), str(draft.alarm_offset()))

    upd, _ = make_button_update(111, 111, "loc:skip")
    await bot.handle_button(upd, ctx)
    upd, _ = make_button_update(111, 111, "cal:0")
    await bot.handle_button(upd, ctx)
    upd, _ = make_button_update(111, 111, "confirm")
    with patch.object(bot, "create_calendar_event", return_value="https://example/2.ics") as write:
        await bot.handle_button(upd, ctx)
    check("write called once", write.call_count == 1)
    if write.call_count:
        written = write.call_args[0][0]
        check("kept as an all-day event", written.all_day is True)
        check("no end time forced on it", written.end_datetime is None, str(written.end_datetime))


async def scenario_security():
    print("\n9. Strangers still get nothing")
    bot.DRAFTS.clear()
    ctx = make_context()
    upd = make_message_update(999, 999, "lunch tomorrow 1pm")
    with patch.object(bot, "parse_event_text") as parse:
        await bot.handle_message(upd, ctx)
    check("Gemini never called for an unknown user", parse.call_count == 0)
    check("no draft created", 999 not in bot.DRAFTS)
    check("told it's private", "private" in str(upd.message.reply_text.await_args))

    bot.DRAFTS[111] = bot.Draft(event=ParsedEvent(title="x", start_datetime="2026-09-11T13:00:00+08:00"))
    upd, query = make_button_update(999, 111, "confirm")
    with patch.object(bot, "create_calendar_event") as write:
        await bot.handle_button(upd, ctx)
    check("stranger's button press writes nothing", write.call_count == 0)


async def scenario_allowlist_fail_closed():
    print("\n10. An empty allowlist refuses everyone, and the bot will not start")
    with patch.object(bot, "ALLOWED_USER_IDS", set()):
        check("empty allowlist allows nobody", bot.is_allowed(111) is False)
        check("... not even user id 0", bot.is_allowed(0) is False)
        with patch.object(bot, "Application") as app_cls:
            exited = None
            try:
                bot.main()
            except SystemExit as e:
                exited = e
        check("main() exits when the allowlist is empty", exited is not None)
        check("exit message says how to fix it",
              exited is not None and "TELEGRAM_ALLOWED_USER_ID" in str(exited.code))
        check("Telegram application never built", app_cls.builder.call_count == 0)

        bot.DRAFTS.clear()
        ctx = make_context()
        upd = make_message_update(111, 111, "lunch tomorrow 1pm")
        with patch.object(bot, "parse_event_text") as parse:
            await bot.handle_message(upd, ctx)
        check("message refused as private", "private" in str(upd.message.reply_text.await_args))
        check("Gemini never called", parse.call_count == 0)
        check("no draft created", 111 not in bot.DRAFTS)
    check("allowlist restored after the test", bot.is_allowed(111) is True)


async def main():
    await scenario_timed_event()
    await scenario_missing_location()
    await scenario_all_day()
    await scenario_security()
    await scenario_allowlist_fail_closed()

    print("\n" + "=" * 70)
    if failures:
        print(f"{len(failures)} of {checks} checks FAILED:")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print(f"All {checks} checks passed. The conversation logic holds together.")
    print("Still unproven offline: whether iCloud honours the alarms (test_alarms.py)")
    print("and how it all feels in the real Telegram app.")


if __name__ == "__main__":
    asyncio.run(main())
