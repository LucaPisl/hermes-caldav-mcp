import asyncio
import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from dav_fake import HOME, FakeDAV, calendar
from icalendar import Calendar
from test_caldav import SIMPLE, multistatus

from hermes_caldav_mcp.credentials import CredentialStore
from hermes_caldav_mcp.deletion import DeletePolicy
from hermes_caldav_mcp.server import (
    INSTRUCTIONS,
    CalendarService,
    _confirm,
    catalog,
    execute,
    make_server,
    run_doctor,
)
from hermes_caldav_mcp.transport import event_id
from hermes_caldav_mcp.types import ConnectionProfile, ErrorCode, SafeError

RECURRING = (Path(__file__).parent / "fixtures/recurring.ics").read_text()
TASK = SIMPLE.replace("BEGIN:VEVENT", "BEGIN:VTODO").replace("END:VEVENT", "END:VTODO")
BROKEN = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nEND:VCALENDAR"
NOW = datetime(2025, 3, 27, 8, 0, tzinfo=timezone.utc)
PERSONAL = calendar("personal", "Personal")
WORK = calendar("work", "Work")
WORKOUT = calendar("workout", "Workout", writable=False)
TASKS = calendar("tasks", "Tasks", components=("VTODO",))
FULL = catalog("full")


def event(uid, start, end, title, extra=""):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20250101T000000Z\r\nDTSTART:{start}\r\n"
        f"DTEND:{end}\r\nSUMMARY:{title}\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def setup(*calendars, enrolled=None, **kwargs):
    """A fake server plus a service whose profile enrolls the given calendars."""
    dav = FakeDAV(calendars)
    profile = ConnectionProfile(
        "https://cloud.example.test/nextcloud",
        "calendar-user",
        "synthetic-password",
        HOME,
        tuple(enrolled if enrolled is not None else calendars),
        "Europe/Berlin",
    )
    service = CalendarService(
        lambda: profile, transport_factory=dav.transport, clock=lambda: NOW, **kwargs
    )
    return dav, service


def ok(result):
    assert result["ok"], result
    return result["data"]


def failed(result, code):
    assert not result["ok"], result
    assert result["error"]["code"] == code, result
    return result["error"]["message"]


# Safety basics -------------------------------------------------------------------


async def test_locked_keyring_between_calls_stops_new_requests(profile, collection):
    store = CredentialStore(lambda: collection)
    store.save("default", profile, verified_encryption=True)
    requests = []

    def handler(r):
        requests.append(r)
        return httpx.Response(
            207, content=multistatus(profile.calendars[0].href, calendar=True)
        )

    service = CalendarService(
        lambda: store.load("default"),
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    first = await execute(service, "list_calendars", {})
    collection.locked = True
    second = await execute(service, "list_calendars", {})
    assert first["ok"]
    assert second["error"]["code"] == "CREDENTIALS_LOCKED"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "name,args,hint",
    [
        ("delete_event", {"event_id": "x"}, "not available"),
        ("list_calendars", {"secret": "forbidden"}, "secret: unknown argument"),
        ("get_event", {"event_id": "bad!"}, ""),
        ("create_event", {"title": "x"}, "start: required"),
        (
            "create_event",
            {"title": "x", "start": "2025-01-01", "attendees": ["evil"]},
            "attendees: unknown argument",
        ),
        ("create_event", {"title": "x", "start": "soon"}, "start: expected"),
        (
            "create_event",
            {"title": "x", "start": "2025-01-01", "repeat_count": 3},
            "repeat:",
        ),
        (
            "create_event",
            {"title": "x", "start": "2025-01-01", "reminder_minutes": [1] * 6},
            "at most 5",
        ),
    ],
)
async def test_invalid_inputs_send_no_requests_and_name_the_argument(name, args, hint):
    dav, service = setup(PERSONAL)
    message = failed(await execute(service, name, args), "INVALID_INPUT")
    assert hint in message
    assert not dav.requests


