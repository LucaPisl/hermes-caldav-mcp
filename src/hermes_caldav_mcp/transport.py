"""Constrain every authenticated request before constructing its headers."""

import base64
import re
import ssl
from urllib.parse import quote, unquote, urlsplit

import httpx

from .types import MAX_DAV_BYTES, ErrorCode, HTTPResult, SafeError


def path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
    ):
        raise SafeError(ErrorCode.INVALID_INPUT)
    if re.search(r"[\x00-\x1f\x7f\\?#]", value) or re.search(
        r"%(?![0-9a-fA-F]{2})", value
    ):
        raise SafeError(ErrorCode.INVALID_INPUT)
    segments = value.split("/")[1:]
    normalized = []
    for i, segment in enumerate(segments):
        try:
            decoded = unquote(segment, encoding="utf-8", errors="strict")
        except UnicodeError:
            raise SafeError(ErrorCode.INVALID_INPUT) from None
        if decoded in (".", "..") or re.search(r"[%/\\\x00-\x1f\x7f]", decoded):
            raise SafeError(ErrorCode.INVALID_INPUT)
        if not decoded and i != len(segments) - 1:
            raise SafeError(ErrorCode.INVALID_INPUT)
        normalized.append(quote(decoded, safe="-._~"))
    return "/" + "/".join(normalized)


def strong_etag(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 256
        and bool(re.fullmatch(r'"[\x21\x23-\x7e]*"', value))
    )


def event_id(calendar, href: str) -> str:
    resource = path(href)
    root = path(calendar.href)
    if not root.endswith("/") or not resource.startswith(root):
        raise SafeError(ErrorCode.INVALID_INPUT)
    filename = resource[len(root) :]
    if not filename or "/" in filename:
        raise SafeError(ErrorCode.INVALID_INPUT)
    return base64.urlsafe_b64encode(filename.encode()).decode().rstrip("=")


def event_href(calendar, identifier: str) -> str:
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,2048}", identifier
    ):
        raise SafeError(ErrorCode.INVALID_INPUT)
    try:
        filename = base64.b64decode(
            identifier + "=" * (-len(identifier) % 4), altchars=b"-_", validate=True
        ).decode()
        href = path(calendar.href) + filename
        if event_id(calendar, href) != identifier:
            raise ValueError("noncanonical")
        return href
    except (ValueError, UnicodeError):
        raise SafeError(ErrorCode.INVALID_INPUT) from None


def endpoint(value: str):
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f\\]", value):
        raise SafeError(ErrorCode.INVALID_INPUT)
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "?" in value
            or "#" in value
        ):
            raise ValueError("url")
        host = parts.hostname.encode("idna").decode().lower()
        if "%" in host:
            raise ValueError("host")
        port = parts.port or 443
        if not 1 <= port <= 65535:
            raise ValueError("port")
        host_url = f"[{host}]" if ":" in host else host
        origin = f"https://{host_url}" + (f":{port}" if port != 443 else "")
        return origin, path(parts.path or "/"), (host, port)
    except (ValueError, UnicodeError):
        raise SafeError(ErrorCode.INVALID_INPUT) from None


