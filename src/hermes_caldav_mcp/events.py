"""Interpret calendar data without following links or executing remote text."""

import re
import warnings
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from dateutil.rrule import rruleset, rrulestr
from icalendar import Alarm, Calendar, Event, Timezone, Todo, vRecur

from .types import (
    MAX_EVENT_BYTES,
    MAX_OCCURRENCES,
    MAX_QUERY_DAYS,
    ErrorCode,
    EventFields,
    SafeError,
)

UTC = ZoneInfo("UTC")
EXPECTED = "a time like 2026-10-30T14:00 or a date like 2026-10-30"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?"
)
PRIORITIES = {"high": 1, "medium": 5, "low": 9, "none": 0}


def _bad(field, expected, got=None):
    hint = f"{field}: expected {expected}"
    if got is not None:
        text = str(got)
        hint += f"; got {text[:40]!r}" + ("..." if len(text) > 40 else "")
    raise SafeError(ErrorCode.INVALID_INPUT, hint)


def get_zone(name, field="timezone") -> ZoneInfo:
    try:
        if isinstance(name, str) and 0 < len(name) <= 128:
            return ZoneInfo(name)
    except (ValueError, OSError, KeyError):
        pass
    _bad(field, "an IANA timezone like Europe/Berlin", name)


def clean_text(value, field, maximum, *, nonempty=False) -> str:
    if not isinstance(value, str):
        _bad(field, "text")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(value) > maximum:
        _bad(field, f"at most {maximum} characters")
    if nonempty and not value.strip():
        _bad(field, "non-empty text")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        _bad(field, "plain text without control characters")
    return value


def _display(value) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if ord(c) >= 32 or c in "\n\t")


def wall_clock(naive: datetime, zone: ZoneInfo, field="start") -> datetime:
    a, b = naive.replace(tzinfo=zone, fold=0), naive.replace(tzinfo=zone, fold=1)
    if (
        a.utcoffset() != b.utcoffset()
        or a.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive
    ):
        raise SafeError(
            ErrorCode.INVALID_INPUT,
            f"{field}: {naive:%Y-%m-%dT%H:%M} is skipped or repeated in {zone.key} "
            "by a daylight-saving change; choose another time",
        )
    return a


def parse_time(value, zone: ZoneInfo, field: str, *, keep_offset=True):
    """Return a date for YYYY-MM-DD, otherwise an aware datetime in zone.

    Naive times are wall-clock times in zone. An explicit offset is kept as an
    instant unless keep_offset is False (the caller named a zone); then its
    wall-clock reading is used in that zone instead of failing."""
    if not isinstance(value, str) or len(value) > 64:
        _bad(field, EXPECTED, value)
    text = value.strip()
    try:
        if _DATE.fullmatch(text):
            return date.fromisoformat(text)
        if not _DATETIME.fullmatch(text):
            raise ValueError("format")
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        _bad(field, EXPECTED, value)
    if parsed.tzinfo is not None and keep_offset:
        return parsed.astimezone(zone)
    return wall_clock(parsed.replace(tzinfo=None), zone, field)


def _instant(value, zone):
    if type(value) is date:
        return datetime.combine(value, time(), zone)
    return value


def check_range(a: datetime, b: datetime):
    if b <= a:
        _bad("to", "a time after from")
    if b - a > timedelta(days=MAX_QUERY_DAYS):
        raise SafeError(
            ErrorCode.LIMIT_EXCEEDED,
            f"from/to: at most {MAX_QUERY_DAYS} days; narrow the range.",
        )
    return a.astimezone(UTC), b.astimezone(UTC)


def parse_range(start, end, zone: ZoneInfo, *, default_days=7, now=None):
    """Lenient query window; a date `to` includes that whole day."""
    a = parse_time(start, zone, "from") if start is not None else now
    if a is None:
        a = datetime.now(zone)
    if type(a) is date:
        a = _instant(a, zone)
    if end is None:
        b = a + timedelta(days=default_days)
    else:
        b = parse_time(end, zone, "to")
        if type(b) is date:
            b = _instant(b + timedelta(days=1), zone)
    return check_range(a, b)


