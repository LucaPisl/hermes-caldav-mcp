"""A small in-memory Nextcloud: calendars, resources and conditional writes."""

from xml.etree import ElementTree as ET

import httpx

from hermes_caldav_mcp.transport import event_id
from hermes_caldav_mcp.types import CalendarRef

D = "DAV:"
C = "urn:ietf:params:xml:ns:caldav"
NC = "http://nextcloud.com/ns"
HOME = "/nextcloud/remote.php/dav/calendars/calendar-user/"


def calendar(slug, name, writable=True, components=("VEVENT",)):
    return CalendarRef("cal-" + slug, HOME + slug + "/", name, writable, components)


def _response(root, href, status="HTTP/1.1 200 OK"):
    response = ET.SubElement(root, f"{{{D}}}response")
    ET.SubElement(response, f"{{{D}}}href").text = href
    ps = ET.SubElement(response, f"{{{D}}}propstat")
    prop = ET.SubElement(ps, f"{{{D}}}prop")
    ET.SubElement(ps, f"{{{D}}}status").text = status
    return prop


class FakeDAV:
    def __init__(self, calendars, *, retention="2592000", trashbin=True):
        self.calendars = list(calendars)
        self.retention = retention
        self.trashbin = trashbin
        self.store = {}
        self.requests = []
        self.failing = {}  # calendar href -> status for REPORT
        self.counter = 0

    def add(self, cal, name, data):
        href = cal.href + name
        self.counter += 1
        self.store[href] = (
            data if isinstance(data, bytes) else data.encode(),
            f'"e{self.counter}"',
        )
        return event_id(cal, href)

    def writes(self):
        return [r for r in self.requests if r.method in {"PUT", "DELETE"}]

    def transport(self):
        return httpx.MockTransport(self)

    def _home(self):
        root = ET.Element(f"{{{D}}}multistatus")
        prop = _response(root, HOME)
        rt = ET.SubElement(prop, f"{{{D}}}resourcetype")
        ET.SubElement(rt, f"{{{D}}}collection")
        if self.trashbin:
            # Like Nextcloud: the retention is a property of the trash-bin collection.
            prop = _response(root, HOME + "trashbin/")
            rt = ET.SubElement(prop, f"{{{D}}}resourcetype")
            ET.SubElement(rt, f"{{{NC}}}trash-bin")
            if self.retention is not None:
                ET.SubElement(
                    prop, f"{{{NC}}}trash-bin-retention-duration"
                ).text = self.retention
        for cal in self.calendars:
            prop = _response(root, cal.href)
            rt = ET.SubElement(prop, f"{{{D}}}resourcetype")
            ET.SubElement(rt, f"{{{D}}}collection")
            ET.SubElement(rt, f"{{{C}}}calendar")
            ET.SubElement(prop, f"{{{D}}}displayname").text = cal.name
            comps = ET.SubElement(prop, f"{{{C}}}supported-calendar-component-set")
            for name in cal.components:
                ET.SubElement(comps, f"{{{C}}}comp", name=name)
            privs = ET.SubElement(prop, f"{{{D}}}current-user-privilege-set")
            p = ET.SubElement(privs, f"{{{D}}}privilege")
            ET.SubElement(p, f"{{{D}}}read")
            if cal.writable:
                p = ET.SubElement(privs, f"{{{D}}}privilege")
                ET.SubElement(p, f"{{{D}}}write-content")
        return ET.tostring(root)

    def _report(self, href, body):
        query = ET.fromstring(body)
        component = query.findall(f".//{{{C}}}comp-filter")[1].get("name")
        root = ET.Element(f"{{{D}}}multistatus")
        for path, (data, etag) in self.store.items():
            if path.rsplit("/", 1)[0] + "/" != href:
                continue
            if b"BEGIN:" + component.encode() not in data and b"BEGIN:V" in data:
                continue
            prop = _response(root, path)
            ET.SubElement(prop, f"{{{D}}}getetag").text = etag
            ET.SubElement(prop, f"{{{C}}}calendar-data").text = data.decode()
        return ET.tostring(root)

    def _calendar_of(self, path):
        return next(c for c in self.calendars if path.startswith(c.href))

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.method == "PROPFIND":
            assert path == HOME and request.headers["Depth"] == "1"
            return httpx.Response(207, content=self._home())
        if request.method == "REPORT":
            if path in self.failing:
                return httpx.Response(self.failing[path])
            return httpx.Response(207, content=self._report(path, request.content))
        current = self.store.get(path)
        if request.method == "GET":
            if current is None:
                return httpx.Response(404)
            return httpx.Response(200, content=current[0], headers={"ETag": current[1]})
        if not self._calendar_of(path).writable:
            return httpx.Response(403)
        if request.method == "PUT":
            if request.headers.get("If-None-Match") == "*":
                if current is not None:
                    return httpx.Response(412)
            elif current is None or request.headers.get("If-Match") != current[1]:
                return httpx.Response(412)
            self.counter += 1
            etag = f'"e{self.counter}"'
            self.store[path] = (request.content, etag)
            return httpx.Response(
                201 if current is None else 204, headers={"ETag": etag}
            )
        if request.method == "DELETE":
            if current is None or request.headers.get("If-Match") != current[1]:
                return httpx.Response(412)
            del self.store[path]
            return httpx.Response(204)
        raise AssertionError("unexpected request")