async def test_validation_hint_does_not_echo_values():
    _, service = setup(PERSONAL)
    secret = "sk-" + "s" * 30
    result = await execute(service, "list_calendars", {"token": secret})
    assert secret not in json.dumps(result)


async def test_internal_exception_is_redacted(profile, capsys):
    def loader():
        raise RuntimeError(profile.password)

    result = await execute(CalendarService(loader), "list_calendars", {})
    assert result["error"]["code"] == "INTERNAL_ERROR"
    assert profile.password not in str(result) + str(capsys.readouterr())


async def test_total_deadline_after_write_is_unknown_once(profile, monkeypatch):
    monkeypatch.setattr("hermes_caldav_mcp.server.TOOL_DEADLINE", 0.02)
    seen = []

    async def handler(r):
        seen.append(r)
        await asyncio.sleep(1)
        return httpx.Response(201)

    service = CalendarService(
        lambda: profile, transport_factory=lambda: httpx.MockTransport(handler)
    )
    result = await execute(
        service, "create_event", {"title": "x", "start": "2025-01-01"}
    )
    assert result["error"]["code"] == "UNKNOWN_WRITE_OUTCOME"
    assert len(seen) == 1


def test_mcp_advertises_only_tools_with_shared_instructions(profile):
    server = make_server(CalendarService(lambda: profile))
    caps = server.get_capabilities()
    assert caps.tools is not None
    assert caps.resources is None and caps.prompts is None and caps.logging is None
    assert "untrusted" in INSTRUCTIONS and "Never retry a delete" in INSTRUCTIONS


# Tiers ---------------------------------------------------------------------------

LITE = [
    "list_calendars",
    "upcoming_events",
    "list_events",
    "search_events",
    "get_event",
    "create_event",
    "update_event",
    "list_tasks",
    "create_task",
    "complete_task",
    "doctor",
]


def test_tiers_and_delete_opt_in():
    assert list(catalog()) == LITE
    assert set(FULL) == {*LITE, "find_free_time", "update_task"}
    assert "delete_event" not in FULL
    assert {"delete_event", "delete_task"} <= set(catalog("lite", deletes=True))
    full = FULL["create_event"][0].model_json_schema()["properties"]
    lite = catalog()["create_event"][0].model_json_schema()["properties"]
    assert "repeat_days" in full and "repeat_days" not in lite


@pytest.mark.parametrize("tier", ["lite", "full"])
def test_every_parameter_is_flat_and_described(tier):
    for name, (schema, _, description) in catalog(tier, deletes=True).items():
        assert 0 < len(description) <= 90, name
        properties = schema.model_json_schema().get("properties", {})
        for field, spec in properties.items():
            assert spec.get("description"), (name, field)
            assert "$ref" not in json.dumps(spec), (name, field)


# Calendar choice -------------------------------------------------------------------


async def test_list_calendars_reports_live_state_zone_and_default():
    stale = replace(WORK, name="Old name", writable=True)
    dav, service = setup(
        PERSONAL,
        replace(WORK, writable=False),
        TASKS,
        enrolled=(PERSONAL, stale, TASKS),
    )
    data = ok(await execute(service, "list_calendars", {}))
    assert data["timezone"] == "Europe/Berlin"
    assert data["now"] == "2025-03-27T09:00+01:00"
    assert data["calendars"] == [
        {
            "calendar_id": "cal-personal",
            "name": "Personal",
            "writable": True,
            "holds": ["events"],
            "default": True,
        },
        {
            "calendar_id": "cal-work",
            "name": "Work",
            "writable": False,
            "holds": ["events"],
        },
        {
            "calendar_id": "cal-tasks",
            "name": "Tasks",
            "writable": True,
            "holds": ["tasks"],
        },
    ]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("cal-work", "Work"),
        ("work", "Work"),
        ("PERS", "Personal"),
        (" workout ", "Workout"),
    ],
)
async def test_calendar_by_id_name_or_unique_prefix(value, expected):
    dav, service = setup(PERSONAL, WORK, WORKOUT)
    for cal in (PERSONAL, WORK, WORKOUT):
        dav.add(cal, "a.ics", event("u", "20250328T100000Z", "20250328T110000Z", "x"))
    data = ok(await execute(service, "upcoming_events", {"calendar": value}))
    assert [e["calendar"] for e in data["events"]] == [expected]