def parse_calendar(data: bytes, component="VEVENT") -> Calendar:
    if len(data) > MAX_EVENT_BYTES:
        raise SafeError(ErrorCode.LIMIT_EXCEEDED)
    try:
        text = data.decode("utf-8-sig").strip()
        if not text.upper().startswith("BEGIN:VCALENDAR") or not text.upper().endswith(
            "END:VCALENDAR"
        ):
            raise ValueError("calendar")
        with warnings.catch_warnings():
            # icalendar warns when it maps vendor TZIDs; never write to stdio.
            warnings.simplefilter("ignore")
            calendar = Calendar.from_ical(data)
        if not isinstance(calendar, Calendar) or calendar.get("VERSION") != "2.0":
            raise ValueError("calendar")
        components = list(calendar.walk())
        if len(components) > 1024:
            raise SafeError(ErrorCode.LIMIT_EXCEEDED)
        if any(c.errors for c in components):
            raise ValueError("parser")
        items = calendar.walk(component)
        if not items:
            raise ValueError("items")
        if len(items) > MAX_OCCURRENCES:
            raise SafeError(ErrorCode.LIMIT_EXCEEDED)
        for item in items:
            for key in ("UID", "DTSTART", "DTEND", "DUE", "DURATION", "RECURRENCE-ID"):
                if isinstance(item.get(key), list):
                    raise ValueError("duplicate")
            if not item.get("UID") or (
                component == "VEVENT" and not item.get("DTSTART")
            ):
                raise ValueError("required")
        if len({str(e["UID"]) for e in items}) != 1:
            raise ValueError("resource UID")
        return calendar
    except SafeError:
        raise
    except Exception:
        raise SafeError(ErrorCode.BAD_RESPONSE) from None


def _serialize(calendar):
    data = calendar.to_ical()
    if len(data) > MAX_EVENT_BYTES:
        raise SafeError(ErrorCode.LIMIT_EXCEEDED)
    return data


def _add_timezone(calendar, zone, start, end):
    if not isinstance(zone, ZoneInfo) or zone.key == "UTC":
        return
    if any(str(t.get("TZID")) == zone.key for t in calendar.walk("VTIMEZONE")):
        return
    if end - start > timedelta(days=3660):
        raise SafeError(ErrorCode.LIMIT_EXCEEDED)
    # A year either side keeps the observance in effect at the event itself.
    calendar.add_component(
        Timezone.from_tzinfo(
            zone,
            first_date=start.date() - timedelta(days=366),
            last_date=end.date() + timedelta(days=366),
        )
    )


def _new_calendar():
    calendar = Calendar()
    calendar.add("version", "2.0")
    calendar.add("prodid", "-//Hermes CalDAV MCP//EN")
    return calendar


def _value(value, default: ZoneInfo):
    """Stored date or aware datetime; floating times use the default zone."""
    if isinstance(value, datetime):
        if value.tzinfo:
            return value
        try:
            return wall_clock(value, default)
        except SafeError:
            raise ValueError("ambiguous floating time") from None
    if type(value) is date:
        return value
    raise ValueError("date")


def event_zone(component, default: ZoneInfo) -> ZoneInfo:
    """The item's own IANA zone (icalendar maps vendor TZIDs), else default."""
    prop = component.get("DTSTART") or component.get("DUE")
    key = getattr(getattr(getattr(prop, "dt", None), "tzinfo", None), "key", None)
    if prop is not None and "TZID" in prop.params and key:
        try:
            return ZoneInfo(key)
        except (ValueError, OSError, KeyError):
            pass
    return default


def component_times(event, default: ZoneInfo):
    start = _value(event["DTSTART"].dt, default)
    if "DTEND" in event and "DURATION" in event:
        raise ValueError("end and duration")
    if "DTEND" in event:
        end = _value(event["DTEND"].dt, default)
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


def _key(value, default):
    value = _value(value, default)
    return value if type(value) is date else value.astimezone(UTC)


def _like(value, base, default):
    """Convert an EXDATE/RDATE/UNTIL value to the type and zone of DTSTART."""
    if type(value) is date:
        value = datetime.combine(value, base.time() if base.tzinfo else time())
    if base.tzinfo is None:
        return value.astimezone(default).replace(tzinfo=None) if value.tzinfo else value
    return value if value.tzinfo else value.replace(tzinfo=base.tzinfo)


