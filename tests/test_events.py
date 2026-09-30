from datetime import datetime, timezone
from pathlib import Path

import pytest
from icalendar import Calendar

from hermes_caldav_mcp.events import (
    build_event,
    event_views,
    patch_event,
    validate_interval,
)
from hermes_caldav_mcp.types import CalendarResource, EventFields, EventPatch, SafeError

SIMPLE = (Path(__file__).parent / "fixtures/simple.ics").read_bytes()
RECURRING = (Path(__file__).parent / "fixtures/recurring.ics").read_bytes()


def test_edit_utc_event_keeps_utc_when_profile_default_differs():
    data = patch_event(
        SIMPLE,
        EventPatch(start="2025-03-29T12:00:00Z", end="2025-03-29T13:00:00Z"),
        default_timezone="Europe/Berlin",
        whole_series=False,
    )
    assert (
        event_views(resource(data), "Europe/Berlin", minimal=True)[0]["start"]
        == "2025-03-29T12:00:00+00:00"
    )


@pytest.mark.parametrize(
    "stamp", ["2025-10-26T02:30:00+02:00", "2025-10-26T02:30:00+01:00"]
)
def test_ambiguous_iana_write_is_refused_instead_of_losing_fold(stamp):
    with pytest.raises(SafeError):
        build_event(
            EventFields(
                "Fold", stamp, "2025-10-26T04:00:00+01:00", timezone="Europe/Berlin"
            ),
            "UTC",
        )


def test_fractional_time_is_not_silently_truncated():
    with pytest.raises(SafeError):
        validate_interval("2025-01-01T00:00:00.5Z", "2025-01-02T00:00:00Z")
    with pytest.raises(SafeError):
        build_event(
            EventFields("Fraction", "2025-01-01T00:00:00.5Z", "2025-01-01T01:00:00Z"),
            "UTC",
        )


def test_date_event_does_not_ignore_timezone_patch():
    _, data = build_event(
        EventFields("Day", "2025-01-01", "2025-01-02", all_day=True), "UTC"
    )
    with pytest.raises(SafeError):
        patch_event(
            data,
            EventPatch(timezone="Europe/Berlin"),
            default_timezone="UTC",
            whole_series=False,
        )


def test_timezone_definition_generation_has_bounded_range():
    with pytest.raises(SafeError) as caught:
        build_event(
            EventFields(
                "Too long",
                "2000-01-01T10:00:00+01:00",
                "2050-01-01T10:00:00+01:00",
                timezone="Europe/Berlin",
            ),
            "UTC",
        )
    assert caught.value.code.value == "LIMIT_EXCEEDED"


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
    event = Calendar.from_ical(data).walk("VEVENT")[0]
    assert str(event["UID"]) == uid
    assert "ATTENDEE" not in event
    assert str(event["DESCRIPTION"]) == fields.description
    assert len(Calendar.from_ical(data).walk("VEVENT")) == 1


def test_create_all_day_and_explicit_iana_time():
    _, data = build_event(
        EventFields("Day", "2025-03-29", "2025-03-30", all_day=True), "Europe/Berlin"
    )
    assert (
        event_views(resource(data), "Europe/Berlin", minimal=True)[0]["end"]
        == "2025-03-30"
    )
    _, data = build_event(
        EventFields(
            "Timed",
            "2025-03-30T03:30:00+02:00",
            "2025-03-30T04:30:00+02:00",
            timezone="Europe/Berlin",
        ),
        "UTC",
    )
    view = event_views(resource(data), "UTC", minimal=True)[0]
    assert view["start"] == "2025-03-30T03:30:00+02:00"
    assert view["timezone"] == "Europe/Berlin"


@pytest.mark.parametrize(
    "fields",
    [
        EventFields("", "2025-01-01", "2025-01-02", all_day=True),
        EventFields("x" * 201, "2025-01-01", "2025-01-02", all_day=True),
        EventFields("x", "2025-01-01", "2025-01-02"),
        EventFields("x", "2025-01-01", "2025-01-01", all_day=True),
        EventFields(
            "x",
            "2025-03-30T02:30:00+01:00",
            "2025-03-30T04:30:00+02:00",
            timezone="Europe/Berlin",
        ),
        EventFields("x", "2025-01-01T10:00:00", "2025-01-01T11:00:00"),
        EventFields(
            "x", "2025-01-01", "2025-01-02", all_day=True, description="x" * 4097
        ),
    ],
)
def test_create_invalid_fields_fail(fields):
    with pytest.raises(SafeError):
        build_event(fields, "UTC")