async def test_ambiguous_or_unknown_calendar_lists_candidates():
    dav, service = setup(PERSONAL, WORK, WORKOUT)
    message = failed(
        await execute(service, "list_events", {"calendar": "wo"}), "INVALID_INPUT"
    )
    assert "Work" in message and "Workout" in message
    message = failed(
        await execute(service, "list_events", {"calendar": "Holidays"}), "NOT_FOUND"
    )
    assert "Personal, Work, Workout" in message


async def test_renamed_calendar_is_found_by_its_live_name():
    dav, service = setup(replace(WORK, name="Job"), enrolled=(WORK,))
    dav.add(WORK, "a.ics", event("u", "20250328T100000Z", "20250328T110000Z", "x"))
    data = ok(await execute(service, "upcoming_events", {"calendar": "Job"}))
    assert len(data["events"]) == 1


async def test_write_without_calendar_uses_default_or_asks():
    dav, service = setup(PERSONAL, WORK)
    args = {"title": "x", "start": "2025-04-01"}
    message = failed(await execute(service, "create_event", args), "INVALID_INPUT")
    assert "calendar: required" in message and "Personal, Work" in message
    assert not dav.writes()
    service.default_calendar = "work"
    assert ok(await execute(service, "create_event", args))["calendar"] == "Work"
    # A single writable calendar needs no choice.
    dav, service = setup(PERSONAL, WORKOUT)
    assert ok(await execute(service, "create_event", args))["calendar"] == "Personal"


async def test_refused_write_is_authoritative_and_actionable():
    args = {"title": "x", "start": "2025-04-01"}
    # The stored flag says writable; Nextcloud says otherwise.
    dav, service = setup(replace(WORK, writable=False), enrolled=(WORK,))
    message = failed(await execute(service, "create_event", args), "PERMISSION_DENIED")
    assert "read-only" in message and "'Work'" in message
    # A stale read-only flag does not block a calendar that became writable.
    dav, service = setup(WORK, enrolled=(replace(WORK, writable=False),))
    ok(await execute(service, "create_event", args))


# Reading ---------------------------------------------------------------------------


async def test_listing_is_sorted_across_calendars_and_compact():
    dav, service = setup(PERSONAL, WORK)
    dav.add(WORK, "b.ics", event("b", "20250329T090000Z", "20250329T100000Z", "Later"))
    dav.add(
        PERSONAL, "a.ics", event("a", "20250328T090000Z", "20250328T100000Z", "Sooner")
    )
    dav.add(PERSONAL, "r.ics", RECURRING)
    data = ok(
        await execute(
            service, "list_events", {"from": "2025-03-28", "to": "2025-03-29"}
        )
    )
    titles = [e["title"] for e in data["events"]]
    assert titles == ["Sooner", "Recurring synthetic event", "Later"]
    assert data["events"][0] == {
        "event_id": event_id(PERSONAL, PERSONAL.href + "a.ics"),
        "calendar": "Personal",
        "title": "Sooner",
        "start": "2025-03-28T10:00:00+01:00",
        "end": "2025-03-28T11:00:00+01:00",
        "all_day": False,
        "timezone": "Europe/Berlin",
    }
    assert data["events"][1]["recurring"] is True
    assert data["events"][1]["occurrence"] == "2025-03-29T09:00:00+01:00"
    assert "truncated" not in data


async def test_large_listing_is_truncated_with_hint():
    dav, service = setup(PERSONAL)
    dav.add(
        PERSONAL,
        "h.ics",
        event(
            "h", "20250301T000000Z", "20250301T001000Z", "Ping", "RRULE:FREQ=HOURLY\r\n"
        ),
    )
    data = ok(
        await execute(
            service, "list_events", {"from": "2025-03-01", "to": "2025-03-31"}
        )
    )
    assert len(data["events"]) == 250
    assert data["truncated"] is True and "narrow" in data["hint"]