def _dates(prop):
    for item in prop if isinstance(prop, list) else [prop]:
        for value in item.dts:
            yield value.dt[0] if isinstance(value.dt, tuple) else value.dt


def _expand(master, start, duration, a, b, default):
    """Recurrence starts overlapping [a, b); at most MAX_OCCURRENCES + 1."""
    is_date = type(start) is date
    base = datetime.combine(start, time()) if is_date else start
    lo, hi = a - duration, b
    if base.tzinfo is None:
        lo, hi = (v.astimezone(default).replace(tzinfo=None) for v in (lo, hi))
    rules = master.get("RRULE", [])
    rs = rruleset()
    for rule in rules if isinstance(rules, list) else [rules]:
        rule = vRecur(dict(rule))
        freq = str((rule.get("FREQ") or [""])[0]).upper()
        if "COUNT" in rule:
            # A counted sub-daily rule cannot be bounded by the query window.
            if freq in {"SECONDLY", "MINUTELY", "HOURLY"}:
                raise ValueError("unbounded rule")
        else:
            # Never iterate past the window, whatever UNTIL the server sent.
            until = min([hi, *(_like(u, base, default) for u in rule.get("UNTIL", []))])
            rule["UNTIL"] = [until.astimezone(UTC) if base.tzinfo else until]
        rs.rrule(rrulestr(rule.to_ical().decode(), dtstart=base))
    if "RDATE" in master:
        for value in _dates(master["RDATE"]):
            rs.rdate(_like(value, base, default))
    if "EXDATE" in master:
        for value in _dates(master["EXDATE"]):
            rs.exdate(_like(value, base, default))
    result = []
    for value in rs.xafter(lo, inc=True):
        if value >= hi or len(result) > MAX_OCCURRENCES:
            break
        result.append(value.date() if is_date else value)
    return result


def occurrences(calendar, a, b, default: ZoneInfo):
    """(component, start, end, recurrence_id) for each instance overlapping [a, b)."""
    events = calendar.walk("VEVENT")
    masters = [e for e in events if "RECURRENCE-ID" not in e]
    if len(masters) > 1:
        raise ValueError("masters")
    overrides = {
        _key(e["RECURRENCE-ID"].dt, default): e for e in events if "RECURRENCE-ID" in e
    }
    result = []

    def add(component, start, end, rid):
        s, e = _instant(start, default), _instant(end, default)
        if s < b and (e > a or (e == s and s >= a)):
            result.append((component, start, end, rid))

    if masters:
        master = masters[0]
        start, end = component_times(master, default)
        if any(k in master for k in ("RRULE", "RDATE")):
            for value in _expand(master, start, end - start, a, b, default):
                if _key(value, default) not in overrides:
                    add(master, value, value + (end - start), value)
        else:
            add(master, start, end, None)
    for event in overrides.values():
        start, end = component_times(event, default)
        add(event, start, end, _value(event["RECURRENCE-ID"].dt, default))
    return result


def is_recurring(calendar) -> bool:
    events = calendar.walk("VEVENT")
    return len(events) > 1 or any(
        k in e for e in events for k in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID")
    )


def iso(value, zone) -> str:
    if type(value) is date:
        return value.isoformat()
    return value.astimezone(zone).isoformat()


