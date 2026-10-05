"""
test_service_offline.py — the Butler adapter, with Gemini and iCloud mocked out.

Nothing here touches the network, spends an API call, or writes to your
calendar. It drives service.py's actions directly and checks the whole
draft -> amend -> confirm path, including the cases where things go wrong.

Run:  python tests/test_service_offline.py
"""

from __future__ import annotations
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.abspath(_os.path.join(_os.path.dirname(__file__), "..")))


import os

# Set before importing service.py, whose main() checks for them.
os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("ICLOUD_APPLE_ID", "test@example.com")
os.environ.setdefault("ICLOUD_APP_SPECIFIC_PASSWORD", "test")
os.environ.setdefault("DEFAULT_CALENDAR_MAP", "111:Bot Events,222:Family")

import service  # noqa: E402
from gemini_parse import ParsedEvent  # noqa: E402

PASS, FAIL = [], []
WRITES: list[dict] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}"
          + (f"\n         {detail}" if detail and not condition else ""))


# --- mocks -------------------------------------------------------------------

def fake_calendars() -> list[str]:
    return ["Bot Events", "Family", "Work"]


def fake_parse(text: str, reference_dt=None, pending=None) -> ParsedEvent:
    """Deterministic stand-in for Gemini. Understands just enough to test flow."""
    low = text.lower()
    if pending is not None:
        # Amendment: carry everything over, change only what's mentioned.
        updated = pending.model_copy()
        if "2pm" in low:
            updated.start_datetime = "2026-09-10T14:00:00+08:00"
        if "all day" in low:
            updated.all_day = True
        return updated
    return ParsedEvent(
        title="Lunch with Sarah",
        start_datetime="2026-09-10T13:00:00+08:00",
        end_datetime=None,
        location=None,
        all_day=False,
        notes=None,
    )


def fake_create(event, calendar_name=None, *, alarm_offset=None) -> str:
    WRITES.append({"title": event.title, "calendar": calendar_name,
                   "alarm": alarm_offset, "start": event.start_datetime})
    return "https://caldav.icloud.com/fake/event.ics"


service.list_calendar_names = fake_calendars
service.calendars = fake_calendars
service.parse_event_text = fake_parse
service.create_calendar_event = fake_create


def reset() -> None:
    service.DRAFTS.clear()
    WRITES.clear()


# --- tests -------------------------------------------------------------------

def test_capabilities_shape():
    print("\n1. /capabilities is contract-shaped and hides internal actions")
    caps = service.CAPABILITIES
    check("has a name", caps["name"] == "calendar")
    check("description says what it does NOT do", "NOT" in caps["description"])
    public = [a for a in caps["actions"] if not a.get("internal")]
    check("exactly one public action", len(public) == 1, f"{[a['name'] for a in public]}")
    check("the public one is draft_event", public[0]["name"] == "draft_event")
    check("every other action is marked internal",
          len(caps["actions"]) - len(public) == len(service.ACTIONS) - 1,
          f"{[a['name'] for a in caps['actions'] if a.get('internal')]}")
    check("every action is implemented",
          {a["name"] for a in caps["actions"]} == set(service.ACTIONS),
          f"{set(service.ACTIONS) ^ {a['name'] for a in caps['actions']}}")


def test_draft_prefills_known_users_calendar():
    print("\n2. A known person's usual calendar is pre-filled")
    reset()
    out = service.action_draft_event({"text": "lunch with Sarah Thursday 1pm"}, 111, "")
    check("ok", out["ok"], str(out))
    check("card shows the pre-filled calendar", "Bot Events" in out["reply"], out["reply"])
    check("asks for the missing location", "location" in out["data"]["missing"],
          str(out["data"]))
    check("not ready yet", out["data"]["ready"] is False)
    labels = [b["label"] for b in out["followup"]["buttons"]]
    check("offers 'No location'", "No location" in labels, str(labels))
    check("does NOT offer Add yet", "Add to calendar" not in labels, str(labels))


def test_draft_for_unknown_user_asks_which_calendar():
    print("\n3. An unknown person is asked which calendar")
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 999, "")
    draft_id = out["data"]["draft_id"]
    labels = [b["label"] for b in out["followup"]["buttons"]]
    check("card prompts for a calendar", "Calendar: choose" in labels, str(labels))
    check("not ready without one", out["data"]["ready"] is False, str(out["data"]))

    out = service.action_open_menu({"draft_id": draft_id, "menu": "calendar"}, 999, "")
    labels = [b["label"] for b in out["followup"]["buttons"]]
    check("menu offers the choices", "Bot Events" in labels and "Family" in labels,
          str(labels))
    check("their own calendar is first for a known user",
          service._calendars_for(222)[0] == "Family", str(service._calendars_for(222)))


