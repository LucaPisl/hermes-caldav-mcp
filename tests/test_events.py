from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar

from hermes_caldav_mcp.events import (
    build_event,
    build_task,
    event_details,
    event_view,
    exclude_occurrence,
    occurrences,
    parse_calendar,
    parse_range,
    patch_event,
    patch_task,
    task_view,
)
from hermes_caldav_mcp.types import (
    CalendarResource,
    EventFields,
    EventPatch,
    SafeError,
    TaskPatch,
)

SIMPLE = (Path(__file__).parent / "fixtures/simple.ics").read_bytes()
RECURRING = (Path(__file__).parent / "fixtures/recurring.ics").read_bytes()
BERLIN = ZoneInfo("Europe/Berlin")
UTC = ZoneInfo("UTC")
MARCH = datetime(2025, 3, 1, tzinfo=BERLIN)


def resource(data):
    return CalendarResource("event-1", "/selected/a.ics", '"rev1"', data)


def views(data, zone=UTC, a=datetime(2025, 1, 1, tzinfo=UTC), days=366):
    calendar = parse_calendar(data)
    return [
        event_view(resource(data), "Personal", c, s, e, rid, zone)
        for c, s, e, rid in occurrences(calendar, a, a + timedelta(days=days), zone)
    ]


def master(data):
    return Calendar.from_ical(data).walk("VEVENT")[0]


# Lenient input ---------------------------------------------------------------


def test_naive_time_is_wall_clock_in_profile_zone_with_default_hour():
    _, data = build_event(EventFields("Dentist", "2025-07-01T09:30"), "Europe/Berlin")
    view = views(data)[0]
    assert view["start"] == "2025-07-01T09:30:00+02:00"
    assert view["end"] == "2025-07-01T10:30:00+02:00"
    assert view["timezone"] == "Europe/Berlin"


def test_duration_minutes_and_exclusive_all_day_default():
    _, data = build_event(
        EventFields("Call", "2025-07-01T09:00", duration_minutes=45), "UTC"
    )
    assert views(data)[0]["end"] == "2025-07-01T09:45:00+00:00"
    _, data = build_event(EventFields("Day", "2025-07-01"), "UTC")
    assert views(data)[0]["end"] == "2025-07-02"
    _, data = build_event(EventFields("Day", "2025-07-01", "2025-07-01"), "UTC")
    assert views(data)[0]["end"] == "2025-07-02"


def test_offset_is_an_instant_but_wall_clock_when_a_zone_is_named():
    _, data = build_event(EventFields("x", "2025-07-01T08:00:00Z"), "Europe/Berlin")
    assert views(data)[0]["start"] == "2025-07-01T10:00:00+02:00"
    # A small model often appends Z by habit; the named zone's clock reading wins.
    _, data = build_event(
        EventFields("x", "2025-07-01T10:00:00Z", timezone="America/New_York"), "UTC"
    )
    view = views(data)[0]
    assert view["start"] == "2025-07-01T10:00:00-04:00"
    assert view["timezone"] == "America/New_York"


@pytest.mark.parametrize(
    "fields,hint",
    [
        (EventFields("", "2025-01-01"), "title"),
        (EventFields("x" * 201, "2025-01-01"), "title"),
        (EventFields("x", "tomorrow"), "start"),
        (EventFields("x", "2025-01-01", "2025-01-01T10:00"), "end"),
        (EventFields("x", "2025-01-01T10:00", "2025-01-01T09:00"), "end"),
        (EventFields("x", "2025-01-01T10:00", "2025-01-01T11:00", 30), "duration"),
        (EventFields("x", "2025-01-01T00:00:00.5Z"), "start"),
        (EventFields("x", "2025-03-30T02:30", timezone="Europe/Berlin"), "skipped"),
        (EventFields("x", "2025-10-26T02:30", timezone="Europe/Berlin"), "repeated"),
        (EventFields("x", "2025-01-01", description="x" * 4097), "description"),
        (EventFields("x", "2025-01-01", timezone="Mars/Base"), "IANA"),
        (
            EventFields(
                "x",
                "2025-01-01",
                repeat={"freq": "daily", "count": 2, "until": "2025-02-01"},
            ),
            "count",
        ),
        (
            EventFields(
                "x", "2025-01-05", repeat={"freq": "daily", "until": "2025-01-01"}
            ),
            "until",
        ),
    ],
)
def test_invalid_fields_fail_with_actionable_hint(fields, hint):
    with pytest.raises(SafeError) as caught:
        build_event(fields, "UTC")
    assert caught.value.code.value == "INVALID_INPUT"
    assert hint in str(caught.value)