def reminders(component) -> list[int]:
    result = set()
    for alarm in component.walk("VALARM"):
        trigger = alarm.get("TRIGGER")
        if (
            trigger is not None
            and isinstance(trigger.dt, timedelta)
            and trigger.dt <= timedelta()
            and trigger.params.get("RELATED", "START") == "START"
        ):
            result.add(int(-trigger.dt.total_seconds() // 60))
    return sorted(result)


def event_view(
    resource, calendar_name, component, start, end, rid, default, *, details=False
):
    zone = event_zone(component, default)
    all_day = type(start) is date
    view = {
        "event_id": resource.event_id,
        "calendar": calendar_name,
        "title": _display(component.get("SUMMARY")),
        "start": iso(start, zone),
        "end": iso(end, zone),
        "all_day": all_day,
        "timezone": None if all_day else zone.key,
    }
    if component.get("LOCATION"):
        view["location"] = _display(component["LOCATION"])
    if rid is not None:
        view["recurring"] = True
        view["occurrence"] = iso(rid, zone)
    if str(component.get("STATUS", "")).upper() == "CANCELLED":
        view["status"] = "cancelled"
    if details:
        view["description"] = _display(component.get("DESCRIPTION"))
        view["reminder_minutes"] = reminders(component)
        if "RRULE" in component and not isinstance(component["RRULE"], list):
            view["repeat"] = component["RRULE"].to_ical().decode()
    return view


def _invitation(calendar):
    return "METHOD" in calendar or any(
        k in e for e in calendar.walk("VEVENT") for k in ("ORGANIZER", "ATTENDEE")
    )


def event_details(resource, calendar_name, default: ZoneInfo) -> dict:
    """The stored event or series plus its overrides, as written (not expanded)."""
    calendar = parse_calendar(resource.data)
    try:
        events = []
        for event in calendar.walk("VEVENT"):
            start, end = component_times(event, default)
            rid = event.get("RECURRENCE-ID")
            view = event_view(
                resource,
                calendar_name,
                event,
                start,
                end,
                _value(rid.dt, default) if rid else None,
                default,
                details=True,
            )
            if rid is None and is_recurring(calendar):
                view["recurring"] = True
            events.append(view)
        return {
            "event_id": resource.event_id,
            "calendar": calendar_name,
            "revision": resource.etag,
            "invitation_managed": _invitation(calendar),
            "events": events,
        }
    except Exception:
        raise SafeError(ErrorCode.BAD_RESPONSE) from None


def _timing(zone, keep, start=None, end=None, duration=None, old=None):
    """Resolve start/end; `old` is the current (start, end) when editing."""
    if end is not None and duration is not None:
        _bad("duration_minutes", "either end or duration_minutes, not both")
    if start is not None:
        s = parse_time(start, zone, "start", keep_offset=keep)
    else:
        s = old[0]
    all_day = type(s) is date
    if end is not None:
        e = parse_time(end, zone, "end", keep_offset=keep)
        if type(e) is not type(s):
            _bad(
                "end",
                "a date like 2026-10-31 for an all-day event"
                if all_day
                else "a time like 2026-10-30T15:00 for a timed event",
                end,
            )
        if all_day and e == s:
            e = s + timedelta(days=1)
    elif duration is not None:
        if all_day:
            _bad("duration_minutes", "end (a date) for an all-day event")
        e = (s.astimezone(UTC) + timedelta(minutes=duration)).astimezone(zone)
    elif old is not None and type(old[0]) is type(s):
        # Moving the start keeps the event's length.
        length = old[1] - old[0]
        e = s + length if all_day else (s.astimezone(UTC) + length).astimezone(zone)
    else:
        e = s + (timedelta(days=1) if all_day else timedelta(hours=1))
    if e <= s:
        _bad("end", "a time after start", end)
    return s, e


def _rrule(repeat, start, zone):
    rule = {"FREQ": repeat["freq"].upper()}
    if repeat.get("interval", 1) != 1:
        rule["INTERVAL"] = repeat["interval"]
    if repeat.get("weekdays"):
        rule["BYDAY"] = list(dict.fromkeys(repeat["weekdays"]))
    if repeat.get("count") is not None and repeat.get("until") is not None:
        _bad("repeat", "either count or until, not both")
    if repeat.get("count") is not None:
        rule["COUNT"] = repeat["count"]
    if repeat.get("until") is not None:
        until = parse_time(repeat["until"], zone, "repeat.until")
        if type(start) is date:
            until = until if type(until) is date else until.date()
        else:
            if type(until) is date:
                until = wall_clock(
                    datetime.combine(until, time(23, 59)), zone, "repeat.until"
                )
            until = until.astimezone(UTC)
        if _instant(until, zone) < _instant(start, zone):
            _bad("repeat.until", "a date on or after start", repeat["until"])
        rule["UNTIL"] = until
    return vRecur(rule)


def _set_alarms(component, minutes):
    component.subcomponents = [c for c in component.subcomponents if c.name != "VALARM"]
    for value in sorted(set(minutes)):
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("trigger", timedelta(minutes=-value))
        alarm.add("description", _display(component.get("SUMMARY")) or "Reminder")
        component.add_component(alarm)


def _set_text(component, name, value):
    old = component.get(name)
    params = dict(old.params) if old is not None and not isinstance(old, list) else {}
    component.pop(name, None)
    if value:
        component.add(name, value, parameters=params)


def _set_times(calendar, component, start, end, until=None):
    for key in ("DTSTART", "DTEND", "DURATION"):
        component.pop(key, None)
    component.add("DTSTART", start)
    component.add("DTEND", end)
    if isinstance(start, datetime):
        _add_timezone(calendar, start.tzinfo, start, max(end, until or end))


def _series_span(start):
    # Timezone definitions for a repeating event cover the next ten years.
    if isinstance(start, datetime):
        return start + timedelta(days=3650)
    return None


def _touch(component):
    now = datetime.now(timezone.utc)
    for key in ("DTSTAMP", "LAST-MODIFIED"):
        component.pop(key, None)
        component.add(key, now)
    if "SEQUENCE" in component:
        try:
            sequence = int(component["SEQUENCE"])
            if not 0 <= sequence < 2147483647:
                raise ValueError("sequence")
            component.pop("SEQUENCE")
            component.add("SEQUENCE", sequence + 1)
        except (ValueError, TypeError):
            raise SafeError(ErrorCode.BAD_RESPONSE) from None


def build_event(fields: EventFields, default_timezone: str) -> tuple[str, bytes]:
    zone = get_zone(fields.timezone) if fields.timezone else ZoneInfo(default_timezone)
    title = clean_text(fields.title, "title", 200, nonempty=True)
    description = clean_text(fields.description, "description", 4096)
    location = clean_text(fields.location, "location", 512)
    start, end = _timing(
        zone, fields.timezone is None, fields.start, fields.end, fields.duration_minutes
    )
    uid = str(uuid4())
    calendar = _new_calendar()
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", datetime.now(timezone.utc))
    event.add("summary", title)
    if description:
        event.add("description", description)
    if location:
        event.add("location", location)
    if fields.repeat:
        event.add("rrule", _rrule(fields.repeat, start, zone))
    _set_times(
        calendar, event, start, end, _series_span(start) if fields.repeat else None
    )
    if fields.reminder_minutes:
        _set_alarms(event, fields.reminder_minutes)
    calendar.add_component(event)
    return uid, _serialize(calendar)


def _editable(calendar):
    if _invitation(calendar):
        raise SafeError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "Invitations with attendees must be changed in Nextcloud Calendar.",
        )
    events = calendar.walk("VEVENT")
    masters = [e for e in events if "RECURRENCE-ID" not in e]
    if len(masters) > 1:
        raise SafeError(ErrorCode.UNSUPPORTED_OPERATION)
    return events, (masters[0] if masters else None)


def find_occurrence(calendar, occurrence, default: ZoneInfo):
    """The (component, start, end, recurrence_id) addressed by an occurrence value."""
    zone = event_zone(calendar.walk("VEVENT")[0], default)
    target = parse_time(occurrence, zone, "occurrence")
    try:
        key = _key(target, default)
        around = _instant(target, zone)
        found = [
            o
            for o in occurrences(
                calendar,
                around - timedelta(days=2),
                around + timedelta(days=2),
                default,
            )
            if o[3] is not None and _key(o[3], default) == key
        ]
    except Exception:
        raise SafeError(ErrorCode.BAD_RESPONSE) from None
    if not found:
        raise SafeError(
            ErrorCode.NOT_FOUND,
            f"occurrence: no occurrence at {occurrence[:40]!r}; copy the occurrence "
            "value from list_events.",
        )
    return found[0]


def patch_event(
    original: bytes,
    changes,
    *,
    default_timezone: str,
    scope: str | None = None,
    occurrence: str | None = None,
) -> bytes:
    default = ZoneInfo(default_timezone)
    calendar = parse_calendar(original)
    events, master = _editable(calendar)
    recurring = is_recurring(calendar)
    supplied = {k: v for k, v in asdict(changes).items() if v is not None}
    if not supplied:
        _bad("update", "at least one field to change, e.g. title or start")
    if occurrence is not None and not recurring:
        _bad("occurrence", "nothing: this event does not repeat; omit occurrence")
    if recurring and scope is None:
        if occurrence is None:
            raise SafeError(
                ErrorCode.INVALID_INPUT,
                "scope: this event repeats; use scope='series' to change every "
                "occurrence, or occurrence=<value from list_events> with scope='this'.",
            )
        scope = "this"
    if recurring and scope == "this":
        if occurrence is None:
            _bad("occurrence", "the occurrence value from list_events for scope='this'")
        if "repeat" in supplied:
            _bad("repeat", "scope='series' to change how the event repeats")
        component, old_start, old_end, rid = find_occurrence(
            calendar, occurrence, default
        )
        if "RECURRENCE-ID" not in component:
            # Override one occurrence with a copy of the series' properties.
            component = Event.from_ical(component.to_ical())
            for key in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"):
                component.pop(key, None)
            component.add("RECURRENCE-ID", rid)
            _set_times(calendar, component, old_start, old_end)
            calendar.add_component(component)
    else:
        if master is None:
            raise SafeError(ErrorCode.UNSUPPORTED_OPERATION)
        component = master
        try:
            old_start, old_end = component_times(master, default)
        except Exception:
            raise SafeError(ErrorCode.BAD_RESPONSE) from None
        timing = {"start", "end", "duration_minutes", "timezone", "repeat"}
        if (
            recurring
            and timing & supplied.keys()
            and (len(events) > 1 or "EXDATE" in master or "RDATE" in master)
        ):
            raise SafeError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "This series has changed or skipped occurrences, so its timing "
                "cannot be changed safely; edit single occurrences with scope='this'.",
            )

    for key, name, maximum in (
        ("title", "SUMMARY", 200),
        ("description", "DESCRIPTION", 4096),
        ("location", "LOCATION", 512),
    ):
        if key in supplied:
            value = clean_text(supplied[key], key, maximum, nonempty=key == "title")
            _set_text(component, name, value)

    if {"start", "end", "duration_minutes", "timezone"} & supplied.keys():
        named = supplied.get("timezone")
        if named and type(old_start) is date:
            _bad("timezone", "nothing for an all-day event; omit timezone")
        old_zone = event_zone(component, default)
        zone = get_zone(named) if named else old_zone
        old = (old_start, old_end)
        if named:
            # Keep the wall-clock times, reinterpreted in the named zone.
            old = tuple(
                wall_clock(
                    v.astimezone(old_zone).replace(tzinfo=None), zone, "timezone"
                )
                for v in old
            )
        start, end = _timing(
            zone,
            named is None,
            supplied.get("start"),
            supplied.get("end"),
            supplied.get("duration_minutes"),
            old,
        )
        stored = component["DTSTART"]
        if (
            not named
            and isinstance(start, datetime)
            and isinstance(stored.dt, datetime)
            and stored.dt.tzinfo is not None
            and "TZID" not in stored.params
        ):
            # Events stored as UTC stay UTC unless a zone is named.
            start, end = start.astimezone(UTC), end.astimezone(UTC)
        _set_times(calendar, component, start, end)

    if "repeat" in supplied:
        component.pop("RRULE", None)
        if supplied["repeat"]["freq"] != "none":
            start = _value(component["DTSTART"].dt, default)
            component.add(
                "RRULE",
                _rrule(supplied["repeat"], start, event_zone(component, default)),
            )
            if isinstance(start, datetime):
                _add_timezone(calendar, start.tzinfo, start, _series_span(start))
    if "reminder_minutes" in supplied:
        _set_alarms(component, supplied["reminder_minutes"])
    _touch(component)
    return _serialize(calendar)


