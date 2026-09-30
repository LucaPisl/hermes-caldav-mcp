"""Synthetic test process; excluded from the installed package."""

import asyncio
import logging
from pathlib import Path

import httpx
from test_caldav import multistatus

from hermes_caldav_mcp.server import CalendarService, make_server, serve
from hermes_caldav_mcp.types import CalendarRef, ConnectionProfile

CAL = CalendarRef(
    "calendar-1",
    "/nextcloud/remote.php/dav/calendars/calendar-user/personal/",
    "Synthetic calendar",
    True,
)
PROFILE = ConnectionProfile(
    "https://cloud.example.test/nextcloud",
    "calendar-user",
    "synthetic-password",
    "/nextcloud/remote.php/dav/calendars/calendar-user/",
    (CAL,),
    "UTC",
)
DATA = (Path(__file__).parent / "fixtures/simple.ics").read_bytes()
RESOURCES = {CAL.href + "a.ics": (DATA, '"old"')}


def handler(request):
    if request.method == "PROPFIND":
        return httpx.Response(207, content=multistatus(CAL.href, calendar=True))
    if request.method == "REPORT":
        return httpx.Response(
            207, content=multistatus(CAL.href + "a.ics", data=DATA.decode())
        )
    if request.method == "GET":
        resource = RESOURCES.get(request.url.path)
        if resource is None:
            return httpx.Response(404)
        data, revision = resource
        return httpx.Response(200, content=data, headers={"ETag": revision})
    if request.method == "PUT":
        resource = RESOURCES.get(request.url.path)
        if request.headers.get("If-None-Match") == "*":
            if resource is not None:
                return httpx.Response(412)
        elif resource is None or request.headers.get("If-Match") != resource[1]:
            return httpx.Response(412)
        RESOURCES[request.url.path] = (request.content, '"fresh"')
        return httpx.Response(201, headers={"ETag": '"fresh"'})
    raise AssertionError("forbidden test request")


logging.disable(logging.CRITICAL)
asyncio.run(
    serve(
        make_server(
            CalendarService(
                lambda: PROFILE, transport_factory=lambda: httpx.MockTransport(handler)
            )
        )
    )
)