async def test_bad_resource_is_reported_not_fatal():
    dav, service = setup(PERSONAL, WORK)
    dav.add(
        PERSONAL, "a.ics", event("a", "20250328T090000Z", "20250328T100000Z", "Good")
    )
    bad = dav.add(PERSONAL, "b.ics", BROKEN)
    dav.failing[WORK.href] = 403
    data = ok(await execute(service, "upcoming_events", {}))
    assert data["events"][0]["title"] == "Good"
    assert data["events"][1] == {
        "event_id": bad,
        "calendar": "Personal",
        "unreadable": True,
    }
    assert data["skipped_calendars"] == ["Work"]


async def test_listing_fails_only_when_everything_is_unreadable():
    dav, service = setup(PERSONAL)
    dav.add(PERSONAL, "b.ics", BROKEN)
    failed(await execute(service, "upcoming_events", {}), "BAD_RESPONSE")
    dav, service = setup(PERSONAL)
    dav.failing[PERSONAL.href] = 403
    failed(await execute(service, "upcoming_events", {}), "PERMISSION_DENIED")


async def test_upcoming_uses_the_clock_and_search_matches_words():
    dav, service = setup(PERSONAL)
    place = "LOCATION:Main street\r\n"
    for name, day, title, extra in (
        ("a", "20250328", "Dentist", place),
        ("b", "20250410", "Dentist again", ""),
        ("c", "20250901", "Dentist far", ""),
    ):
        dav.add(
            PERSONAL,
            name + ".ics",
            event(name, day + "T090000Z", day + "T100000Z", title, extra),
        )
    data = ok(await execute(service, "upcoming_events", {"days": 3}))
    assert [e["title"] for e in data["events"]] == ["Dentist"]
    data = ok(await execute(service, "search_events", {"text": "DENTIST"}))
    # The default window ends 180 days from now, so September is included.
    assert len(data["events"]) == 3
    data = ok(await execute(service, "search_events", {"text": "dentist main"}))
    assert [e["title"] for e in data["events"]] == ["Dentist"]


async def test_get_event_probes_calendars_and_shows_details():
    dav, service = setup(PERSONAL, WORK)
    identifier = dav.add(WORK, "r.ics", RECURRING)
    data = ok(await execute(service, "get_event", {"event": identifier}))
    assert data["calendar"] == "Work" and data["revision"] == '"e1"'
    assert data["events"][0]["reminder_minutes"] == [15]
    missing = event_id(WORK, WORK.href + "x.ics")
    message = failed(
        await execute(service, "get_event", {"event_id": missing}), "NOT_FOUND"
    )
    assert "fresh listing" in message


async def test_find_free_time_respects_busy_and_working_hours():
    dav, service = setup(PERSONAL, WORK)
    dav.add(
        PERSONAL, "a.ics", event("a", "20250401T080000Z", "20250401T090000Z", "Busy")
    )
    dav.add(
        WORK,
        "b.ics",
        event(
            "b",
            "20250401T120000Z",
            "20250401T130000Z",
            "Free",
            "TRANSP:TRANSPARENT\r\n",
        ),
    )
    dav.add(
        WORK,
        "c.ics",
        event(
            "c", "20250401T130000Z", "20250401T140000Z", "Gone", "STATUS:CANCELLED\r\n"
        ),
    )
    args = {
        "duration_minutes": 60,
        "from": "2025-04-01",
        "to": "2025-04-01",
        "working_hours": "09:00-17:00",
    }
    failed(await execute(service, "find_free_time", args), "INVALID_INPUT")
    data = ok(await execute(service, "find_free_time", args, tools=FULL))
    assert data["free"] == [
        {
            "start": "2025-04-01T09:00:00+02:00",
            "end": "2025-04-01T10:00:00+02:00",
            "minutes": 60,
        },
        {
            "start": "2025-04-01T11:00:00+02:00",
            "end": "2025-04-01T17:00:00+02:00",
            "minutes": 360,
        },
    ]
    args["calendars"] = "Work"
    data = ok(await execute(service, "find_free_time", args, tools=FULL))
    assert data["free"][0]["minutes"] == 480


