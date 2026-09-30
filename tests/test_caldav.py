from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest

from hermes_caldav_mcp.caldav import (
    create_resource,
    discover_calendars,
    get_resource,
    query_events,
    update_resource,
)
from hermes_caldav_mcp.events import validate_interval
from hermes_caldav_mcp.transport import CalDAVClient, event_id
from hermes_caldav_mcp.types import CalendarResource, SafeError


async def test_writes_are_exclusive_and_conditional(profile):
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(
            201 if len(seen) == 1 else 204, headers={"ETag": '"fresh"'}
        )

    cal = profile.calendars[0]
    resource = CalendarResource(
        event_id(cal, cal.href + "existing.ics"),
        cal.href + "existing.ics",
        '"old"',
        SIMPLE.encode(),
    )
    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        created = await create_resource(client, cal, "generated-uid", SIMPLE.encode())
        updated = await update_resource(client, cal, resource, '"old"', SIMPLE.encode())
    assert created["revision"] == updated["revision"] == '"fresh"'
    assert seen[0].headers["If-None-Match"] == "*"
    assert seen[1].headers["If-Match"] == '"old"'
    assert "If-Match" not in seen[0].headers


@pytest.mark.parametrize(
    "etag,expected", [(None, '"old"'), ('"old"', '"stale"'), ('W/"old"', 'W/"old"')]
)
async def test_unusable_or_stale_revision_never_writes(profile, etag, expected):
    cal = profile.calendars[0]
    resource = CalendarResource(
        event_id(cal, cal.href + "a.ics"), cal.href + "a.ics", etag, SIMPLE.encode()
    )
    seen = []
    async with CalDAVClient(
        profile, transport=httpx.MockTransport(lambda r: seen.append(r))
    ) as client:
        with pytest.raises(SafeError):
            await update_resource(client, cal, resource, expected, SIMPLE.encode())
    assert not seen


async def test_concurrent_write_conflict_is_not_retried(profile):
    cal = profile.calendars[0]
    resource = CalendarResource(
        event_id(cal, cal.href + "a.ics"), cal.href + "a.ics", '"old"', SIMPLE.encode()
    )
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(412)

    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SafeError) as caught:
            await update_resource(client, cal, resource, '"old"', SIMPLE.encode())
    assert caught.value.code.value == "CONFLICT"
    assert len(seen) == 1


async def test_created_resource_can_have_no_new_revision(profile):
    async with CalDAVClient(
        profile, transport=httpx.MockTransport(lambda r: httpx.Response(201))
    ) as client:
        result = await create_resource(
            client, profile.calendars[0], "new-uid", SIMPLE.encode()
        )
    assert result["revision"] is None


D = "DAV:"
C = "urn:ietf:params:xml:ns:caldav"
SIMPLE = (Path(__file__).parent / "fixtures/simple.ics").read_text()


def multistatus(
    href,
    *,
    data=None,
    status="HTTP/1.1 200 OK",
    calendar=False,
    writable=True,
    expand=None,
):
    root = ET.Element(f"{{{D}}}multistatus")
    response = ET.SubElement(root, f"{{{D}}}response")
    ET.SubElement(response, f"{{{D}}}href").text = href
    ps = ET.SubElement(response, f"{{{D}}}propstat")
    prop = ET.SubElement(ps, f"{{{D}}}prop")
    ET.SubElement(ps, f"{{{D}}}status").text = status
    if calendar:
        rt = ET.SubElement(prop, f"{{{D}}}resourcetype")
        ET.SubElement(rt, f"{{{C}}}calendar")
        ET.SubElement(prop, f"{{{D}}}displayname").text = "Synthetic calendar"
        comps = ET.SubElement(prop, f"{{{C}}}supported-calendar-component-set")
        ET.SubElement(comps, f"{{{C}}}comp", name="VEVENT")
        privs = ET.SubElement(prop, f"{{{D}}}current-user-privilege-set")
        if writable:
            p = ET.SubElement(privs, f"{{{D}}}privilege")
            ET.SubElement(p, f"{{{D}}}write-content")
    if data is not None:
        ET.SubElement(prop, f"{{{D}}}getetag").text = '"rev1"'
        ET.SubElement(prop, f"{{{C}}}calendar-data").text = data
    return ET.tostring(root)