def test_hint_never_echoes_long_input():
    with pytest.raises(SafeError) as caught:
        build_event(EventFields("x", "s" * 60), "UTC")
    assert "s" * 41 not in str(caught.value)


@pytest.mark.parametrize(
    "stamp", ["2025-10-26T02:30:00+02:00", "2025-10-26T02:30:00+01:00"]
)
def test_ambiguous_wall_clock_in_named_zone_is_refused(stamp):
    with pytest.raises(SafeError):
        build_event(EventFields("Fold", stamp, timezone="Europe/Berlin"), "UTC")


def test_lone_carriage_returns_become_newlines():
    _, data = build_event(
        EventFields("x", "2025-01-01", description="a\rb\r\nc"), "UTC"
    )
    assert str(master(data)["DESCRIPTION"]) == "a\nb\nc"


def test_create_unique_uid_and_literal_property_text():
    fields = EventFields(
        "Test\\nATTENDEE:evil",
        "2025-03-29T10:00:00Z",
        "2025-03-29T11:00:00Z",
        description="text\nATTENDEE:mailto:evil@example.test\nEND:VEVENT",
    )
    uid, data = build_event(fields, "UTC")
    other_uid, _ = build_event(fields, "UTC")
    assert uid != other_uid
    event = master(data)
    assert str(event["UID"]) == uid
    assert "ATTENDEE" not in event
    assert str(event["DESCRIPTION"]) == fields.description
    assert len(Calendar.from_ical(data).walk("VEVENT")) == 1


def test_timezone_definition_has_observances_and_bounded_range():
    _, data = build_event(EventFields("x", "2025-07-01T10:00"), "Europe/Berlin")
    vtz = Calendar.from_ical(data).walk("VTIMEZONE")[0]
    assert vtz.walk("DAYLIGHT") and vtz.walk("STANDARD")
    with pytest.raises(SafeError) as caught:
        build_event(
            EventFields("x", "2000-01-01T10:00", "2050-01-01T10:00"), "Europe/Berlin"
        )
    assert caught.value.code.value == "LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2025-01-01", "2025-01-01", ("2024-12-31T23:00:00", "2025-01-01T23:00:00")),
        ("2025-01-01T10:00", None, ("2025-01-01T09:00:00", "2025-01-08T09:00:00")),
        (
            "2025-01-01 10:00",
            "2025-01-01T12:00:00+01:00",
            ("2025-01-01T09:00:00", "2025-01-01T11:00:00"),
        ),
    ],
)
def test_query_range_is_lenient(start, end, expected):
    a, b = parse_range(start, end, BERLIN)
    assert (a.isoformat()[:19], b.isoformat()[:19]) == expected


@pytest.mark.parametrize(
    "start,end,code",
    [
        ("bad", None, "INVALID_INPUT"),
        ("2025-01-02", "2025-01-01", "INVALID_INPUT"),
        ("2025-01-01T00:00:00.5Z", None, "INVALID_INPUT"),
        ("2025-01-01", "2026-01-03", "LIMIT_EXCEEDED"),
    ],
)
def test_invalid_query_ranges_fail(start, end, code):
    with pytest.raises(SafeError) as caught:
        parse_range(start, end, UTC)
    assert caught.value.code.value == code


def test_exact_query_limit():
    parse_range("2025-01-01T00:00:00Z", "2026-01-02T00:00:00Z", UTC)


# Reading ----------------------------------------------------------------------


