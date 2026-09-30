"""Secret Service only. Searchable metadata deliberately contains no connection data."""

import json
import os
import re
import time
from contextlib import contextmanager
from copy import copy
from dataclasses import asdict
from pathlib import Path
from zoneinfo import ZoneInfo

from jeepney import DBus, DBusErrorResponse, HeaderFields, MessageType
from jeepney.io.blocking import open_dbus_connection
from secretstorage.collection import get_collection_by_alias
from secretstorage.exceptions import LockedException
from secretstorage.util import open_session

from .types import CalendarRef, ConnectionProfile, ErrorCode, SafeError

APPLICATION = "hermes-caldav-mcp"
MAX_PROFILE_BYTES = 32768
KEYRING_ALIASES = {
    "gnome": ("org.gnome.keyring",),
    "kde": ("org.kde.secretservicecompat", "org.kde.kwalletd6", "org.kde.kwalletd5"),
    "system": ("org.freedesktop.secrets",),
}


def _bus_reply(connection, message):
    reply = connection.send_and_get_reply(message)
    if reply.header.message_type == MessageType.error:
        raise DBusErrorResponse(reply)
    return reply.body


def _owner_executable(connection, owner):
    (pid,) = _bus_reply(connection, DBus().GetConnectionUnixProcessID(owner))
    if type(pid) is not int or pid <= 0:
        raise ValueError("Invalid process identity")
    return str(Path(f"/proc/{pid}/exe").readlink())


def _keyring_owner(connection, provider):
    for name in KEYRING_ALIASES[provider]:
        try:
            (owner,) = _bus_reply(connection, DBus().GetNameOwner(name))
            return owner
        except DBusErrorResponse as error:
            if error.name != "org.freedesktop.DBus.Error.NameHasNoOwner":
                raise
    raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)


def _connect():
    # Hermes filters environment variables. The standard Linux user bus path
    # avoids putting machine identifiers in the generated configuration.
    bus = (
        "SESSION"
        if os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        else f"unix:path=/run/user/{os.getuid()}/bus"
    )
    return open_dbus_connection(bus, auth_timeout=5)


class _BoundedConnection:
    def __init__(self, connection, *, destination="org.freedesktop.secrets"):
        self.connection = connection
        self.destination = destination
        self.deadline = time.monotonic() + 10

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def _timeout(self, timeout):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        return min(remaining, timeout) if timeout is not None else remaining

    def send_and_get_reply(self, message, *, timeout=None):
        if (
            message.header.fields.get(HeaderFields.destination)
            == "org.freedesktop.secrets"
        ):
            # Route this connection only; do not modify SecretStorage globals or
            # mutate a message that another connection might also use.
            message = copy(message)
            message.header = copy(message.header)
            message.header.fields = dict(message.header.fields)
            message.header.fields[HeaderFields.destination] = self.destination
        return self.connection.send_and_get_reply(
            message, timeout=self._timeout(timeout)
        )

    def recv_until_filtered(self, queue, *, timeout=None):
        return self.connection.recv_until_filtered(
            queue, timeout=self._timeout(timeout)
        )


def validate_profile_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name
    ):
        raise SafeError(ErrorCode.INVALID_INPUT)
    return name