async def test_discovery_is_scoped_and_conservative(profile):
    p = replace(profile, calendars=())
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(
            207,
            content=multistatus(
                "https://cloud.example.test" + profile.calendars[0].href,
                calendar=True,
                writable=False,
            ),
        )

    async with CalDAVClient(
        p, transport=httpx.MockTransport(handler), discovery=True
    ) as client:
        calendars = await discover_calendars(client)
    assert len(calendars) == 1
    assert calendars[0].name == "Synthetic calendar"
    assert calendars[0].writable is False
    assert calendars[0].href == profile.calendars[0].href
    assert seen[0].headers["Depth"] == "1"
    assert str(seen[0].url).endswith(profile.calendar_home)


async def test_query_emits_bounded_utc_filter_and_retains_revision(profile):
    seen = []
    cal = profile.calendars[0]

    def handler(r):
        seen.append(r)
        return httpx.Response(207, content=multistatus(cal.href + "a.ics", data=SIMPLE))

    start, end = validate_interval("2025-03-01T00:00:00Z", "2025-04-01T00:00:00Z")
    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        resources = await query_events(client, cal, start, end)
    assert resources[0].etag == '"rev1"'
    assert resources[0].event_id == event_id(cal, cal.href + "a.ics")
    xml = ET.fromstring(seen[0].content)
    assert xml.find(f".//{{{C}}}expand").attrib == {
        "start": "20250301T000000Z",
        "end": "20250401T000000Z",
    }
    assert xml.find(f".//{{{C}}}time-range").attrib == {
        "start": "20250301T000000Z",
        "end": "20250401T000000Z",
    }


@pytest.mark.parametrize(
    "payload,code",
    [
        (
            b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]><x>&y;</x>',
            "BAD_RESPONSE",
        ),
        (
            multistatus("https://foreign.example.test/a.ics", data=SIMPLE),
            "BAD_RESPONSE",
        ),
        (multistatus("/wrong/a.ics", data=SIMPLE), "BAD_RESPONSE"),
        (
            multistatus(
                "/nextcloud/remote.php/dav/calendars/calendar-user/personal/a.ics",
                data=SIMPLE,
                status="HTTP/1.1 403 Forbidden",
            ),
            "PERMISSION_DENIED",
        ),
        (
            multistatus(
                "/nextcloud/remote.php/dav/calendars/calendar-user/personal/a.ics",
                data=SIMPLE.replace("SUMMARY:", "RRULE:FREQ=DAILY\nSUMMARY:"),
            ),
            "UNSUPPORTED_OPERATION",
        ),
    ],
)
async def test_hostile_or_failed_dav_response_fails(profile, payload, code):
    start, end = validate_interval("2025-03-01T00:00:00Z", "2025-04-01T00:00:00Z")
    async with CalDAVClient(
        profile,
        transport=httpx.MockTransport(lambda r: httpx.Response(207, content=payload)),
    ) as client:
        with pytest.raises(SafeError) as caught:
            await query_events(client, profile.calendars[0], start, end)
    assert caught.value.code.value == code


@pytest.mark.parametrize("count,passes", [(250, True), (251, False)])
async def test_occurrence_limit_counts_instances_within_resource(
    profile, count, passes
):
    event = SIMPLE.split("BEGIN:VEVENT\n", 1)[1].split("END:VEVENT", 1)[0]
    data = (
        "BEGIN:VCALENDAR\nVERSION:2.0\n"
        + "".join(
            "BEGIN:VEVENT\n"
            + event.replace(
                "UID:synthetic-event",
                "UID:synthetic-event\nRECURRENCE-ID:20250329T100000Z",
            )
            + "END:VEVENT\n"
            for _ in range(count)
        )
        + "END:VCALENDAR\n"
    )
    # Each synthetic instance must have a distinct recurrence ID.
    for i in range(count):
        data = data.replace(
            "RECURRENCE-ID:20250329T100000Z",
            f"RECURRENCE-ID:20250329T10{i // 60:02}{i % 60:02}Z",
            1,
        )
    cal = profile.calendars[0]
    start, end = validate_interval("2025-03-01T00:00:00Z", "2025-04-01T00:00:00Z")
    async with CalDAVClient(
        profile,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                207, content=multistatus(cal.href + "a.ics", data=data)
            )
        ),
    ) as client:
        if passes:
            assert len(await query_events(client, cal, start, end)) == 1
        else:
            with pytest.raises(SafeError) as caught:
                await query_events(client, cal, start, end)
            assert caught.value.code.value == "LIMIT_EXCEEDED"


async def test_get_fetches_only_one_validated_resource(profile):
    cal = profile.calendars[0]
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(200, content=SIMPLE, headers={"ETag": '"rev2"'})

    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        result = await get_resource(client, cal, event_id(cal, cal.href + "a.ics"))
    assert result.etag == '"rev2"'
    assert len(seen) == 1
