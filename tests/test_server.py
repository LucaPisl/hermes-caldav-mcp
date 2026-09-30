import asyncio
from dataclasses import replace

import httpx
import pytest
from test_caldav import SIMPLE, multistatus

from hermes_caldav_mcp.credentials import CredentialStore
from hermes_caldav_mcp.server import CalendarService, execute, make_server
from hermes_caldav_mcp.transport import event_id


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
    "name,args",
    [
        ("delete_event", {}),
        ("list_calendars", {"secret": "forbidden"}),
        ("get_event", {"calendar_id": "other", "event_id": "bad"}),
        (
            "create_event",
            {
                "calendar_id": "calendar-1",
                "fields": {
                    "title": "x",
                    "start": "2025-01-01",
                    "end": "2025-01-02",
                    "all_day": True,
                    "attendees": ["evil"],
                },
            },
        ),
    ],
)
async def test_forbidden_inputs_cannot_send_requests(profile, name, args):
    seen = []
    service = CalendarService(
        lambda: profile,
        transport_factory=lambda: httpx.MockTransport(lambda r: seen.append(r)),
    )
    result = await execute(service, name, args)
    assert not result["ok"]
    assert not seen


async def test_internal_exception_is_redacted(profile, capsys):
    def loader():
        raise RuntimeError(profile.password)

    result = await execute(CalendarService(loader), "list_calendars", {})
    assert result["error"]["code"] == "INTERNAL_ERROR"
    assert profile.password not in str(result) + str(capsys.readouterr())


async def test_service_get_put_race_has_revision_and_no_retry(profile):
    cal = profile.calendars[0]
    seen = []

    def handler(r):
        seen.append(r)
        if r.method == "GET":
            return httpx.Response(200, content=SIMPLE, headers={"ETag": '"old"'})
        assert r.headers["If-Match"] == '"old"'
        return httpx.Response(412)

    service = CalendarService(
        lambda: profile, transport_factory=lambda: httpx.MockTransport(handler)
    )
    result = await execute(
        service,
        "update_event",
        {
            "calendar_id": cal.id,
            "event_id": event_id(cal, cal.href + "a.ics"),
            "expected_revision": '"old"',
            "patch": {"title": "Changed"},
        },
    )
    assert result["error"]["code"] == "CONFLICT"
    assert [r.method for r in seen] == ["GET", "PUT"]


async def test_read_only_calendar_never_writes(profile):
    cal = replace(profile.calendars[0], writable=False)
    profile = replace(profile, calendars=(cal,))
    seen = []
    service = CalendarService(
        lambda: profile,
        transport_factory=lambda: httpx.MockTransport(lambda r: seen.append(r)),
    )
    result = await execute(
        service,
        "create_event",
        {
            "calendar_id": cal.id,
            "fields": {
                "title": "x",
                "start": "2025-01-01",
                "end": "2025-01-02",
                "all_day": True,
            },
        },
    )
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert not seen


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
        service,
        "create_event",
        {
            "calendar_id": "calendar-1",
            "fields": {
                "title": "x",
                "start": "2025-01-01",
                "end": "2025-01-02",
                "all_day": True,
            },
        },
    )
    assert result["error"]["code"] == "UNKNOWN_WRITE_OUTCOME"
    assert len(seen) == 1


def test_mcp_advertises_only_tools(profile):
    server = make_server(CalendarService(lambda: profile))
    caps = server.get_capabilities()
    assert caps.tools is not None
    assert caps.resources is None
    assert caps.prompts is None
    assert caps.logging is None