def _decode(data: bytes) -> ConnectionProfile:
    if len(data) > MAX_PROFILE_BYTES:
        raise ValueError("size")
    obj = json.loads(data)
    if set(obj) != {"version", "profile"} or obj["version"] != 1:
        raise ValueError("schema")
    p = obj["profile"]
    if set(p) != set(ConnectionProfile.__dataclass_fields__):
        raise ValueError("schema")
    for key in (
        "base_url",
        "username",
        "password",
        "calendar_home",
        "default_timezone",
    ):
        if not isinstance(p[key], str) or not p[key] or len(p[key]) > 8192:
            raise ValueError("field")
    if p["ca_bundle"] is not None and not isinstance(p["ca_bundle"], str):
        raise ValueError("ca")
    ZoneInfo(p["default_timezone"])
    if not isinstance(p["calendars"], list) or not 1 <= len(p["calendars"]) <= 100:
        raise ValueError("calendars")
    for c in p["calendars"]:
        if set(c) != {"id", "href", "name", "writable"}:
            raise ValueError("calendar")
        if any(
            not isinstance(c[k], str) or len(c[k]) > 4096
            for k in ("id", "href", "name")
        ):
            raise ValueError("calendar")
        if not c["id"] or not c["href"] or type(c["writable"]) is not bool:
            raise ValueError("calendar")
    p["calendars"] = tuple(CalendarRef(**c) for c in p["calendars"])
    if len({c.id for c in p["calendars"]}) != len(p["calendars"]):
        raise ValueError("duplicate")
    return ConnectionProfile(**p)


class CredentialStore:
    def __init__(
        self, collection_factory=None, *, provider="system", expected_executable=None
    ):
        if provider not in KEYRING_ALIASES:
            raise SafeError(ErrorCode.INVALID_INPUT)
        self.collection_factory = collection_factory
        self.provider = provider
        self.expected_executable = expected_executable

    def check_available(self):
        """Check lock state and encrypted IPC without reading any stored item."""
        with self._collection():
            pass

    @contextmanager
    def _collection(self):
        connection = None
        try:
            if self.collection_factory:
                collection = self.collection_factory()
            else:
                connection = _BoundedConnection(_connect())
                # Resolve once and bind the complete operation to that owner.
                # GetNameOwner never starts a daemon or prompts for unlocking.
                owner = _keyring_owner(connection, self.provider)
                if (
                    self.expected_executable is not None
                    and _owner_executable(connection, owner) != self.expected_executable
                ):
                    raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)
                connection.destination = owner
                # Unlike get_default_collection, this never creates a collection.
                collection = get_collection_by_alias(connection, "default")
            if collection.is_locked():
                raise SafeError(ErrorCode.CREDENTIALS_LOCKED)
            if not self.collection_factory:
                # SecretStorage can negotiate a plaintext IPC fallback. Refuse
                # it before either sending or retrieving a credential bundle.
                collection.session = open_session(connection)
            if not collection.session.encrypted:
                raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)
            yield collection
        except SafeError:
            raise
        except LockedException:
            raise SafeError(ErrorCode.CREDENTIALS_LOCKED) from None
        except Exception:
            raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE) from None
        finally:
            if connection is not None:
                connection.close()

    def _attributes(self, name):
        return {"application": APPLICATION, "profile": validate_profile_name(name)}

    def load(self, profile_name: str) -> ConnectionProfile:
        attributes = self._attributes(profile_name)
        with self._collection() as collection:
            items = list(collection.search_items(attributes))
            if not items:
                raise SafeError(ErrorCode.CREDENTIALS_MISSING)
            if len(items) != 1 or items[0].get_attributes() != attributes:
                raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)
            if items[0].is_locked():
                raise SafeError(ErrorCode.CREDENTIALS_LOCKED)
            return _decode(items[0].get_secret())

    def save(
        self,
        profile_name: str,
        profile: ConnectionProfile,
        *,
        verified_encryption: bool,
        allow_unverified_storage: bool = False,
    ) -> None:
        attributes = self._attributes(profile_name)
        if verified_encryption is not True and allow_unverified_storage is not True:
            raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)
        try:
            data = json.dumps(
                {"version": 1, "profile": asdict(profile)}, ensure_ascii=False
            ).encode()
            _decode(data)
        except Exception:
            raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE) from None
        with self._collection() as collection:
            collection.create_item(
                "Hermes calendar profile",
                attributes,
                data,
                replace=True,
                content_type="application/json",
            )

    def forget(self, profile_name: str) -> None:
        attributes = self._attributes(profile_name)
        with self._collection() as collection:
            for item in collection.search_items(attributes):
                if item.get_attributes() == attributes:
                    item.delete()
