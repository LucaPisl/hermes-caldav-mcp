"""Interpret calendar data without following links or executing remote text."""

import re
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, Timezone

from .types import (
    MAX_EVENT_BYTES,
    MAX_OCCURRENCES,
    MAX_QUERY_DAYS,
    ErrorCode,
    EventFields,
    SafeError,
)


def aware_datetime(value: str) -> datetime:
    try:
        if (
            not isinstance(value, str)
            or len(value) > 64
            or not re.match(r"^\d{4}-\d{2}-\d{2}T", value)
        ):
            raise ValueError("datetime")
        parsed = datetime.fromisoformat(value)
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() is None
            or parsed.microsecond
            or parsed.utcoffset().microseconds
        ):
            raise ValueError("offset")
        return parsed
    except (ValueError, TypeError):
        raise SafeError(ErrorCode.INVALID_INPUT) from None


def validate_interval(start: str, end: str) -> tuple[datetime, datetime]:
    a = aware_datetime(start).astimezone(timezone.utc)
    b = aware_datetime(end).astimezone(timezone.utc)
    if b <= a:
        raise SafeError(ErrorCode.INVALID_INPUT)
    if b - a > timedelta(days=MAX_QUERY_DAYS):
        raise SafeError(ErrorCode.LIMIT_EXCEEDED)
    return a, b


def parse_calendar(data: bytes) -> Calendar:
    if len(data) > MAX_EVENT_BYTES:
        raise SafeError(ErrorCode.LIMIT_EXCEEDED)
    try:
        text = data.decode("utf-8-sig").strip()
        if not text.upper().startswith("BEGIN:VCALENDAR") or not text.upper().endswith(
            "END:VCALENDAR"
        ):
            raise ValueError("calendar")
        calendar = Calendar.from_ical(data)
        if not isinstance(calendar, Calendar) or calendar.get("VERSION") != "2.0":
            raise ValueError("calendar")
        components = list(calendar.walk())
        if len(components) > 1024:
            raise SafeError(ErrorCode.LIMIT_EXCEEDED)
        if any(c.errors for c in components):
            raise ValueError("parser")
        events = calendar.walk("VEVENT")
        if not events:
            raise ValueError("events")
        if len(events) > MAX_OCCURRENCES:
            raise SafeError(ErrorCode.LIMIT_EXCEEDED)
        for event in events:
            for key in ("UID", "DTSTART", "DTEND", "DURATION", "RECURRENCE-ID"):
                if isinstance(event.get(key), list):
                    raise ValueError("duplicate")
            if not event.get("UID") or not event.get("DTSTART"):
                raise ValueError("required")
        if len({str(e["UID"]) for e in events}) != 1:
            raise ValueError("resource UID")
        return calendar
    except SafeError:
        raise
    except Exception:
        raise SafeError(ErrorCode.BAD_RESPONSE) from None


def _text(value, maximum, *, nonempty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (nonempty and not value.strip())
        or any(ord(c) < 32 and c not in "\n\r\t" for c in value)
    ):
        raise SafeError(ErrorCode.INVALID_INPUT)


def _fields_times(fields, default_timezone):
    _text(fields.title, 200, nonempty=True)
    _text(fields.description, 4096)
    _text(fields.location, 512)
    if type(fields.all_day) is not bool:
        raise SafeError(ErrorCode.INVALID_INPUT)
    try:
        if fields.all_day:
            if (
                fields.timezone is not None
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields.start)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields.end)
            ):
                raise ValueError("date")
            start, end = (
                date.fromisoformat(fields.start),
                date.fromisoformat(fields.end),
            )
            zone = None
        else:
            zone = ZoneInfo(fields.timezone or default_timezone)
            start, end = aware_datetime(fields.start), aware_datetime(fields.end)
            for value in (start, end):
                local = value.astimezone(zone)
                if (
                    local.replace(tzinfo=None) != value.replace(tzinfo=None)
                    or local.utcoffset() != value.utcoffset()
                    or local.replace(fold=0).utcoffset()
                    != local.replace(fold=1).utcoffset()
                ):
                    raise ValueError("local offset")
            start, end = start.astimezone(zone), end.astimezone(zone)
        duration = (
            end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
            if isinstance(start, datetime)
            else end - start
        )
        if duration <= timedelta():
            raise ValueError("interval")
        return start, end, zone
    except SafeError:
        raise
    except Exception:
        raise SafeError(ErrorCode.INVALID_INPUT) from None


def _serialize(calendar):
    data = calendar.to_ical()
    if len(data) > MAX_EVENT_BYTES:
        raise SafeError(ErrorCode.LIMIT_EXCEEDED)
    return data


def _add_timezone(calendar, zone, start, end):
    if zone is None or zone.key == "UTC":
        return
    if any(str(t.get("TZID")) == zone.key for t in calendar.walk("VTIMEZONE")):
        return
    if end.astimezone(timezone.utc) - start.astimezone(timezone.utc) > timedelta(
        days=3660
    ):
        raise SafeError(ErrorCode.LIMIT_EXCEEDED)
    calendar.add_component(
        Timezone.from_tzinfo(zone, first_date=start.date(), last_date=end.date())
    )


def build_event(fields: EventFields, default_timezone: str) -> tuple[str, bytes]:
    start, end, zone = _fields_times(fields, default_timezone)
    uid = str(uuid4())
    calendar = Calendar()
    calendar.add("version", "2.0")
    calendar.add("prodid", "-//Hermes CalDAV MCP//EN")
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("summary", fields.title)
    if fields.description:
        event.add("description", fields.description)
    if fields.location:
        event.add("location", fields.location)
    calendar.add_component(event)
    _add_timezone(calendar, zone, start, end)
    return uid, _serialize(calendar)