class CalDAVClient:
    def __init__(self, profile, *, transport=None, discovery=False):
        self.write_started = False
        self.profile = profile
        self.discovery = discovery
        self.origin, base, self.authority = endpoint(profile.base_url)
        self.home = path(profile.calendar_home)
        if not self.home.startswith(
            base.rstrip("/") + "/remote.php/dav/calendars/"
        ) or not self.home.endswith("/"):
            raise SafeError(ErrorCode.INVALID_INPUT)
        self.roots = tuple(path(c.href) for c in profile.calendars)
        if any(
            not root.startswith(self.home)
            or not root.endswith("/")
            or "/" in root[len(self.home) : -1]
            or root == self.home
            for root in self.roots
        ):
            raise SafeError(ErrorCode.INVALID_INPUT)
        if (
            not profile.username
            or ":" in profile.username
            or any(ord(c) < 32 for c in profile.username)
        ):
            raise SafeError(ErrorCode.INVALID_INPUT)
        try:
            self.ssl_context = ssl.create_default_context(cafile=profile.ca_bundle)
        except (OSError, ssl.SSLError):
            raise SafeError(ErrorCode.NETWORK_ERROR) from None
        self.http = httpx.AsyncClient(
            verify=self.ssl_context,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            timeout=httpx.Timeout(20, connect=10),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    def validated_href(self, href: str, *, discovered=False) -> str:
        if href.startswith("https://"):
            origin, resource, authority = endpoint(href)
            if authority != self.authority or origin != self.origin:
                raise SafeError(ErrorCode.INVALID_INPUT)
        else:
            resource = path(href)
        if discovered:
            if resource == self.home:
                return resource
            if (
                resource.startswith(self.home)
                and resource.endswith("/")
                and "/" not in resource[len(self.home) : -1]
                and resource != self.home
            ):
                return resource
        for root in self.roots:
            if resource == root or (
                resource.startswith(root)
                and resource[len(root) :]
                and "/" not in resource[len(root) :]
            ):
                return resource
        raise SafeError(ErrorCode.INVALID_INPUT)

    async def request(
        self,
        method: str,
        href: str,
        *,
        body=None,
        expected_revision=None,
        create=False,
        max_bytes=MAX_DAV_BYTES,
    ) -> HTTPResult:
        if (
            method not in {"PROPFIND", "REPORT", "GET", "PUT"}
            or not 1 <= max_bytes <= MAX_DAV_BYTES
        ):
            raise SafeError(ErrorCode.INVALID_INPUT)
        resource = self.validated_href(href, discovered=self.discovery)
        if self.discovery:
            if method != "PROPFIND" or resource != self.home:
                raise SafeError(ErrorCode.INVALID_INPUT)
        elif (method in {"REPORT", "PROPFIND"}) != resource.endswith("/"):
            raise SafeError(ErrorCode.INVALID_INPUT)
        if method != "PUT" and (create or expected_revision is not None):
            raise SafeError(ErrorCode.INVALID_INPUT)
        headers = {"Accept-Encoding": "identity"}
        if method == "PROPFIND":
            headers.update(
                {
                    "Depth": "1" if self.discovery else "0",
                    "Content-Type": "application/xml; charset=utf-8",
                }
            )
        elif method == "REPORT":
            headers.update(
                {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}
            )
        elif method == "PUT":
            if create == (expected_revision is not None) or (
                expected_revision is not None and not strong_etag(expected_revision)
            ):
                raise SafeError(ErrorCode.INVALID_INPUT)
            headers["Content-Type"] = "text/calendar; charset=utf-8"
            headers["If-None-Match" if create else "If-Match"] = (
                "*" if create else expected_revision
            )
        try:
            if method == "PUT":
                self.write_started = True
            async with self.http.stream(
                method,
                self.origin + resource,
                headers=headers,
                content=body,
                auth=httpx.BasicAuth(self.profile.username, self.profile.password),
            ) as response:
                status = response.status_code
                failures = {
                    401: ErrorCode.AUTH_FAILED,
                    403: ErrorCode.PERMISSION_DENIED,
                    404: ErrorCode.NOT_FOUND,
                    412: ErrorCode.CONFLICT,
                }
                if status in failures:
                    raise SafeError(failures[status])
                allowed = {
                    "PROPFIND": {207},
                    "REPORT": {207},
                    "GET": {200},
                    "PUT": {200, 201, 204},
                }
                if status not in allowed[method]:
                    if method == "PUT" and status >= 500:
                        raise SafeError(ErrorCode.UNKNOWN_WRITE_OUTCOME)
                    raise SafeError(ErrorCode.BAD_RESPONSE)
                etag = response.headers.get("ETag")
                if method == "PUT":
                    # Status and ETag suffice. Consuming an unused response body
                    # could turn an acknowledged write into a false failure.
                    return HTTPResult(status, b"", etag if strong_etag(etag) else None)
                # Reject compressed responses to bound decompression memory as well as bytes.
                if (
                    response.headers.get("Content-Encoding", "identity").lower()
                    != "identity"
                ):
                    raise SafeError(ErrorCode.BAD_RESPONSE)
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        if int(length) < 0:
                            raise ValueError("length")
                        if int(length) > max_bytes:
                            raise SafeError(ErrorCode.LIMIT_EXCEEDED)
                    except ValueError:
                        raise SafeError(ErrorCode.BAD_RESPONSE) from None
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > max_bytes:
                        raise SafeError(ErrorCode.LIMIT_EXCEEDED)
                    data.extend(chunk)
                etag = response.headers.get("ETag")
                return HTTPResult(
                    status, bytes(data), etag if strong_etag(etag) else None
                )
        except httpx.HTTPError:
            raise SafeError(
                ErrorCode.UNKNOWN_WRITE_OUTCOME
                if method == "PUT"
                else ErrorCode.NETWORK_ERROR
            ) from None
