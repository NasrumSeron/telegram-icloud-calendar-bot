"""
Step 3 of the build: turn raw text into a structured calendar event using Gemini.

This module is deliberately standalone (no Telegram, no calendar-writing) so we
can test the parsing quality on its own before wiring it into the bot.

How it works:
  - We tell Gemini "today's date/time is X, in the Asia/Singapore timezone"
    so it can resolve relative phrases like "next Friday" or "tomorrow"
    correctly. Without this, the model has no fixed reference point.
  - We force the response into a JSON shape we define with pydantic
    (ParsedEvent below), using Gemini's structured-output feature. This is
    much more reliable than asking for "JSON" in the prompt and hoping.
  - If the model is unsure about something (e.g. no clear duration), it says
    so in `notes` instead of silently guessing — so the bot can surface that
    to you at confirmation time.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

load_dotenv()

SINGAPORE_TZ = ZoneInfo("Asia/Singapore")

# EDIT ME: plain values below — safe to change directly, then rebuild the
# image (`docker compose up -d --build`).
MODEL_NAME = "gemini-3.5-flash-lite"  # 3.7-flash was hitting heavy demand/congestion on free tier
# (28s+ for a one-word reply); 3.5-flash tested fast and reliable. Revisit if it ever
# becomes deprecated or starts showing the same slowness.
REQUEST_TIMEOUT_MS = 150_000  # free-tier ("Standard") latency is officially "seconds to
# minutes" per Google's own docs -- this isn't a bug, it's the free-tier trade-off we
# chose. 150s gives real margin above the ~1-2 min worst case we saw in testing.

# --- Retry settings (added 2026-09-07 after a 503 UNAVAILABLE killed a real request) ---
# The SDK does NOT retry by default: with no retry_options set, it uses a
# "stop after 1 attempt" policy, i.e. never retry. We have to ask for it.
RETRY_ATTEMPTS = 4  # EDIT ME: total tries, including the first one. 1 = no retries.
RETRY_INITIAL_DELAY_SECONDS = 2  # EDIT ME: first backoff wait; doubles each retry.
RETRY_MAX_DELAY_SECONDS = 30  # EDIT ME: cap on the wait between retries.
# EDIT ME: which failures are worth retrying.
#   503 = "high demand, try again later" -- exactly what we hit, and it fails fast,
#         so retrying costs a couple of seconds, not a couple of minutes.
#   500/502 = generic server-side hiccups, same reasoning.
# Deliberately NOT retried:
#   504 = we already waited the full 150s deadline; retrying stacks another 150s.
#   429 = quota. A per-minute limit might clear, but a daily one won't, and failing
#         fast with the real message is more useful than four slow attempts.
RETRY_ON_STATUS_CODES = [500, 502, 503]

# EDIT ME: if the model above is still unavailable after all retries, try this one
# instead before giving up. Set to None to disable the fallback entirely.
# gemini-3.5-flash passed the same 10-phrase gold standard, so accuracy is not the
# trade-off here -- it just has a much lower daily request limit, which is fine for
# the rare occasion the primary model is congested.
FALLBACK_MODEL_NAME = "gemini-3.5-flash"


class ParsedEvent(BaseModel):
    title: str = Field(description="Short event title, e.g. 'Lunch with John'.")
    start_datetime: str = Field(
        description=(
            "ISO 8601 datetime WITH timezone offset, e.g. '2026-08-28T13:00:00+08:00'. "
            "Resolve any relative date/time (e.g. 'next Friday', 'tomorrow 3pm') "
            "against the reference 'now' given in the prompt."
        )
    )
    end_datetime: Optional[str] = Field(
        default=None,
        description=(
            "ISO 8601 datetime with timezone offset, or null if not stated or "
            "inferrable. If the message clearly implies a duration, fill it in; "
            "otherwise leave null and mention it in notes."
        ),
    )
    location: Optional[str] = Field(default=None, description="Location, or null if none mentioned.")
    all_day: bool = Field(default=False, description="True if this is an all-day event with no specific time.")
    notes: Optional[str] = Field(
        default=None,
        description=(
            "Anything ambiguous or assumed while parsing (e.g. 'assumed 1 hour "
            "duration, none stated'), or null if nothing to flag."
        ),
    )


def parse_event_text(
    text: str,
    reference_dt: Optional[datetime] = None,
    pending: Optional[ParsedEvent] = None,
) -> ParsedEvent:
    """Parse free-text into a ParsedEvent using Gemini's free tier.

    reference_dt: the "now" to resolve relative dates against. Defaults to the
    actual current time in Singapore. Tests can pass a fixed value for
    reproducibility.

    pending: an already-parsed, not-yet-confirmed event, if one is awaiting
    confirmation for this chat. When given, Gemini decides whether `text` is
    a correction to THIS event (e.g. "make it 2pm instead") -- in which case
    it returns the updated event with everything else carried over -- or a
    completely different, unrelated event, in which case it ignores
    `pending` and parses `text` fresh. This lets the same function handle
    both "first message" and "fix one thing" without separate code paths.
    """
    if reference_dt is None:
        reference_dt = datetime.now(SINGAPORE_TZ)

    # An explicit timeout turns a silent network hang into a clear error
    # instead of hanging forever. Note: this value is forwarded to Google's
    # server as the request deadline (not just a client-side cutoff) -- a
    # schema-constrained JSON generation can genuinely take longer than a
    # short deadline, which surfaces as a 504 DEADLINE_EXCEEDED from the
    # server itself rather than a client-side timeout. 60s gives it room.
    client = genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(
            timeout=REQUEST_TIMEOUT_MS,
            retry_options=types.HttpRetryOptions(
                attempts=RETRY_ATTEMPTS,
                initial_delay=RETRY_INITIAL_DELAY_SECONDS,
                max_delay=RETRY_MAX_DELAY_SECONDS,
                http_status_codes=RETRY_ON_STATUS_CODES,
            ),
        ),
    )

    pending_block = ""
    if pending is not None:
        pending_block = f"""