def test_full_happy_path():
    print("\n4. draft -> location -> confirm actually writes")
    reset()
    out = service.action_draft_event({"text": "lunch with Sarah Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]

    out = service.action_skip_location({"draft_id": draft_id}, 111, "")
    check("ready once location is settled", out["data"]["ready"] is True, str(out["data"]))
    labels = [b["label"] for b in out["followup"]["buttons"]]
    check("now offers Add to calendar", "Add to calendar" in labels, str(labels))

    out = service.action_confirm_event({"draft_id": draft_id}, 111, "")
    check("confirm succeeded", out["ok"], str(out))
    check("one event written", len(WRITES) == 1, str(WRITES))
    check("written to the right calendar", WRITES[0]["calendar"] == "Bot Events",
          str(WRITES[0]))
    check("an alarm was attached", WRITES[0]["alarm"] is not None, str(WRITES[0]))
    check("no followup — conversation is over", "followup" not in out, str(out.keys()))
    check("draft cleaned up", draft_id not in service.DRAFTS)


def test_typed_correction():
    print("\n5. Typing 'make it 2pm' amends rather than starting over")
    reset()
    out = service.action_draft_event({"text": "lunch with Sarah Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]
    service.action_skip_location({"draft_id": draft_id}, 111, "")

    out = service.action_amend_draft({"draft_id": draft_id, "text": "make it 2pm"}, 111, "")
    check("still the same draft", out["data"]["draft_id"] == draft_id)
    check("time changed to 2pm", "02:00 PM" in out["reply"], out["reply"])
    check("title carried over", "Lunch with Sarah" in out["reply"], out["reply"])
    check("location choice survived the edit", out["data"]["ready"] is True,
          str(out["data"]))


def test_bare_text_becomes_the_location():
    print("\n6. A bare reply while location is missing IS the location")
    reset()
    out = service.action_draft_event({"text": "lunch with Sarah Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]

    out = service.action_amend_draft({"draft_id": draft_id, "text": "Marina Bay Sands"},
                                     111, "")
    check("location captured", "Marina Bay Sands" in out["reply"], out["reply"])
    check("ready now", out["data"]["ready"] is True, str(out["data"]))

    # But something that reads like a time must NOT become the location.
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]
    out = service.action_amend_draft({"draft_id": draft_id, "text": "make it 2pm"}, 111, "")
    # Check the Where LINE, not the whole card — the card's help footer
    # contains the literal example text 'make it 2pm', which fooled the first
    # version of this assertion into failing on correct behaviour.
    where = next(l for l in out["reply"].splitlines() if l.startswith("Where:"))
    check("a time change did not become the location",
          "make it 2pm" not in where, where)
    check("the time actually changed", "02:00 PM" in out["reply"], out["reply"])
    check("location still outstanding", "location" in out["data"]["missing"],
          str(out["data"]))


def test_confirm_blocked_until_ready():
    print("\n7. Confirm is refused while something is missing")
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 999, "")
    draft_id = out["data"]["draft_id"]

    out = service.action_confirm_event({"draft_id": draft_id}, 999, "")
    check("refused", out["ok"] is False, str(out))
    check("says what's missing", "calendar" in out["error"], out["error"])
    check("nothing written", WRITES == [], str(WRITES))


def test_draft_is_private_to_its_owner():
    print("\n8. One person cannot confirm another person's draft")
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]
    service.action_skip_location({"draft_id": draft_id}, 111, "")

    out = service.action_confirm_event({"draft_id": draft_id}, 222, "")
    check("other user refused", out["ok"] is False, str(out))
    check("nothing written", WRITES == [], str(WRITES))


def test_expired_and_bogus_drafts():
    print("\n9. Expired or invented draft ids fail cleanly")
    reset()
    out = service.action_confirm_event({"draft_id": "deadbeef"}, 111, "")
    check("bogus id refused", out["ok"] is False, str(out))
    check("message tells you what to do", "again" in out["error"].lower(), out["error"])

    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]
    service.DRAFTS[draft_id]["created"] -= (service.DRAFT_TTL_SECONDS + 10)
    out = service.action_amend_draft({"draft_id": draft_id, "text": "2pm"}, 111, "")
    check("expired draft refused", out["ok"] is False, str(out))


def test_cancel_writes_nothing():
    print("\n10. Cancel discards without touching the calendar")
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]
    out = service.action_cancel_draft({"draft_id": draft_id}, 111, "")
    check("ok", out["ok"])
    check("says nothing was added", "Nothing was added" in out["reply"], out["reply"])
    check("draft gone", draft_id not in service.DRAFTS)
    check("no writes", WRITES == [], str(WRITES))


def test_unknown_calendar_rejected():
    print("\n11. A calendar that doesn't exist is refused")
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 999, "")
    draft_id = out["data"]["draft_id"]
    out = service.action_set_calendar({"draft_id": draft_id, "calendar": "Nonexistent"},
                                      999, "")
    check("refused", out["ok"] is False, str(out))
    check("names the problem", "Nonexistent" in out["error"], out["error"])