def exclude_occurrence(original: bytes, occurrence: str, default_timezone: str):
    """Skip one occurrence via EXDATE; returns (data, component, start, end)."""
    default = ZoneInfo(default_timezone)
    calendar = parse_calendar(original)
    _, master = _editable(calendar)
    if master is None or not any(k in master for k in ("RRULE", "RDATE")):
        _bad("occurrence", "nothing: this event does not repeat; omit occurrence")
    component, start, end, rid = find_occurrence(calendar, occurrence, default)
    if component is not master:
        calendar.subcomponents.remove(component)
    master.add("EXDATE", rid)
    _touch(master)
    return _serialize(calendar), component, start, end


def _apply_task(calendar, todo, changes, default):
    zone = get_zone(changes.timezone) if changes.timezone else event_zone(todo, default)
    if changes.title is not None:
        title = clean_text(changes.title, "title", 200, nonempty=True)
        _set_text(todo, "SUMMARY", title)
    if changes.notes is not None:
        _set_text(todo, "DESCRIPTION", clean_text(changes.notes, "notes", 4096))
    if changes.priority is not None:
        todo.pop("PRIORITY", None)
        priority = PRIORITIES[changes.priority]
        if priority:
            todo.add("PRIORITY", priority)
    if changes.due is not None:
        todo.pop("DUE", None)
        if changes.due:
            due = parse_time(
                changes.due, zone, "due", keep_offset=changes.timezone is None
            )
            todo.add("DUE", due)
            if isinstance(due, datetime):
                _add_timezone(calendar, due.tzinfo, due, due)
    if changes.completed is not None:
        for key in ("STATUS", "COMPLETED", "PERCENT-COMPLETE"):
            todo.pop(key, None)
        if changes.completed:
            todo.add("STATUS", "COMPLETED")
            todo.add("COMPLETED", datetime.now(timezone.utc))
            todo.add("PERCENT-COMPLETE", 100)
        else:
            todo.add("STATUS", "NEEDS-ACTION")