def test_listing_view_is_compact_and_omits_text_and_revision(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("side effect while parsing text")

    monkeypatch.setattr("builtins.open", forbidden)
    assert views(SIMPLE) == [
        {
            "event_id": "event-1",
            "calendar": "Personal",
            "title": "Synthetic meeting",
            "start": "2025-03-29T10:00:00+00:00",
            "end": "2025-03-29T11:00:00+00:00",
            "all_day": False,
            "timezone": "UTC",
            "location": "Test room",
        }
    ]
    details = event_details(resource(SIMPLE), "Personal", UTC)
    assert details["revision"] == '"rev1"'
    assert "ignore previous instructions" in details["events"][0]["description"]


def test_utc_event_is_shown_in_profile_zone():
    view = views(SIMPLE, BERLIN)[0]
    assert (view["start"], view["timezone"]) == (
        "2025-03-29T11:00:00+01:00",
        "Europe/Berlin",
    )


def test_recurrence_expands_with_exdate_rdate_and_override():
    listed = views(RECURRING, BERLIN, MARCH, 30)
    starts = sorted(v["start"][:10] for v in listed)
    # Weekly from 03-01 without 03-08, plus RDATE 03-10; 03-15 is overridden.
    assert starts == [
        "2025-03-01",
        "2025-03-10",
        "2025-03-15",
        "2025-03-22",
        "2025-03-29",
    ]
    override = next(v for v in listed if v["title"] == "Detached override")
    assert override["start"] == "2025-03-15T11:00:00+01:00"
    assert override["occurrence"] == "2025-03-15T09:00:00+01:00"
    assert all(v["recurring"] for v in listed)


def test_window_selects_only_overlapping_occurrences():
    a = datetime(2025, 3, 22, tzinfo=BERLIN)
    assert [v["start"][:10] for v in views(RECURRING, BERLIN, a, 1)] == ["2025-03-22"]


def test_unbounded_series_is_capped_just_above_the_occurrence_limit():
    data = SIMPLE.replace(b"SUMMARY:", b"RRULE:FREQ=HOURLY\r\nSUMMARY:")
    found = occurrences(
        parse_calendar(data),
        datetime(2025, 3, 29, tzinfo=UTC),
        datetime(2025, 6, 1, tzinfo=UTC),
        UTC,
    )
    assert len(found) == 251


def test_counted_sub_daily_rule_is_refused():
    data = SIMPLE.replace(b"SUMMARY:", b"RRULE:FREQ=MINUTELY;COUNT=100000\r\nSUMMARY:")
    with pytest.raises(ValueError):
        occurrences(
            parse_calendar(data),
            datetime(2025, 3, 29, tzinfo=UTC),
            datetime(2025, 4, 1, tzinfo=UTC),
            UTC,
        )


def _tzid(tzid):
    return SIMPLE.replace(
        b"DTSTART:20250329T100000Z", b"DTSTART;TZID=" + tzid + b":20250329T100000"
    ).replace(b"DTEND:20250329T110000Z", b"DURATION:PT1H")


@pytest.mark.parametrize(
    "tzid,zone,start",
    [
        (b"W. Europe Standard Time", "Europe/Berlin", "2025-03-29T10:00:00+01:00"),
        (b"Custom/Nowhere", "UTC", "2025-03-29T10:00:00+00:00"),
    ],
)
def test_non_iana_tzid_is_mapped_or_falls_back(tzid, zone, start):
    view = views(_tzid(tzid))[0]
    assert (view["timezone"], view["start"]) == (zone, start)
    changed = patch_event(
        _tzid(tzid), EventPatch(start="2025-03-29T12:00"), default_timezone="UTC"
    )
    view = views(changed)[0]
    assert view["timezone"] == zone
    assert view["end"].startswith("2025-03-29T13:00")


@pytest.mark.parametrize(
    "stamp,expected",
    [
        (b"20250330T013000", "2025-03-30T01:30:00+01:00"),
        (b"20251026T033000", "2025-10-26T03:30:00+01:00"),
    ],
)
def test_iana_zone_retains_historical_dst_offset(stamp, expected):
    data = SIMPLE.replace(
        b"DTSTART:20250329T100000Z", b"DTSTART;TZID=Europe/Berlin:" + stamp
    ).replace(b"DTEND:20250329T110000Z", b"DURATION:PT1H")
    assert views(data)[0]["start"] == expected


def test_all_day_end_is_exclusive():
    data = (
        SIMPLE.replace(b"20250329T100000Z", b"20250329")
        .replace(b"20250329T110000Z", b"20250330")
        .replace(b"DTSTART:", b"DTSTART;VALUE=DATE:")
        .replace(b"DTEND:", b"DTEND;VALUE=DATE:")
    )
    view = views(data)[0]
    assert (view["start"], view["end"], view["all_day"]) == (
        "2025-03-29",
        "2025-03-30",
        True,
    )
    assert view["timezone"] is None


@pytest.mark.parametrize("stamp", [b"20250330T023000", b"20251026T023000"])
def test_ambiguous_or_nonexistent_floating_local_time_is_unreadable(stamp):
    data = SIMPLE.replace(b"20250329T100000Z", stamp).replace(
        b"DTEND:20250329T110000Z", b"DURATION:PT1H"
    )
    with pytest.raises(ValueError):
        views(data, BERLIN)


@pytest.mark.parametrize(
    "data",
    [
        b"garbage",
        SIMPLE.replace(b"UID:synthetic-event", b"UID:a\r\nUID:b"),
        SIMPLE.replace(b"DTSTART:20250329T100000Z", b"DTSTART:invalid"),
        SIMPLE.replace(b"END:VCALENDAR", b"END:INVALID"),
    ],
)
def test_malformed_event_is_not_silently_accepted(data):
    with pytest.raises(SafeError):
        parse_calendar(data)


# Editing ------------------------------------------------------------------------


def test_start_only_change_keeps_the_duration():
    data = SIMPLE.replace(b"DTEND:20250329T110000Z", b"DTEND:20250329T101500Z")
    changed = patch_event(
        data, EventPatch(start="2025-03-29T12:00"), default_timezone="UTC"
    )
    view = views(changed)[0]
    assert (view["start"], view["end"]) == (
        "2025-03-29T12:00:00+00:00",
        "2025-03-29T12:15:00+00:00",
    )
    assert str(master(changed)["UID"]) == "synthetic-event"
    assert "ignore previous" in str(master(changed)["DESCRIPTION"])


def test_utc_event_stays_utc_when_profile_default_differs():
    changed = patch_event(
        SIMPLE, EventPatch(start="2025-03-29T13:00"), default_timezone="Europe/Berlin"
    )
    assert master(changed)["DTSTART"].to_ical() == b"20250329T120000Z"


def test_timezone_only_change_keeps_the_clock_times():
    _, data = build_event(EventFields("x", "2025-07-01T09:00"), "Europe/Berlin")
    changed = patch_event(
        data, EventPatch(timezone="America/New_York"), default_timezone="Europe/Berlin"
    )
    view = views(changed, BERLIN)[0]
    assert (view["start"], view["end"]) == (
        "2025-07-01T09:00:00-04:00",
        "2025-07-01T10:00:00-04:00",
    )
    assert view["timezone"] == "America/New_York"


def test_date_event_rejects_timezone_patch():
    _, data = build_event(EventFields("Day", "2025-01-01"), "UTC")
    with pytest.raises(SafeError):
        patch_event(data, EventPatch(timezone="Europe/Berlin"), default_timezone="UTC")


def test_series_title_change_retains_complete_calendar_semantics():
    before = Calendar.from_ical(RECURRING)
    data = patch_event(
        RECURRING,
        EventPatch(title="Changed", description=""),
        default_timezone="UTC",
        scope="series",
    )
    after = Calendar.from_ical(data)
    a, b = before.walk("VEVENT")[0], after.walk("VEVENT")[0]
    for key in (
        "UID",
        "DTSTART",
        "DTEND",
        "RRULE",
        "EXDATE",
        "RDATE",
        "X-CUSTOM-EVENT",
    ):
        assert a[key].to_ical() == b[key].to_ical()
        assert a[key].params == b[key].params
    assert a.subcomponents[0].to_ical() == b.subcomponents[0].to_ical()
    assert before.walk("VTIMEZONE")[0].to_ical() == after.walk("VTIMEZONE")[0].to_ical()
    assert before.walk("VEVENT")[1].to_ical() == after.walk("VEVENT")[1].to_ical()
    assert before["X-CUSTOM-CALENDAR"] == after["X-CUSTOM-CALENDAR"]
    assert b["SUMMARY"] == "Changed"
    assert "DESCRIPTION" not in b
    assert "LAST-MODIFIED" in b


def test_scope_this_creates_an_override_for_one_occurrence():
    data = patch_event(
        RECURRING,
        EventPatch(title="Moved", start="2025-03-22T15:00"),
        default_timezone="Europe/Berlin",
        scope="this",
        occurrence="2025-03-22T09:00:00+01:00",
    )
    listed = views(data, BERLIN, MARCH, 30)
    moved = next(v for v in listed if v["title"] == "Moved")
    assert moved["occurrence"] == "2025-03-22T09:00:00+01:00"
    assert moved["start"].startswith("2025-03-22T")
    assert len(listed) == 5
    titles = [v["title"] for v in listed]
    assert titles.count("Recurring synthetic event") == 3


def test_occurrence_alone_edits_an_existing_override():
    data = patch_event(
        RECURRING,
        EventPatch(location="Room 2"),
        default_timezone="Europe/Berlin",
        occurrence="2025-03-15T09:00:00+01:00",
    )
    overrides = Calendar.from_ical(data).walk("VEVENT")[1:]
    assert len(overrides) == 1
    assert str(overrides[0]["LOCATION"]) == "Room 2"
    assert "LOCATION" not in master(data)


def test_series_shift_without_exceptions_keeps_duration():
    _, data = build_event(
        EventFields(
            "x", "2025-03-03T09:00", duration_minutes=15, repeat={"freq": "weekly"}
        ),
        "Europe/Berlin",
    )
    changed = patch_event(
        data,
        EventPatch(start="2025-03-04T10:00"),
        default_timezone="Europe/Berlin",
        scope="series",
    )
    first = views(changed, BERLIN, MARCH, 14)[0]
    assert (first["start"], first["end"]) == (
        "2025-03-04T10:00:00+01:00",
        "2025-03-04T10:15:00+01:00",
    )


@pytest.mark.parametrize(
    "original,patch,kwargs,hint",
    [
        (RECURRING, EventPatch(title="Changed"), {}, "scope"),
        (
            RECURRING,
            EventPatch(start="2025-03-01T11:00"),
            {"scope": "series"},
            "occurrences",
        ),
        (RECURRING, EventPatch(title="x"), {"scope": "this"}, "occurrence"),
        (
            RECURRING,
            EventPatch(title="x"),
            {"occurrence": "2025-03-08T09:00:00+01:00"},
            "no occurrence",
        ),
        (
            RECURRING,
            EventPatch(repeat={"freq": "daily"}),
            {"occurrence": "2025-03-22T09:00:00+01:00"},
            "series",
        ),
        (
            SIMPLE,
            EventPatch(title="x"),
            {"occurrence": "2025-03-29T10:00:00Z"},
            "does not repeat",
        ),
        (
            SIMPLE.replace(
                b"SUMMARY:", b"ATTENDEE:mailto:test@example.test\r\nSUMMARY:"
            ),
            EventPatch(title="Changed"),
            {},
            "Invitations",
        ),
        (
            SIMPLE.replace(b"VERSION:2.0", b"VERSION:2.0\r\nMETHOD:REQUEST"),
            EventPatch(title="Changed"),
            {},
            "Invitations",
        ),
        (SIMPLE, EventPatch(), {}, "at least one"),
    ],
)
def test_unsafe_edits_fail_with_hint(original, patch, kwargs, hint):
    with pytest.raises(SafeError) as caught:
        patch_event(original, patch, default_timezone="Europe/Berlin", **kwargs)
    assert hint in str(caught.value)


def test_exclude_occurrence_adds_exdate_and_drops_its_override():
    data, _, start, _ = exclude_occurrence(
        RECURRING, "2025-03-15T09:00:00+01:00", "Europe/Berlin"
    )
    assert start.astimezone(BERLIN).isoformat() == "2025-03-15T11:00:00+01:00"
    listed = views(data, BERLIN, MARCH, 30)
    assert "2025-03-15" not in [v["start"][:10] for v in listed]
    assert len(listed) == 4
    assert len(Calendar.from_ical(data).walk("VEVENT")) == 1


def test_exclude_occurrence_refuses_single_event():
    with pytest.raises(SafeError):
        exclude_occurrence(SIMPLE, "2025-03-29T10:00:00Z", "UTC")


# Repetition and reminders ------------------------------------------------------


def test_repeat_builds_rrule_with_weekdays_and_until():
    _, data = build_event(
        EventFields(
            "Standup",
            "2025-03-03T09:00",
            repeat={"freq": "weekly", "weekdays": ["MO", "WE"], "until": "2025-03-12"},
        ),
        "Europe/Berlin",
    )
    rule = master(data)["RRULE"]
    assert rule["FREQ"] == ["WEEKLY"] and rule["BYDAY"] == ["MO", "WE"]
    listed = views(data, BERLIN, MARCH, 30)
    assert [v["start"][:10] for v in listed] == [
        "2025-03-03",
        "2025-03-05",
        "2025-03-10",
        "2025-03-12",
    ]
    assert all(v["recurring"] for v in listed)


def test_repeat_count_and_interval():
    _, data = build_event(
        EventFields(
            "x", "2025-03-01", repeat={"freq": "daily", "interval": 2, "count": 3}
        ),
        "UTC",
    )
    assert [v["start"] for v in views(data, UTC, MARCH, 30)] == [
        "2025-03-01",
        "2025-03-03",
        "2025-03-05",
    ]


def test_repeat_none_stops_a_series():
    _, data = build_event(
        EventFields("x", "2025-03-03", repeat={"freq": "daily"}), "UTC"
    )
    changed = patch_event(
        data,
        EventPatch(repeat={"freq": "none"}),
        default_timezone="UTC",
        scope="series",
    )
    assert "RRULE" not in master(changed)


def test_reminders_are_display_alarms_and_replaceable():
    _, data = build_event(
        EventFields("x", "2025-03-03T09:00", reminder_minutes=[60, 10, 10]), "UTC"
    )
    alarms = master(data).walk("VALARM")
    assert len(alarms) == 2
    assert {str(a["ACTION"]) for a in alarms} == {"DISPLAY"}
    details = event_details(resource(data), "P", UTC)
    assert details["events"][0]["reminder_minutes"] == [10, 60]
    changed = patch_event(data, EventPatch(reminder_minutes=[]), default_timezone="UTC")
    assert not master(changed).walk("VALARM")


def test_details_show_existing_series_reminder_and_rule():
    details = event_details(resource(RECURRING), "P", BERLIN)
    series = details["events"][0]
    assert series["reminder_minutes"] == [15]
    assert series["repeat"] == "FREQ=WEEKLY;COUNT=10"
    assert series["recurring"] is True
    assert details["invitation_managed"] is False


# Tasks ---------------------------------------------------------------------------


def test_task_lifecycle():
    _, data = build_task(
        TaskPatch("Pay rent", "2025-04-01", "IBAN in notes", "high"), "Europe/Berlin"
    )
    assert task_view(resource(data), "Tasks", BERLIN) == {
        "task_id": "event-1",
        "list": "Tasks",
        "title": "Pay rent",
        "completed": False,
        "due": "2025-04-01",
        "priority": "high",
        "notes": "IBAN in notes",
    }
    todo = Calendar.from_ical(data).walk("VTODO")[0]
    assert todo["DUE"].dt == date(2025, 4, 1)
    done = patch_task(data, TaskPatch(completed=True), default_timezone="Europe/Berlin")
    todo = Calendar.from_ical(done).walk("VTODO")[0]
    assert str(todo["STATUS"]) == "COMPLETED" and "COMPLETED" in todo
    assert task_view(resource(done), "Tasks", BERLIN)["completed"] is True
    cleared = patch_task(
        done,
        TaskPatch(due="", priority="none", completed=False),
        default_timezone="UTC",
    )
    view = task_view(resource(cleared), "Tasks", BERLIN)
    assert view["completed"] is False
    assert "due" not in view and "priority" not in view


def test_timed_task_due_and_refused_edits():
    _, data = build_task(TaskPatch("Call", "2025-04-01T17:00"), "Europe/Berlin")
    assert task_view(resource(data), "T", BERLIN)["due"] == "2025-04-01T17:00:00+02:00"
    with pytest.raises(SafeError):
        patch_task(data, TaskPatch(), default_timezone="UTC")
    with pytest.raises(SafeError):
        build_task(TaskPatch("", None), "UTC")
    repeating = data.replace(b"END:VTODO", b"RRULE:FREQ=DAILY\r\nEND:VTODO")
    with pytest.raises(SafeError) as caught:
        patch_task(repeating, TaskPatch(title="x"), default_timezone="UTC")
    assert caught.value.code.value == "UNSUPPORTED_OPERATION"
    assert task_view(resource(repeating), "T", BERLIN)["recurring"] is True
