"""The small DAV subset needed for selected calendar collections, VEVENTs and VTODOs."""

import hashlib
import re
from xml.etree import ElementTree as XML

from defusedxml import ElementTree

from .events import parse_calendar
from .transport import event_href, event_id, strong_etag
from .types import (
    MAX_EVENT_BYTES,
    CalendarRef,
    CalendarResource,
    ErrorCode,
    SafeError,
)

D = "DAV:"
C = "urn:ietf:params:xml:ns:caldav"
NC = "http://nextcloud.com/ns"
COMPONENTS = ("VEVENT", "VTODO")


def _tag(namespace, name):
    return f"{{{namespace}}}{name}"


def _status(text):
    match = re.fullmatch(r"HTTP/\d(?:\.\d)? (\d{3})(?: .*)?", text or "")
    if not match:
        raise SafeError(ErrorCode.BAD_RESPONSE)
    return int(match[1])


def _failure(status):
    raise SafeError(
        {
            401: ErrorCode.AUTH_FAILED,
            403: ErrorCode.PERMISSION_DENIED,
            404: ErrorCode.NOT_FOUND,
            412: ErrorCode.CONFLICT,
        }.get(status, ErrorCode.BAD_RESPONSE)
    )


def _responses(data):
    try:
        tree = ElementTree.fromstring(
            data, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
        if tree.tag != _tag(D, "multistatus") or sum(1 for _ in tree.iter()) > 8192:
            raise ValueError("xml")
        result = []
        for response in tree.findall(_tag(D, "response")):
            hrefs = response.findall(_tag(D, "href"))
            if len(hrefs) != 1 or not hrefs[0].text:
                raise ValueError("href")
            status = response.find(_tag(D, "status"))
            if status is not None:
                if _status(status.text) != 200:
                    _failure(_status(status.text))
            props, failures = {}, {}
            for ps in response.findall(_tag(D, "propstat")):
                code = _status(ps.findtext(_tag(D, "status")))
                prop = ps.find(_tag(D, "prop"))
                if prop is None:
                    raise ValueError("prop")
                for element in prop:
                    if element.tag in props or element.tag in failures:
                        raise ValueError("duplicate property")
                    if code == 200:
                        props[element.tag] = element
                    else:
                        failures[element.tag] = code
            if not props and not failures:
                raise ValueError("properties")
            result.append((hrefs[0].text, props, failures))
        return result
    except SafeError:
        raise
    except Exception:
        raise SafeError(ErrorCode.BAD_RESPONSE) from None


def _required(props, failures, tag):
    if tag in failures:
        _failure(failures[tag])
    if tag not in props:
        raise SafeError(ErrorCode.BAD_RESPONSE)
    return props[tag]


def _propfind_body():
    root = XML.Element(_tag(D, "propfind"))
    prop = XML.SubElement(root, _tag(D, "prop"))
    for ns, name in (
        (D, "resourcetype"),
        (D, "displayname"),
        (D, "current-user-privilege-set"),
        (C, "supported-calendar-component-set"),
        (NC, "trash-bin-retention-duration"),
    ):
        XML.SubElement(prop, _tag(ns, name))
    return XML.tostring(root, encoding="utf-8", xml_declaration=True)


def _calendar(href, props, failures, identifier=None):
    rt = _required(props, failures, _tag(D, "resourcetype"))
    if rt.find(_tag(C, "calendar")) is None:
        return None
    comps = props.get(_tag(C, "supported-calendar-component-set"))
    names = tuple(
        n
        for n in COMPONENTS
        if any(c.get("name") == n for c in (() if comps is None else comps))
    )
    if not names:
        return None
    priv = props.get(_tag(D, "current-user-privilege-set"))
    writable = priv is not None and any(
        priv.find(".//" + _tag(D, k)) is not None
        for k in ("write-content", "write", "all")
    )
    display = props.get(_tag(D, "displayname"))
    name = display.text if display is not None and display.text else "Calendar"
    return CalendarRef(
        identifier or "cal-" + hashlib.sha256(href.encode()).hexdigest()[:20],
        href,
        name[:512],
        writable,
        names,
    )


async def home_status(client):
    """One PROPFIND of the calendar home: its calendars and trash-bin details."""
    response = await client.request("PROPFIND", client.home, body=_propfind_body())
    calendars, seen, retention, trashbin = [], set(), None, False
    for href, props, failures in _responses(response.data):
        try:
            href = client.validated_href(href, discovered=True)
        except SafeError:
            raise SafeError(ErrorCode.BAD_RESPONSE) from None
        if href in seen:
            raise SafeError(ErrorCode.BAD_RESPONSE)
        seen.add(href)
        if href == client.home:
            value = props.get(_tag(NC, "trash-bin-retention-duration"))
            if value is not None and (value.text or "").strip().isdigit():
                retention = int(value.text.strip())
            continue
        rt = props.get(_tag(D, "resourcetype"))
        if rt is not None and rt.find(_tag(NC, "trash-bin")) is not None:
            trashbin = True
            continue
        calendar = _calendar(href, props, failures)
        if calendar is not None:
            calendars.append(calendar)
        if len(calendars) > 100:
            raise SafeError(ErrorCode.LIMIT_EXCEEDED)
    return {"calendars": calendars, "retention": retention, "trashbin": trashbin}


async def discover_calendars(client):
    return (await home_status(client))["calendars"]


async def live_calendars(client, profile):
    """Enrolled calendars only, with current names, privileges and components."""
    live = {c.href: c for c in await discover_calendars(client)}
    return [
        CalendarRef(
            c.id,
            c.href,
            live[c.href].name,
            live[c.href].writable,
            live[c.href].components,
        )
        for c in profile.calendars
        if c.href in live
    ]


def _query_body(component, start=None, end=None):
    root = XML.Element(_tag(C, "calendar-query"))
    prop = XML.SubElement(root, _tag(D, "prop"))
    XML.SubElement(prop, _tag(D, "getetag"))
    XML.SubElement(prop, _tag(C, "calendar-data"))
    filt = XML.SubElement(root, _tag(C, "filter"))
    vc = XML.SubElement(filt, _tag(C, "comp-filter"), name="VCALENDAR")
    comp = XML.SubElement(vc, _tag(C, "comp-filter"), name=component)
    if start is not None:
        # Unexpanded resources keep their TZIDs; recurrences expand locally.
        bounds = {
            "start": start.strftime("%Y%m%dT%H%M%SZ"),
            "end": end.strftime("%Y%m%dT%H%M%SZ"),
        }
        XML.SubElement(comp, _tag(C, "time-range"), bounds)
    return XML.tostring(root, encoding="utf-8", xml_declaration=True)


async def query_resources(client, calendar, component="VEVENT", start=None, end=None):
    """Matching resources; ones that cannot be parsed come back with data=None."""
    response = await client.request(
        "REPORT", calendar.href, body=_query_body(component, start, end)
    )
    resources, seen = [], set()
    for href, props, _failures in _responses(response.data):
        try:
            href = client.validated_href(href)
            identifier = event_id(calendar, href)
        except SafeError:
            raise SafeError(ErrorCode.BAD_RESPONSE) from None
        if href in seen:
            raise SafeError(ErrorCode.BAD_RESPONSE)
        seen.add(href)
        element = props.get(_tag(C, "calendar-data"))
        data = (element.text or "").encode() if element is not None else b""
        etag_prop = props.get(_tag(D, "getetag"))
        etag = etag_prop.text if etag_prop is not None else None
        try:
            parse_calendar(data, component)
        except SafeError:
            data = None
        resources.append(
            CalendarResource(
                identifier, href, etag if strong_etag(etag) else None, data
            )
        )
    return resources


async def get_resource(client, calendar, identifier, component="VEVENT"):
    href = event_href(calendar, identifier)
    result = await client.request("GET", href, max_bytes=MAX_EVENT_BYTES)
    parse_calendar(result.data, component)
    return CalendarResource(identifier, href, result.etag, result.data)


async def create_resource(client, calendar, uid, data):
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", uid) or len(data) > MAX_EVENT_BYTES:
        raise SafeError(ErrorCode.INVALID_INPUT)
    href = calendar.href + uid + ".ics"
    result = await client.request(
        "PUT", href, body=data, create=True, max_bytes=MAX_EVENT_BYTES
    )
    return {
        "event_id": event_id(calendar, href),
        "revision": result.etag,
        "created": True,
    }


async def update_resource(client, calendar, resource, expected_revision, data):
    if (
        not strong_etag(expected_revision)
        or not strong_etag(resource.etag)
        or expected_revision != resource.etag
    ):
        raise SafeError(ErrorCode.CONFLICT)
    if (
        event_href(calendar, resource.event_id) != resource.href
        or len(data) > MAX_EVENT_BYTES
    ):
        raise SafeError(ErrorCode.INVALID_INPUT)
    result = await client.request(
        "PUT",
        resource.href,
        body=data,
        expected_revision=expected_revision,
        max_bytes=MAX_EVENT_BYTES,
    )
    return {"event_id": resource.event_id, "revision": result.etag, "updated": True}


async def delete_resource(client, calendar, resource):
    if (
        not strong_etag(resource.etag)
        or event_href(calendar, resource.event_id) != resource.href
    ):
        raise SafeError(ErrorCode.CONFLICT)
    await client.request("DELETE", resource.href, expected_revision=resource.etag)