# Writing events ----------------------------------------------------------------------


async def test_create_event_with_repeat_and_reminder():
    dav, service = setup(PERSONAL)
    data = ok(
        await execute(
            service,
            "create_event",
            {
                "title": "Standup",
                "start": "2025-04-07T09:00",
                "duration_minutes": 15,
                "repeat": "weekdays",
                "repeat_until": "2025-04-30",
                "reminder_minutes": 5,
            },
        )
    )
    assert data["created"] is True
    assert (data["start"], data["end"]) == (
        "2025-04-07T09:00:00+02:00",
        "2025-04-07T09:15:00+02:00",
    )
    [put] = dav.writes()
    assert put.headers["If-None-Match"] == "*"
    stored = Calendar.from_ical(dav.store[put.url.path][0]).walk("VEVENT")[0]
    assert stored["RRULE"]["BYDAY"] == ["MO", "TU", "WE", "TH", "FR"]
    assert stored.walk("VALARM")[0]["TRIGGER"].to_ical() == b"-PT5M"
    data = ok(
        await execute(
            service, "list_events", {"from": "2025-04-07", "to": "2025-04-13"}
        )
    )
    assert len(data["events"]) == 5


async def test_update_keeps_duration_and_uses_fresh_revision():
    dav, service = setup(PERSONAL)
    identifier = dav.add(
        PERSONAL, "a.ics", event("a", "20250401T080000Z", "20250401T083000Z", "Call")
    )
    data = ok(
        await execute(
            service, "update_event", {"event": identifier, "start": "2025-04-02T15:00"}
        )
    )
    assert data == {
        "updated": True,
        "event_id": identifier,
        "calendar": "Personal",
        "revision": dav.store[PERSONAL.href + "a.ics"][1],
    }
    assert dav.writes()[0].headers["If-Match"] == '"e1"'
    view = ok(await execute(service, "get_event", {"event_id": identifier}))
    first = view["events"][0]
    assert (first["start"], first["end"]) == (
        "2025-04-02T15:00:00+02:00",
        "2025-04-02T15:30:00+02:00",
    )


async def test_update_with_stale_or_unquoted_revision():
    dav, service = setup(PERSONAL)
    identifier = dav.add(
        PERSONAL, "a.ics", event("a", "20250401T080000Z", "20250401T083000Z", "Call")
    )
    stale = {"event_id": identifier, "revision": '"old"', "title": "y"}
    failed(await execute(service, "update_event", stale), "CONFLICT")
    assert not dav.writes()
    bare = {"event_id": identifier, "revision": "e1", "title": "y"}
    ok(await execute(service, "update_event", bare))


async def test_get_put_race_has_revision_and_no_retry():
    dav, service = setup(PERSONAL)
    identifier = dav.add(PERSONAL, "a.ics", SIMPLE)

    def racing(request):
        if request.method == "PUT":
            dav.store[request.url.path] = (b"other", '"moved"')
        return dav(request)

    service.transport_factory = lambda: httpx.MockTransport(racing)
    args = {"event_id": identifier, "title": "Changed"}
    failed(await execute(service, "update_event", args), "CONFLICT")
    assert [r.method for r in dav.requests] == ["GET", "PUT"]


async def test_recurring_update_needs_a_scope_and_this_scope_overrides():
    dav, service = setup(PERSONAL)
    identifier = dav.add(PERSONAL, "r.ics", RECURRING)
    message = failed(
        await execute(service, "update_event", {"event_id": identifier, "title": "x"}),
        "INVALID_INPUT",
    )
    assert "scope" in message
    once = {
        "event_id": identifier,
        "scope": "this",
        "occurrence": "2025-03-22T09:00:00+01:00",
        "title": "Only once",
    }
    ok(await execute(service, "update_event", once))
    data = ok(
        await execute(
            service, "list_events", {"from": "2025-03-20", "to": "2025-03-23"}
        )
    )
    assert [e["title"] for e in data["events"]] == ["Only once"]