def build_task(changes, default_timezone: str) -> tuple[str, bytes]:
    uid = str(uuid4())
    calendar = _new_calendar()
    todo = Todo()
    todo.add("uid", uid)
    todo.add("dtstamp", datetime.now(timezone.utc))
    todo.add("status", "NEEDS-ACTION")
    _apply_task(calendar, todo, changes, ZoneInfo(default_timezone))
    calendar.add_component(todo)
    return uid, _serialize(calendar)


def patch_task(original: bytes, changes, *, default_timezone: str) -> bytes:
    calendar = parse_calendar(original, "VTODO")
    todos = calendar.walk("VTODO")
    if len(todos) != 1 or any(k in todos[0] for k in ("RRULE", "RDATE")):
        raise SafeError(
            ErrorCode.UNSUPPORTED_OPERATION,
            "Repeating tasks must be changed in Nextcloud Tasks.",
        )
    if all(v is None for k, v in asdict(changes).items() if k != "timezone"):
        _bad("update", "at least one field to change, e.g. title or due")
    _apply_task(calendar, todos[0], changes, ZoneInfo(default_timezone))
    _touch(todos[0])
    return _serialize(calendar)


def task_view(resource, list_name, default: ZoneInfo) -> dict:
    calendar = parse_calendar(resource.data, "VTODO")
    try:
        todo = calendar.walk("VTODO")[0]
        view = {
            "task_id": resource.event_id,
            "list": list_name,
            "title": _display(todo.get("SUMMARY")),
            "completed": str(todo.get("STATUS", "")).upper() == "COMPLETED"
            or "COMPLETED" in todo,
        }
        if "DUE" in todo:
            view["due"] = iso(
                _value(todo["DUE"].dt, default), event_zone(todo, default)
            )
        priority = int(todo.get("PRIORITY", 0) or 0)
        if priority:
            # RFC 5545: 1-4 high, 5 medium, 6-9 low.
            view["priority"] = (
                "high" if priority < 5 else "medium" if priority == 5 else "low"
            )
        if todo.get("DESCRIPTION"):
            view["notes"] = _display(todo["DESCRIPTION"])
        if any(k in todo for k in ("RRULE", "RDATE")):
            view["recurring"] = True
        return view
    except Exception:
        raise SafeError(ErrorCode.BAD_RESPONSE) from None