There is a currently PENDING, not-yet-confirmed calendar event, still awaiting
the user's yes/no confirmation:
{pending.model_dump_json()}

Decide which of these the new message below is:
(a) A correction/amendment to THIS pending event -- e.g. it only mentions a
    changed time, date, location, or title, and plausibly refers to the same
    event. If so, apply the change and return the FULL updated event with
    every field filled in (carry over anything the message doesn't mention
    unchanged from the pending event above).
(b) A different, unrelated event. If so, IGNORE the pending event entirely
    and parse the new message as its own fresh, independent event.
"""

    prompt = f"""You extract calendar events from a short message written by a user in Singapore.

Reference "now": {reference_dt.isoformat()} (Asia/Singapore, UTC+8).
{pending_block}
Resolve all relative dates/times against that reference, using these exact rules:
- "next <weekday>" (e.g. "next Friday") ALWAYS means that weekday in the
  calendar week AFTER the current one (weeks run Monday-Sunday) -- even if
  that weekday hasn't happened yet in the current week. Example: if today is
  Wednesday, "next Friday" means the Friday of NEXT week (9 days away), NOT
  the Friday 2 days away.
- "this <weekday>" means the soonest occurrence, including today if today
  itself is that weekday.
- "in N weeks on <weekday>" is ALSO calendar-week anchored, same idea as
  "next <weekday>": take the current week's Monday, advance it by exactly
  N calendar weeks, then take <weekday> within THAT week. Do NOT compute
  it as "today + N*7 days, then nearest matching weekday" -- that gives a
  different (wrong) answer. Example: if today is Wednesday Aug 26 (this
  week's Monday = Aug 24), "in 2 weeks on Tuesday" = Aug 24 + 14 days =
  Sep 7 (that week's Monday), so Tuesday of that week = Sep 8 -- NOT Sep 15.
- "tomorrow", "N days from now" are computed literally (exact day count)
  from the reference "now".

Always output start_datetime and end_datetime (if any) as ISO 8601 with a
+08:00 offset. If no year is stated, assume the next occurrence of that date
on or after the reference "now".

Message: \"\"\"{text}\"\"\"
"""

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=ParsedEvent,
        # We don't use function-calling tools here, so silence the SDK's
        # unrelated "use AFC in Chat.send_message instead" log line.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    print("  -> sending request to Gemini (free tier -- can take up to ~1-2 min, this is normal)...", flush=True)
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt, config=config)
    except errors.ServerError as exc:
        # Retries (configured on the client above) are already exhausted by the
        # time we land here. A 503 means the model itself is congested, so the
        # only remaining move is a different model.
        if not FALLBACK_MODEL_NAME:
            raise
        print(
            f"  -> {MODEL_NAME} unavailable after {RETRY_ATTEMPTS} attempts ({exc}). "
            f"Falling back to {FALLBACK_MODEL_NAME}.",
            flush=True,
        )
        response = client.models.generate_content(
            model=FALLBACK_MODEL_NAME, contents=prompt, config=config
        )
    print("  -> got response.", flush=True)

    return ParsedEvent.model_validate_json(response.text)


if __name__ == "__main__":
    # Quick manual smoke test: python gemini_parse.py "some event text"
    import sys

    text = " ".join(sys.argv[1:]) or "lunch with John next Friday 1pm at the cafe"
    result = parse_event_text(text)
    print(result.model_dump_json(indent=2))
