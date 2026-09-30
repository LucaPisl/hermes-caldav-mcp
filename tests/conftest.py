import pytest
from fakes import FakeCollection

from hermes_caldav_mcp.types import CalendarRef, ConnectionProfile


@pytest.fixture
def profile():
    return ConnectionProfile(
        "https://cloud.example.test/nextcloud",
        "calendar-user",
        "synthetic-password",
        "/nextcloud/remote.php/dav/calendars/calendar-user/",
        (
            CalendarRef(
                "calendar-1",
                "/nextcloud/remote.php/dav/calendars/calendar-user/personal/",
                "Personal",
                True,
            ),
        ),
        "Europe/Berlin",
    )


@pytest.fixture
def collection():
    return FakeCollection()
