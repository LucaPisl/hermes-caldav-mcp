"""Synthetic D-Bus boundary; real SecretStorage negotiates and decrypts sessions."""

import json
from dataclasses import asdict

from jeepney import HeaderFields, new_error, new_method_return
from secretstorage.dhcrypto import Session
from secretstorage.util import format_secret


class KeyringBus:
    def __init__(self, profile=None):
        self.names = {
            "org.gnome.keyring": ":1.10",
            "org.kde.secretservicecompat": ":1.20",
            "org.freedesktop.secrets": ":1.10",
        }
        self.activatable = set(self.names)
        self.locked = set()
        self.plain = set()
        self.missing_default = set()
        self.calls = []
        self.sessions = {}
        self.closed = False
        self.switch_default_on_read = False
        self.payload = (
            json.dumps({"version": 1, "profile": asdict(profile)}).encode()
            if profile
            else None
        )

    def close(self):
        self.closed = True

    def send_and_get_reply(self, message, *, timeout=None):
        fields = message.header.fields
        destination = fields[HeaderFields.destination]
        method = fields[HeaderFields.member]
        self.calls.append((destination, method, message.body, timeout))

        def reply(signature, *body):
            return new_method_return(message, signature, body)

        def error(name):
            return new_error(message, name, "s", ("synthetic error",))

        if destination == "org.freedesktop.DBus":
            if method == "ListNames":
                return reply("as", list(self.names))
            if method == "ListActivatableNames":
                return reply("as", list(self.activatable))
            if method == "GetNameOwner":
                if message.body[0] not in self.names:
                    return error("org.freedesktop.DBus.Error.NameHasNoOwner")
                return reply("s", self.names[message.body[0]])
            if method == "GetConnectionUnixProcessID":
                return reply("u", 123)
        owner = self.names.get(destination, destination)
        if owner not in {":1.10", ":1.20", ":1.30"}:
            return error("org.freedesktop.DBus.Error.ServiceUnknown")
        if method == "ReadAlias":
            if self.switch_default_on_read:
                self.names["org.freedesktop.secrets"] = ":1.20"
            return reply(
                "o",
                "/"
                if owner in self.missing_default
                else "/org/freedesktop/secrets/collection/default",
            )
        if method == "Get":
            name = message.body[1]
            values = {
                "Label": ("s", "Synthetic wallet"),
                "Locked": ("b", owner in self.locked),
                "Attributes": (
                    "a{ss}",
                    {"application": "hermes-caldav-mcp", "profile": "default"},
                ),
            }
            return reply("v", values[name])
        if method == "OpenSession":
            if owner in self.plain:
                if message.body[0] != "plain":
                    return error("org.freedesktop.DBus.Error.NotSupported")
                return reply("vo", ("s", ""), "/org/freedesktop/secrets/session/plain")
            session = Session()
            session.object_path = "/org/freedesktop/secrets/session/encrypted"
            session.set_server_public_key(int.from_bytes(message.body[1][1], "big"))
            self.sessions[owner] = session
            return reply(
                "vo",
                ("ay", session.my_public_key.to_bytes(128, "big")),
                session.object_path,
            )
        if method == "SearchItems":
            return reply(
                "ao",
                ["/org/freedesktop/secrets/collection/default/item"]
                if self.payload
                else [],
            )
        if method == "GetSecret":
            return reply(
                "(oayays)",
                format_secret(self.sessions[owner], self.payload, "application/json"),
            )
        if method == "CreateItem":
            return reply("oo", "/org/freedesktop/secrets/collection/default/item", "/")
        if method == "Delete":
            self.payload = None
            return reply("o", "/")
        raise AssertionError("Unexpected D-Bus method: " + method)