# Tasks -------------------------------------------------------------------------------


async def test_task_lifecycle():
    dav, service = setup(PERSONAL, TASKS)
    data = ok(
        await execute(
            service,
            "create_task",
            {"title": "Pay rent", "due": "2025-04-01", "priority": "high"},
        )
    )
    assert (data["list"], data["due"], data["priority"]) == (
        "Tasks",
        "2025-04-01",
        "high",
    )
    task = data["task_id"]
    dav.add(TASKS, "z.ics", TASK)
    listed = ok(await execute(service, "list_tasks", {}))["tasks"]
    assert [t["title"] for t in listed] == ["Pay rent", "Synthetic meeting"]
    ok(await execute(service, "complete_task", {"task": task, "list": "tasks"}))
    listed = ok(await execute(service, "list_tasks", {}))["tasks"]
    assert [t["title"] for t in listed] == ["Synthetic meeting"]
    done = ok(await execute(service, "list_tasks", {"include_completed": True}))
    assert done["tasks"][-1]["completed"] is True
    change = {"task_id": task, "completed": False, "due": ""}
    failed(await execute(service, "update_task", change), "INVALID_INPUT")
    data = ok(await execute(service, "update_task", change, tools=FULL))
    assert data["completed"] is False and "due" not in data


async def test_tasks_without_a_task_list_explain_setup():
    dav, service = setup(PERSONAL)
    message = failed(await execute(service, "list_tasks", {}), "NOT_FOUND")
    assert "setup" in message


# Doctor -----------------------------------------------------------------------------


async def test_doctor_reports_each_check_without_secrets():
    dav, service = setup(PERSONAL, TASKS)
    checks = ok(await execute(service, "doctor", {}))["checks"]
    assert {c["check"]: c["status"] for c in checks} == {
        "profile": "pass",
        "credential": "pass",
        "connection": "pass",
        "calendars": "pass",
        "task_lists": "pass",
        "trash_bin": "pass",
    }
    assert "30 days" in checks[-1]["detail"]
    text = json.dumps(checks)
    assert "synthetic-password" not in text and "cloud.example.test" not in text


async def test_doctor_warns_and_fails_usefully():
    dav, service = setup(PERSONAL, enrolled=(PERSONAL, WORK))
    dav.retention = "0"
    checks = {
        c["check"]: c for c in await run_doctor(service.profile_loader, dav.transport)
    }
    assert checks["calendars"]["status"] == "warn"
    assert "Work" in checks["calendars"]["detail"]
    assert checks["task_lists"]["status"] == "warn"
    assert checks["trash_bin"]["status"] == "warn"

    checks = await run_doctor(
        service.profile_loader,
        lambda: httpx.MockTransport(lambda r: httpx.Response(401)),
    )
    assert checks[-1]["check"] == "connection" and checks[-1]["status"] == "fail"
    assert "AUTH_FAILED" in checks[-1]["detail"]

    def locked():
        raise SafeError(ErrorCode.CREDENTIALS_LOCKED)

    checks = await run_doctor(locked)
    assert [(c["check"], c["status"]) for c in checks] == [("profile", "fail")]
    assert "CREDENTIALS_LOCKED" in checks[0]["detail"]


# Deleting ---------------------------------------------------------------------------


def deleting(tmp_path, *calendars, **policy):
    return setup(
        *(calendars or (PERSONAL,)),
        delete_policy=DeletePolicy(directory=tmp_path / "state", **policy),
    )


def answer(reason=None, log=None):
    async def confirm(message):
        if log is not None:
            log.append(message)
        return reason

    return confirm