def test_patch_retains_complete_calendar_semantics():
    before = Calendar.from_ical(RECURRING)
    data = patch_event(
        RECURRING,
        EventPatch(title="Changed", description=""),
        default_timezone="UTC",
        whole_series=True,
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
    assert not str(b.get("DESCRIPTION", ""))
    assert "LAST-MODIFIED" in b


@pytest.mark.parametrize(
    "original,patch,whole",
    [
        (RECURRING, EventPatch(title="Changed"), False),
        (
            RECURRING,
            EventPatch(
                start="2025-03-01T11:00:00+02:00", end="2025-03-01T12:00:00+02:00"
            ),
            True,
        ),
        (
            SIMPLE.replace(b"SUMMARY:", b"ATTENDEE:mailto:test@example.test\nSUMMARY:"),
            EventPatch(title="Changed"),
            False,
        ),
        (
            SIMPLE.replace(b"VERSION:2.0", b"VERSION:2.0\nMETHOD:REQUEST"),
            EventPatch(title="Changed"),
            False,
        ),
        (SIMPLE, EventPatch(), False),
    ],
)
def test_unsafe_edits_fail(original, patch, whole):
    with pytest.raises(SafeError):
        patch_event(original, patch, default_timezone="UTC", whole_series=whole)


def test_timing_edit_retains_uid_and_untouched_text():
    data = patch_event(
        SIMPLE,
        EventPatch(start="2025-03-29T12:00:00Z", end="2025-03-29T13:00:00Z"),
        default_timezone="UTC",
        whole_series=False,
    )
    view = event_views(resource(data), "UTC", minimal=False)[0]
    assert view["uid"] == "synthetic-event"
    assert view["start"] == "2025-03-29T12:00:00+00:00"
    assert view["description"].startswith("Untrusted text")


def resource(data):
    return CalendarResource("event-1", "/selected/a.ics", '"rev1"', data)


def test_minimal_reads_do_not_disclose_descriptions_or_links(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("side effect while parsing text")

    monkeypatch.setattr("builtins.open", forbidden)
    views = event_views(resource(SIMPLE), "UTC", minimal=True)
    assert views == [
        {
            "event_id": "event-1",
            "revision": '"rev1"',
            "title": "Synthetic meeting",
            "start": "2025-03-29T10:00:00+00:00",
            "end": "2025-03-29T11:00:00+00:00",
            "all_day": False,
            "timezone": "UTC",
            "recurring": False,
            "occurrence": None,
        }
    ]
    details = event_views(resource(SIMPLE), "UTC", minimal=False)[0]
    assert "ignore previous instructions" in details["description"]
    assert "raw_ics" not in details


def test_all_day_end_is_exclusive():
    data = (
        SIMPLE.replace(b"20250329T100000Z", b"20250329")
        .replace(b"20250329T110000Z", b"20250330")
        .replace(b"DTSTART:", b"DTSTART;VALUE=DATE:")
        .replace(b"DTEND:", b"DTEND;VALUE=DATE:")
    )
    view = event_views(resource(data), "UTC", minimal=True)[0]
    assert view["start"] == "2025-03-29"
    assert view["end"] == "2025-03-30"
    assert view["all_day"] is True


@pytest.mark.parametrize(
    "start,end",
    [
        ("2025-01-01", "2025-01-02"),
        ("2025-01-01T00:00:00", "2025-01-02T00:00:00"),
        ("bad", "bad"),
        ("2025-01-02T00:00:00Z", "2025-01-01T00:00:00Z"),
        ("2025-01-01T00:00:00Z", "2025-04-04T00:00:01Z"),
    ],
)
def test_invalid_query_intervals_fail(start, end):
    with pytest.raises(SafeError):
        validate_interval(start, end)


def test_exact_query_limit():
    start, end = validate_interval("2025-01-01T01:00:00+01:00", "2025-04-04T00:00:00Z")
    assert start == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert (end - start).days == 93


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
    assert (
        event_views(resource(data), "Europe/Berlin", minimal=True)[0]["start"]
        == expected
    )


@pytest.mark.parametrize("stamp", [b"20250330T023000", b"20251026T023000"])
def test_ambiguous_or_nonexistent_floating_local_time_fails(stamp):
    data = SIMPLE.replace(b"20250329T100000Z", stamp).replace(
        b"DTEND:20250329T110000Z", b"DURATION:PT1H"
    )
    with pytest.raises(SafeError):
        event_views(resource(data), "Europe/Berlin", minimal=True)


@pytest.mark.parametrize(
    "data",
    [
        b"garbage",
        SIMPLE.replace(b"UID:synthetic-event", b"UID:a\nUID:b"),
        SIMPLE.replace(b"DTSTART:20250329T100000Z", b"DTSTART:invalid"),
        SIMPLE.replace(b"END:VCALENDAR", b"END:INVALID"),
    ],
)
def test_malformed_event_is_not_silently_accepted(data):
    with pytest.raises(SafeError):
        event_views(resource(data), "UTC", minimal=True)