def patch_event(
    original: bytes, changes, *, default_timezone: str, whole_series: bool
) -> bytes:
    calendar = parse_calendar(original)
    events = calendar.walk("VEVENT")
    if "METHOD" in calendar or any(
        k in event for event in events for k in ("ORGANIZER", "ATTENDEE")
    ):
        raise SafeError(ErrorCode.UNSUPPORTED_OPERATION)
    masters = [event for event in events if "RECURRENCE-ID" not in event]
    if len(masters) != 1:
        raise SafeError(ErrorCode.UNSUPPORTED_OPERATION)
    master = masters[0]
    recurring = len(events) > 1 or any(
        k in master for k in ("RRULE", "RDATE", "EXDATE")
    )
    if type(whole_series) is not bool or (recurring and not whole_series):
        raise SafeError(ErrorCode.UNSUPPORTED_OPERATION)
    supplied = {k: v for k, v in asdict(changes).items() if v is not None}
    if not supplied:
        raise SafeError(ErrorCode.INVALID_INPUT)
    for key, maximum in (("title", 200), ("description", 4096), ("location", 512)):
        if key in supplied:
            _text(supplied[key], maximum, nonempty=key == "title")
    timing = any(k in supplied for k in ("start", "end", "timezone"))
    if timing:
        if recurring and (len(events) > 1 or "EXDATE" in master or "RDATE" in master):
            raise SafeError(ErrorCode.UNSUPPORTED_OPERATION)
        try:
            start, end = event_times(master, default_timezone)
        except Exception:
            raise SafeError(ErrorCode.BAD_RESPONSE) from None
        if type(start) is date and "timezone" in supplied:
            raise SafeError(ErrorCode.INVALID_INPUT)
        zone = str(
            master["DTSTART"].params.get(
                "TZID",
                "UTC"
                if isinstance(start, datetime) and start.utcoffset() == timedelta()
                else default_timezone,
            )
        )
        fields = EventFields(
            str(master.get("SUMMARY", "Event")),
            supplied.get("start", start.isoformat()),
            supplied.get("end", end.isoformat()),
            all_day=type(start) is date,
            timezone=None if type(start) is date else supplied.get("timezone", zone),
        )
        a, b, tz = _fields_times(fields, default_timezone)
        _add_timezone(calendar, tz, a, b)
        for key in ("DTSTART", "DTEND", "DURATION"):
            master.pop(key, None)
        master.add("DTSTART", a)
        master.add("DTEND", b)
    for key, property_name in (
        ("title", "SUMMARY"),
        ("description", "DESCRIPTION"),
        ("location", "LOCATION"),
    ):
        if key in supplied:
            old = master.get(property_name)
            params = dict(old.params) if old is not None else {}
            master.pop(property_name, None)
            master.add(property_name, supplied[key], parameters=params)
    now = datetime.now(timezone.utc)
    for key in ("DTSTAMP", "LAST-MODIFIED"):
        master.pop(key, None)
        master.add(key, now)
    if "SEQUENCE" in master:
        try:
            sequence = int(master["SEQUENCE"])
            if not 0 <= sequence < 2147483647:
                raise ValueError("sequence")
            master.pop("SEQUENCE")
            master.add("SEQUENCE", sequence + 1)
        except (ValueError, TypeError):
            raise SafeError(ErrorCode.BAD_RESPONSE) from None
    return _serialize(calendar)


def localize(value, default_timezone: str):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            zone = ZoneInfo(default_timezone)
            a, b = (
                value.replace(tzinfo=zone, fold=0),
                value.replace(tzinfo=zone, fold=1),
            )
            if (
                a.utcoffset() != b.utcoffset()
                or a.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
                != value
            ):
                raise ValueError("ambiguous floating time")
            return a
        return value
    if type(value) is date:
        return value
    raise ValueError("date")


def event_times(event, default_timezone: str):
    start = localize(event["DTSTART"].dt, default_timezone)
    if "DTEND" in event and "DURATION" in event:
        raise ValueError("end and duration")
    if "DTEND" in event:
        end = localize(event["DTEND"].dt, default_timezone)
    elif "DURATION" in event:
        duration = event["DURATION"].dt
        if type(start) is date and duration.seconds:
            raise ValueError("date duration")
        end = start + duration
    else:
        end = start + (timedelta(days=1) if type(start) is date else timedelta())
    if type(start) is not type(end) or end < start:
        raise ValueError("interval")
    return start, end


def event_views(resource, default_timezone: str, *, minimal: bool) -> list[dict]:
    calendar = parse_calendar(resource.data)
    try:
        result = []
        for event in calendar.walk("VEVENT"):
            start, end = event_times(event, default_timezone)
            recurring = any(
                k in event for k in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID")
            )
            occurrence = event.get("RECURRENCE-ID")
            tz = (
                None
                if type(start) is date
                else str(
                    event["DTSTART"].params.get(
                        "TZID",
                        "UTC" if start.utcoffset() == timedelta() else default_timezone,
                    )
                )
            )
            view = {
                "event_id": resource.event_id,
                "revision": resource.etag,
                "title": str(event.get("SUMMARY", "")),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "all_day": type(start) is date,
                "timezone": tz,
                "recurring": recurring,
                "occurrence": occurrence.dt.isoformat() if occurrence else None,
            }
            if not minimal:
                view.update(
                    {
                        "description": str(event.get("DESCRIPTION", "")),
                        "location": str(event.get("LOCATION", "")),
                        "uid": str(event["UID"]),
                        "invitation_managed": "METHOD" in calendar
                        or any(k in event for k in ("ORGANIZER", "ATTENDEE")),
                    }
                )
            result.append(view)
        return result
    except Exception:
        raise SafeError(ErrorCode.BAD_RESPONSE) from None
