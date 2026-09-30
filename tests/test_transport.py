import ssl
from dataclasses import replace

import httpx
import pytest

from hermes_caldav_mcp.transport import CalDAVClient, event_href, event_id
from hermes_caldav_mcp.types import SafeError


@pytest.mark.parametrize(
    "href",
    [
        "http://cloud.example.test/nextcloud/remote.php/dav/calendars/calendar-user/personal/a.ics",
        "https://user:secret@cloud.example.test/x",
        "https://foreign.example.test/x",
        "https://cloud.example.test:444/x",
        "//foreign.example.test/x",
        "/nextcloud/remote.php/dav/files/a",
        "/nextcloud/remote.php/dav/calendars/calendar-user/personal-evil/a.ics",
        "/nextcloud/remote.php/dav/calendars/calendar-user/personal/../other/a.ics",
        "/nextcloud/remote.php/dav/calendars/calendar-user/personal/%2e%2e/a.ics",
        "/nextcloud/remote.php/dav/calendars/calendar-user/personal/a%2fb.ics",
        "/nextcloud/remote.php/dav/calendars/calendar-user/personal/%252e%252e.ics",
        "/nextcloud/remote.php/dav/calendars/calendar-user/personal/a.ics?x=1",
        "/nextcloud/remote.php/dav/calendars/calendar-user/personal/a.ics#x",
        "/nextcloud/remote.php/dav/calendars/calendar-user/outbox/a.ics",
        "/nextcloud/remote.php/dav/calendars/calendar-user/personal/attachments/a",
    ],
)
async def test_rejects_destinations_before_authentication(profile, href):
    requests = []
    async with CalDAVClient(
        profile, transport=httpx.MockTransport(lambda r: requests.append(r))
    ) as client:
        with pytest.raises(SafeError):
            await client.request("GET", href)
    assert requests == []


@pytest.mark.parametrize("method", ["DELETE", "MKCALENDAR", "POST", "get"])
async def test_forbidden_methods_never_send(profile, method):
    requests = []
    async with CalDAVClient(
        profile, transport=httpx.MockTransport(lambda r: requests.append(r))
    ) as client:
        with pytest.raises(SafeError):
            await client.request(method, profile.calendars[0].href + "a.ics")
    assert not requests


async def test_unicode_space_and_opaque_event_id(profile):
    cal = profile.calendars[0]
    href = cal.href + "caf%C3%A9%20meeting.ics"
    identifier = event_id(cal, href)
    assert "/" not in identifier
    assert event_href(cal, identifier) == href
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"calendar", headers={"ETag": '"rev1"'})

    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        result = await client.request("GET", "https://cloud.example.test" + href)
        assert result.etag == '"rev1"'
        assert result.data == b"calendar"
    assert len(seen) == 1
    assert seen[0].headers["Authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "AUTH_FAILED"),
        (403, "PERMISSION_DENIED"),
        (404, "NOT_FOUND"),
        (412, "CONFLICT"),
        (302, "BAD_RESPONSE"),
        (500, "BAD_RESPONSE"),
    ],
)
async def test_statuses_are_explicit_and_bodies_redacted(profile, status, code):
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(
            status,
            content=profile.password.encode(),
            headers={"Location": "https://foreign.example.test/"},
        )

    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SafeError) as error:
            await client.request("GET", profile.calendars[0].href + "a.ics")
    assert error.value.code.value == code
    assert profile.password not in str(error.value)
    assert len(seen) == 1


async def test_size_cap_without_content_length(profile):
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 128
            yield b"x" * 129

    async with CalDAVClient(
        profile,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Chunks())),
    ) as client:
        with pytest.raises(SafeError) as error:
            await client.request(
                "GET", profile.calendars[0].href + "a.ics", max_bytes=256
            )
    assert error.value.code.value == "LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "method,code", [("GET", "NETWORK_ERROR"), ("PUT", "UNKNOWN_WRITE_OUTCOME")]
)
async def test_network_failures_never_repeat_writes(profile, method, code):
    seen = []

    def handler(r):
        seen.append(r)
        raise httpx.ReadTimeout(profile.password)

    async with CalDAVClient(profile, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SafeError) as error:
            await client.request(
                method, profile.calendars[0].href + "a.ics", create=method == "PUT"
            )
    assert error.value.code.value == code
    assert profile.password not in str(error.value)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "revision", ['W/"weak"', '"bad\r\ninjected"', "unquoted", "", '"a"b"']
)
async def test_invalid_revisions_send_nothing(profile, revision):
    seen = []
    async with CalDAVClient(
        profile, transport=httpx.MockTransport(lambda r: seen.append(r))
    ) as client:
        with pytest.raises(SafeError):
            await client.request(
                "PUT", profile.calendars[0].href + "a.ics", expected_revision=revision
            )
    assert not seen


def test_verified_tls_and_no_ambient_proxy(profile, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    client = CalDAVClient(profile)
    assert client.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert client.ssl_context.check_hostname
    assert client.http._trust_env is False


@pytest.mark.parametrize(
    "url",
    [
        "http://cloud.example.test",
        "https://u:p@cloud.example.test",
        "https://cloud.example.test/?x=1",
        "https://cloud.example.test/#x",
    ],
)
def test_bad_profile_endpoint_is_rejected(profile, url):
    with pytest.raises(SafeError):
        CalDAVClient(replace(profile, base_url=url))


async def test_successful_put_never_reads_unneeded_response_body(profile):
    class Unreadable(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("PUT acknowledgement body must not be consumed")
            yield b""

    response = httpx.Response(
        201,
        stream=Unreadable(),
        headers={"ETag": '"new"', "Content-Length": "999999999"},
    )
    async with CalDAVClient(
        profile, transport=httpx.MockTransport(lambda r: response)
    ) as client:
        result = await client.request(
            "PUT", profile.calendars[0].href + "a.ics", create=True
        )
    assert result.status == 201
    assert result.etag == '"new"'


async def test_server_failure_after_put_reports_unknown_outcome(profile):
    async with CalDAVClient(
        profile, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    ) as client:
        with pytest.raises(SafeError) as caught:
            await client.request(
                "PUT", profile.calendars[0].href + "a.ics", create=True
            )
    assert caught.value.code.value == "UNKNOWN_WRITE_OUTCOME"
