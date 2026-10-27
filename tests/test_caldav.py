from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx
import pytest

from hermes_caldav_mcp.caldav import (
    create_resource,
    delete_resource,
    discover_calendars,
    get_resource,
    home_status,
    live_calendars,
    query_resources,
    update_resource,
)
from hermes_caldav_mcp.events import parse_range
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
NC = "http://nextcloud.com/ns"
SIMPLE = (Path(__file__).parent / "fixtures/simple.ics").read_text()


def multistatus(
    href,
    *,
    data=None,
    status="HTTP/1.1 200 OK",
    calendar=False,
    writable=True,
    components=("VEVENT",),
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
        for name in components:
            ET.SubElement(comps, f"{{{C}}}comp", name=name)
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


RANGE = parse_range("2025-03-01T00:00:00Z", "2025-04-01T00:00:00Z", ZoneInfo("UTC"))
TASK = SIMPLE.replace("BEGIN:VEVENT", "BEGIN:VTODO").replace("END:VEVENT", "END:VTODO")


async def test_query_emits_bounded_utc_filter_without_server_expansion(profile):
    seen = []
    cal = profile.calendars[0]

    def handler(r):
        seen.append(r)
        return httpx.Response(207, content=multistatus(cal.href + "a.ics", data=SIMPLE))

    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        resources = await query_resources(client, cal, "VEVENT", *RANGE)
    assert resources[0].etag == '"rev1"'
    assert resources[0].event_id == event_id(cal, cal.href + "a.ics")
    assert resources[0].data == SIMPLE.encode()
    xml = ET.fromstring(seen[0].content)
    # Recurrences are expanded locally so the original TZIDs survive.
    assert xml.find(f".//{{{C}}}expand") is None
    assert xml.find(f".//{{{C}}}time-range").attrib == {
        "start": "20250301T000000Z",
        "end": "20250401T000000Z",
    }
    filters = [e.get("name") for e in xml.iter(f"{{{C}}}comp-filter")]
    assert filters == ["VCALENDAR", "VEVENT"]


async def test_task_query_is_unbounded_and_filters_vtodo(profile):
    seen = []
    cal = profile.calendars[0]

    def handler(r):
        seen.append(r)
        return httpx.Response(207, content=multistatus(cal.href + "t.ics", data=TASK))

    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        resources = await query_resources(client, cal, "VTODO")
    assert resources[0].data == TASK.encode()
    xml = ET.fromstring(seen[0].content)
    assert xml.find(f".//{{{C}}}time-range") is None
    assert [e.get("name") for e in xml.iter(f"{{{C}}}comp-filter")][1] == "VTODO"


@pytest.mark.parametrize(
    "data,status",
    [
        ("garbage", "HTTP/1.1 200 OK"),
        (SIMPLE.replace("UID:synthetic-event", "UID:a\nUID:b"), "HTTP/1.1 200 OK"),
        (SIMPLE, "HTTP/1.1 403 Forbidden"),
    ],
)
async def test_unreadable_resource_is_returned_without_data(profile, data, status):
    cal = profile.calendars[0]
    payload = multistatus(cal.href + "a.ics", data=data, status=status)
    async with CalDAVClient(
        profile,
        transport=httpx.MockTransport(lambda r: httpx.Response(207, content=payload)),
    ) as client:
        resources = await query_resources(client, cal, "VEVENT", *RANGE)
    assert len(resources) == 1 and resources[0].data is None


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]><x>&y;</x>',
        multistatus("https://foreign.example.test/a.ics", data=SIMPLE),
        multistatus("/wrong/a.ics", data=SIMPLE),
    ],
)
async def test_hostile_dav_response_fails(profile, payload):
    async with CalDAVClient(
        profile,
        transport=httpx.MockTransport(lambda r: httpx.Response(207, content=payload)),
    ) as client:
        with pytest.raises(SafeError) as caught:
            await query_resources(client, profile.calendars[0], "VEVENT", *RANGE)
    assert caught.value.code.value == "BAD_RESPONSE"


def home_listing(profile, *, retention="2592000", writable=False):
    home = profile.calendar_home
    root = ET.fromstring(
        multistatus(
            profile.calendars[0].href,
            calendar=True,
            writable=writable,
            components=("VEVENT", "VTODO"),
        )
    )
    for href, kind in ((home, None), (home + "trashbin/", "trash-bin")):
        response = ET.SubElement(root, f"{{{D}}}response")
        ET.SubElement(response, f"{{{D}}}href").text = href
        ps = ET.SubElement(response, f"{{{D}}}propstat")
        prop = ET.SubElement(ps, f"{{{D}}}prop")
        ET.SubElement(ps, f"{{{D}}}status").text = "HTTP/1.1 200 OK"
        rt = ET.SubElement(prop, f"{{{D}}}resourcetype")
        ET.SubElement(rt, f"{{{D}}}collection")
        if kind:
            ET.SubElement(rt, f"{{{NC}}}{kind}")
        elif retention is not None:
            ET.SubElement(
                prop, f"{{{NC}}}trash-bin-retention-duration"
            ).text = retention
    return ET.tostring(root)


async def test_home_status_reports_trash_bin_and_live_privileges(profile):
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(207, content=home_listing(profile))

    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        status = await home_status(client)
        live = await live_calendars(client, profile)
    assert status["retention"] == 2592000 and status["trashbin"] is True
    assert [c.components for c in status["calendars"]] == [("VEVENT", "VTODO")]
    # The enrolled id is kept, but privileges come from the server, not the profile.
    assert live[0].id == "calendar-1" and live[0].writable is False
    assert live[0].name == "Synthetic calendar"
    assert seen[0].headers["Depth"] == "1"
    assert str(seen[0].url).endswith(profile.calendar_home)


async def test_unenrolled_live_calendars_are_ignored(profile):
    other = replace(
        profile,
        calendars=(replace(profile.calendars[0], href=profile.calendar_home + "x/"),),
    )
    async with CalDAVClient(
        other,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(207, content=home_listing(profile))
        ),
    ) as client:
        assert await live_calendars(client, other) == []


async def test_delete_is_conditional_and_maps_conflict(profile):
    cal = profile.calendars[0]
    href = cal.href + "a.ics"
    resource = CalendarResource(event_id(cal, href), href, '"rev1"', SIMPLE.encode())
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(204 if len(seen) == 1 else 412)

    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        await delete_resource(client, cal, resource)
        with pytest.raises(SafeError) as caught:
            await delete_resource(client, cal, resource)
        assert caught.value.code.value == "CONFLICT"
        weak = replace(resource, etag='W/"rev1"')
        with pytest.raises(SafeError):
            await delete_resource(client, cal, weak)
        with pytest.raises(SafeError):
            await client.request("DELETE", href)
    assert [r.method for r in seen] == ["DELETE", "DELETE"]
    assert seen[0].headers["If-Match"] == '"rev1"'


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