def audit_lines(tmp_path):
    path = tmp_path / "state/audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_confirmed_delete_backs_up_audits_and_deletes(tmp_path):
    dav, service = deleting(tmp_path)
    identifier = dav.add(PERSONAL, "a.ics", SIMPLE)
    original = dav.store[PERSONAL.href + "a.ics"][0]
    asked = []
    data = ok(
        await execute(
            service,
            "delete_event",
            {"event_id": identifier},
            confirm=answer(None, asked),
        )
    )
    assert data["deleted"] is True and data["title"] == "Synthetic meeting"
    assert "Synthetic meeting" in asked[0] and "'Personal'" in asked[0]
    [delete] = dav.writes()
    assert delete.method == "DELETE" and delete.headers["If-Match"] == '"e1"'
    assert PERSONAL.href + "a.ics" not in dav.store
    folder = tmp_path / "state/deleted"
    backups = list(folder.iterdir())
    assert [p.name for p in backups] == [data["backup"]]
    assert backups[0].read_bytes() == original
    assert data["backup"].endswith("-Personal-synthetic-event.ics")
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "state/audit.jsonl").st_mode) == 0o600
    lines = audit_lines(tmp_path)
    assert [line["outcome"] for line in lines] == ["confirmed", "deleted"]
    assert lines[0]["uid"] == "synthetic-event" and lines[0]["backup"] == data["backup"]


@pytest.mark.parametrize(
    "confirm,reason",
    [
        (answer("the user declined"), "the user declined"),
        (answer("the user cancelled"), "the user cancelled"),
        (None, "the client cannot ask the user"),
    ],
)
async def test_unconfirmed_delete_never_deletes(tmp_path, confirm, reason):
    dav, service = deleting(tmp_path)
    identifier = dav.add(PERSONAL, "a.ics", SIMPLE)
    message = failed(
        await execute(
            service, "delete_event", {"event_id": identifier}, confirm=confirm
        ),
        "NOT_DELETED",
    )
    assert message.startswith("Not deleted: " + reason)
    assert "Do not retry" in message
    assert not dav.writes()
    assert not (tmp_path / "state/deleted").exists()
    assert audit_lines(tmp_path)[0]["outcome"].startswith("not confirmed")


async def test_confirmation_exception_never_deletes(tmp_path):
    dav, service = deleting(tmp_path)
    identifier = dav.add(PERSONAL, "a.ics", SIMPLE)

    async def broken(message):
        raise RuntimeError("transport closed")

    message = failed(
        await execute(
            service, "delete_event", {"event_id": identifier}, confirm=broken
        ),
        "NOT_DELETED",
    )
    assert "confirmation request failed" in message
    assert not dav.writes()


async def test_change_after_confirmation_is_not_deleted(tmp_path):
    dav, service = deleting(tmp_path)
    identifier = dav.add(PERSONAL, "a.ics", SIMPLE)

    async def meanwhile(message):
        dav.store[PERSONAL.href + "a.ics"] = (SIMPLE.encode(), '"edited"')

    message = failed(
        await execute(
            service, "delete_event", {"event_id": identifier}, confirm=meanwhile
        ),
        "NOT_DELETED",
    )
    assert "changed since confirmation" in message
    assert PERSONAL.href + "a.ics" in dav.store
    assert audit_lines(tmp_path)[-1]["outcome"] == "CONFLICT"


async def test_series_delete_is_refused_without_asking(tmp_path):
    dav, service = deleting(tmp_path)
    identifier = dav.add(PERSONAL, "r.ics", RECURRING)
    asked = []
    message = failed(
        await execute(
            service,
            "delete_event",
            {"event_id": identifier},
            confirm=answer(None, asked),
        ),
        "NOT_DELETED",
    )
    assert "series" in message and "occurrence" in message
    assert not asked and not dav.writes()


async def test_occurrence_delete_adds_exdate_with_if_match(tmp_path):
    dav, service = deleting(tmp_path)
    identifier = dav.add(PERSONAL, "r.ics", RECURRING)
    data = ok(
        await execute(
            service,
            "delete_event",
            {"event_id": identifier, "occurrence": "2025-03-22T09:00:00+01:00"},
            confirm=answer(),
        )
    )
    assert data["occurrence"] == "2025-03-22T09:00:00+01:00"
    [put] = dav.writes()
    assert put.method == "PUT" and put.headers["If-Match"] == '"e1"'
    assert b"EXDATE" in dav.store[PERSONAL.href + "r.ics"][0]
    listed = ok(
        await execute(
            service, "list_events", {"from": "2025-03-20", "to": "2025-03-23"}
        )
    )
    assert listed["events"] == []
    assert len(list((tmp_path / "state/deleted").iterdir())) == 1


