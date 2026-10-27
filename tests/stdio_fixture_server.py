"""Synthetic test process; excluded from the installed package.

Usage: stdio_fixture_server.py [full] [delete STATE_DIR]
"""

import asyncio
import logging
import sys
from pathlib import Path

from dav_fake import HOME, FakeDAV, calendar

from hermes_caldav_mcp.deletion import DeletePolicy
from hermes_caldav_mcp.server import CalendarService, make_server, serve
from hermes_caldav_mcp.types import ConnectionProfile

CAL = calendar("personal", "Synthetic calendar", components=("VEVENT", "VTODO"))
PROFILE = ConnectionProfile(
    "https://cloud.example.test/nextcloud",
    "calendar-user",
    "synthetic-password",
    HOME,
    (CAL,),
    "UTC",
)
DAV = FakeDAV([CAL])
DAV.add(CAL, "a.ics", (Path(__file__).parent / "fixtures/simple.ics").read_bytes())

args = sys.argv[1:]
policy = None
if "delete" in args:
    policy = DeletePolicy(directory=Path(args[args.index("delete") + 1]))
logging.disable(logging.CRITICAL)
asyncio.run(
    serve(
        make_server(
            CalendarService(
                lambda: PROFILE, transport_factory=DAV.transport, delete_policy=policy
            ),
            tools="full" if "full" in args else "lite",
        )
    )
)