def test_alert_can_be_changed():
    print("\n12. BUG FIX: the alert can actually be changed")
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]

    labels = [b["label"] for b in out["followup"]["buttons"]]
    check("main card offers an Alert button",
          any(l.startswith("Alert:") for l in labels), str(labels))
    check("it shows the current value", "Alert: 30 min before" in labels, str(labels))

    out = service.action_open_menu({"draft_id": draft_id, "menu": "alert"}, 111, "")
    labels = [b["label"] for b in out["followup"]["buttons"]]
    check("alert menu lists the choices",
          "1 hour before" in labels and "No alert" in labels, str(labels))
    check("submenu replaces the main buttons",
          not any(l.startswith("Calendar:") for l in labels), str(labels))
    check("offers a way back", "< Back" in labels, str(labels))

    out = service.action_set_alert({"draft_id": draft_id, "value": 60}, 111, "")
    check("card now shows the new alert", "1 hour before" in out["reply"], out["reply"])
    check("menu closed after choosing",
          any(l.startswith("Calendar:") for l in
              [b["label"] for b in out["followup"]["buttons"]]))

    # "No alert" must actually mean none, not silently fall back to the default.
    out = service.action_set_alert({"draft_id": draft_id, "value": None}, 111, "")
    check("'No alert' really means none", "Alert:     none" in out["reply"], out["reply"])

    # ...and it must survive all the way to what gets written.
    service.action_skip_location({"draft_id": draft_id}, 111, "")
    service.action_confirm_event({"draft_id": draft_id}, 111, "")
    check("no alarm was written", WRITES and WRITES[0]["alarm"] is None, str(WRITES))


def test_calendar_can_be_changed_after_prefill():
    print("\n13. BUG FIX: the calendar can be changed once pre-filled")
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]
    check("pre-filled from the map", "Bot Events" in out["reply"], out["reply"])

    labels = [b["label"] for b in out["followup"]["buttons"]]
    check("a Calendar button is still offered",
          "Calendar: Bot Events" in labels, str(labels))

    out = service.action_open_menu({"draft_id": draft_id, "menu": "calendar"}, 111, "")
    labels = [b["label"] for b in out["followup"]["buttons"]]
    check("lists every calendar", {"Bot Events", "Family", "Work"} <= set(labels),
          str(labels))

    out = service.action_set_calendar({"draft_id": draft_id, "calendar": "Family"}, 111, "")
    check("switched", "Calendar:  Family" in out["reply"], out["reply"])

    service.action_skip_location({"draft_id": draft_id}, 111, "")
    service.action_confirm_event({"draft_id": draft_id}, 111, "")
    check("written to the chosen calendar", WRITES[0]["calendar"] == "Family", str(WRITES))


def test_typing_opens_the_right_menu():
    print("\n14. Typing about a button-only field opens that menu")
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]

    for typed, expect in [("change the alert to 1 hour", "1 hour before"),
                          ("use a different calendar", "Family"),
                          ("make it all day", "All day")]:
        out = service.action_amend_draft({"draft_id": draft_id, "text": typed}, 111, "")
        labels = [b["label"] for b in out["followup"]["buttons"]]
        check(f"{typed!r} opens the right menu", expect in labels, str(labels))
        check(f"{typed!r} explains why typing didn't work",
              "can't change that one" in out["reply"], out["reply"][-120:])
        service.action_open_menu({"draft_id": draft_id, "menu": ""}, 111, "")


def test_menu_intent_does_not_hijack_real_edits():
    print("\n15. Ordinary edits are NOT mistaken for menu commands")
    reset()
    for typed in ["make it 2pm", "remind me to call the dean",
                  "lunch with Sarah instead", "Marina Bay Sands"]:
        check(f"{typed!r} is not a menu command",
              service._menu_intent(typed) is None, str(service._menu_intent(typed)))


def test_all_day_switch_uses_all_day_alert_choices():
    print("\n16. Switching to all-day switches the alert choices too")
    reset()
    out = service.action_draft_event({"text": "lunch Thursday 1pm"}, 111, "")
    draft_id = out["data"]["draft_id"]

    out = service.action_set_duration({"draft_id": draft_id, "minutes": None}, 111, "")
    check("now all day", "all day" in out["reply"], out["reply"])

    out = service.action_open_menu({"draft_id": draft_id, "menu": "alert"}, 111, "")
    labels = [b["label"] for b in out["followup"]["buttons"]]
    check("offers all-day alert choices", "09:00 on the day" in labels, str(labels))
    check("does not offer minutes-before choices", "30 min before" not in labels,
          str(labels))

    labels_main = [b["label"] for b in
                   service.action_open_menu({"draft_id": draft_id, "menu": ""},
                                            111, "")["followup"]["buttons"]]
    check("duration button becomes 'Make it timed'", "Make it timed" in labels_main,
          str(labels_main))