async def test_delete_allowlist_and_session_limit(tmp_path):
    dav, service = deleting(
        tmp_path, PERSONAL, WORK, calendars=("work",), max_deletes=1
    )
    personal = dav.add(PERSONAL, "a.ics", SIMPLE)
    work = dav.add(WORK, "a.ics", SIMPLE)
    second = dav.add(WORK, "b.ics", SIMPLE)
    message = failed(
        await execute(
            service,
            "delete_event",
            {"event_id": personal, "calendar": "Personal"},
            confirm=answer(),
        ),
        "NOT_DELETED",
    )
    assert "not allowed" in message
    ok(
        await execute(
            service,
            "delete_event",
            {"event_id": work, "calendar": "Work"},
            confirm=answer(),
        )
    )
    message = failed(
        await execute(
            service,
            "delete_event",
            {"event_id": second, "calendar": "Work"},
            confirm=answer(),
        ),
        "NOT_DELETED",
    )
    assert "limit of 1" in message
    assert len(dav.writes()) == 1


async def test_unwritable_backup_stops_the_delete(tmp_path):
    (tmp_path / "blocked").write_text("")
    dav, service = setup(
        PERSONAL, delete_policy=DeletePolicy(directory=tmp_path / "blocked")
    )
    identifier = dav.add(PERSONAL, "a.ics", SIMPLE)
    message = failed(
        await execute(
            service, "delete_event", {"event_id": identifier}, confirm=answer()
        ),
        "NOT_DELETED",
    )
    assert "backup" in message and not dav.writes()


async def test_delete_task_and_restore_from_backup(tmp_path):
    dav, service = deleting(tmp_path, TASKS)
    identifier = dav.add(TASKS, "t.ics", TASK)
    data = ok(
        await execute(service, "delete_task", {"task_id": identifier}, confirm=answer())
    )
    assert data["task_id"] == identifier and not dav.store
    backup = tmp_path / "state/deleted" / data["backup"]
    restored = await service.restore(backup.read_bytes())
    assert restored["restored"] is True and restored["calendar"] == "Tasks"
    assert dav.writes()[-1].headers["If-None-Match"] == "*"
    assert list(dav.store.values())[0][0] == backup.read_bytes()


async def test_delete_tools_require_the_flag():
    dav, service = setup(PERSONAL)
    identifier = dav.add(PERSONAL, "a.ics", SIMPLE)
    failed(
        await execute(
            service, "delete_event", {"event_id": identifier}, confirm=answer()
        ),
        "INVALID_INPUT",
    )
    assert not dav.writes()


# Elicitation ---------------------------------------------------------------------------


class Session:
    def __init__(self, capable=True, result=None, error=None):
        self.capable, self.result, self.error = capable, result, error

    def check_client_capability(self, capabilities):
        return self.capable

    async def elicit_form(self, message, schema, related_request_id=None):
        assert schema["properties"]["confirm"]["type"] == "boolean"
        assert related_request_id == 7
        if self.error:
            raise self.error
        return self.result


def reply(action, content=None):
    return Session(result=SimpleNamespace(action=action, content=content))


@pytest.mark.parametrize(
    "session,expected",
    [
        (reply("accept", {"confirm": True}), None),
        (reply("accept", {}), None),
        (reply("accept", {"confirm": False}), "the user did not confirm"),
        (reply("decline"), "the user declined"),
        (reply("cancel"), "the user cancelled"),
        (Session(capable=False), "the client cannot ask the user for confirmation"),
        (Session(error=RuntimeError("x")), "the confirmation request failed"),
    ],
)
async def test_confirmation_answers(session, expected):
    context = SimpleNamespace(session=session, request_id=7)
    assert await _confirm(context, "Delete?") == expected