def test_http_input_validation():
    import http.client
    import json as _json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    print("\n17. /invoke rejects bad input with a short fixed reply (Gate G #14, C2/C3)")
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    BAD = {"ok": False, "error": "Bad request."}

    def raw(content_length, payload=b""):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.putrequest("POST", "/invoke")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", content_length)
            conn.endheaders()
            if payload:
                conn.send(payload)
            r = conn.getresponse()
            return r.status, _json.loads(r.read())
        finally:
            conn.close()

    def post(obj):
        data = _json.dumps(obj).encode()
        return raw(str(len(data)), data)

    try:
        check("non-numeric Content-Length", raw("abc") == (200, BAD))
        check("negative Content-Length", raw("-5") == (200, BAD))
        check("invalid JSON gets the fixed text", raw("5", b"{nope") == (200, BAD))
        for payload in (b"[]", b'"x"', b"5", b"null"):
            check("non-object JSON %r" % payload, raw(str(len(payload)), payload) == (200, BAD))
        big = _json.dumps({"action": "draft_event", "params": {"text": "a" * 20000}}).encode()
        check("body over 16 kB", raw(str(len(big)), big) == (200, BAD))
        check("params must be an object (list)",
              post({"action": "draft_event", "params": []}) == (200, BAD))
        check("params must be an object (string)",
              post({"action": "draft_event", "params": "x"}) == (200, BAD))
        check("user_id not a number",
              post({"action": "draft_event", "params": {"text": "hi"}, "user_id": "abc"}) == (200, BAD))
        check("user_id is a list",
              post({"action": "draft_event", "params": {"text": "hi"}, "user_id": [1]}) == (200, BAD))
        inf = b'{"action": "draft_event", "params": {"text": "hi"}, "user_id": 1e999}'
        check("user_id is infinity", raw(str(len(inf)), inf) == (200, BAD))
        check("text param over 4096 chars",
              post({"action": "draft_event", "params": {"text": "a" * 4097}, "user_id": 111}) == (200, BAD))
        check("original_message over 4096 chars",
              post({"action": "draft_event", "params": {"text": "x"}, "user_id": 111,
                    "original_message": "m" * 4097}) == (200, BAD))
        check("action name over 64 chars", post({"action": "a" * 65}) == (200, BAD))

        reset()
        st, body = post({"action": "draft_event", "params": {"text": "a" * 4096}, "user_id": 111})
        check("4096-char text is the limit, not 4095", body.get("ok") is True, str(body)[:120])

        def boom(params, user_id, original):
            raise RuntimeError("kaboom-SECRET")
        saved = service.ACTIONS["draft_event"]
        service.ACTIONS["draft_event"] = boom
        try:
            st, body = post({"action": "draft_event", "params": {"text": "x"}, "user_id": 111})
        finally:
            service.ACTIONS["draft_event"] = saved
        check("internal error reply is the fixed text",
              body == {"ok": False, "error": "Calendar hit an internal error."}, str(body))
        check("no exception type or message in the reply",
              "kaboom" not in str(body) and "RuntimeError" not in str(body))

        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=5) as r:
            check("server still healthy after all of that", _json.loads(r.read())["ok"] is True)
    finally:
        server.shutdown()
        server.server_close()


def run() -> int:
    print("=" * 70)
    print("Calendar bot — Butler adapter, offline")
    print("=" * 70)
    for fn in [
        test_capabilities_shape,
        test_draft_prefills_known_users_calendar,
        test_draft_for_unknown_user_asks_which_calendar,
        test_full_happy_path,
        test_typed_correction,
        test_bare_text_becomes_the_location,
        test_confirm_blocked_until_ready,
        test_draft_is_private_to_its_owner,
        test_expired_and_bogus_drafts,
        test_cancel_writes_nothing,
        test_unknown_calendar_rejected,
        test_alert_can_be_changed,
        test_calendar_can_be_changed_after_prefill,
        test_typing_opens_the_right_menu,
        test_menu_intent_does_not_hijack_real_edits,
        test_all_day_switch_uses_all_day_alert_choices,
        test_http_input_validation,
    ]:
        fn()

    print("\n" + "=" * 70)
    print(f"{len(PASS)} passed, {len(FAIL)} failed")
    for name in FAIL:
        print(f"  FAILED: {name}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(run())
