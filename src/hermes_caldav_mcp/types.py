"""Shared immutable values and deliberately non-diagnostic errors."""

from dataclasses import dataclass
from enum import Enum

MAX_QUERY_DAYS = 366
MAX_OCCURRENCES = 250
MAX_DAV_BYTES = 2097152
MAX_EVENT_BYTES = 262144
TOOL_DEADLINE = 30


class ErrorCode(str, Enum):
    CREDENTIALS_MISSING = "CREDENTIALS_MISSING"
    CREDENTIALS_LOCKED = "CREDENTIALS_LOCKED"
    CREDENTIAL_STORE_UNAVAILABLE = "CREDENTIAL_STORE_UNAVAILABLE"
    AUTH_FAILED = "AUTH_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INVALID_INPUT = "INVALID_INPUT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"
    BAD_RESPONSE = "BAD_RESPONSE"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN_WRITE_OUTCOME = "UNKNOWN_WRITE_OUTCOME"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_DELETED = "NOT_DELETED"


MESSAGES = {
    ErrorCode.CREDENTIALS_MISSING: "Enroll this profile locally before use.",
    ErrorCode.CREDENTIALS_LOCKED: "Unlock the configured keyring locally before use.",
    ErrorCode.CREDENTIAL_STORE_UNAVAILABLE: "The protected credential store is unavailable or invalid.",
    ErrorCode.AUTH_FAILED: "Nextcloud rejected authentication.",
    ErrorCode.PERMISSION_DENIED: "Calendar access was denied.",
    ErrorCode.NOT_FOUND: "The selected calendar or event was not found.",
    ErrorCode.CONFLICT: "The event changed. Read its current revision before editing.",
    ErrorCode.INVALID_INPUT: "The supplied calendar arguments are invalid.",
    ErrorCode.LIMIT_EXCEEDED: "The request exceeds a configured size or interval limit.",
    ErrorCode.UNSUPPORTED_OPERATION: "This calendar operation is not supported safely.",
    ErrorCode.BAD_RESPONSE: "Nextcloud returned an invalid or unsupported response.",
    ErrorCode.NETWORK_ERROR: "The calendar request failed. Check connectivity and TLS.",
    ErrorCode.UNKNOWN_WRITE_OUTCOME: "The write outcome is unknown. Read the calendar before attempting another write.",
    ErrorCode.INTERNAL_ERROR: "The calendar operation could not be completed.",
    ErrorCode.NOT_DELETED: "Not deleted:",
}


class SafeError(Exception):
    """A fixed message plus an optional hint built only from field names and
    the caller's own (truncated) input, never from secrets or server data."""

    def __init__(self, code: ErrorCode, hint: str | None = None):
        self.code = code
        self.hint = hint
        super().__init__(MESSAGES[code] + (f" {hint}" if hint else ""))


@dataclass(frozen=True, repr=False)
class CalendarRef:
    id: str
    href: str
    name: str
    writable: bool
    components: tuple[str, ...] = ("VEVENT",)


@dataclass(frozen=True, repr=False)
class ConnectionProfile:
    base_url: str
    username: str
    password: str
    calendar_home: str
    calendars: tuple[CalendarRef, ...]
    default_timezone: str = "UTC"
    ca_bundle: str | None = None


@dataclass(frozen=True, repr=False)
class HTTPResult:
    status: int
    data: bytes
    etag: str | None


@dataclass(frozen=True, repr=False)
class CalendarResource:
    event_id: str
    href: str
    etag: str | None
    data: bytes


@dataclass(frozen=True, repr=False)
class EventFields:
    title: str
    start: str
    end: str | None = None
    duration_minutes: int | None = None
    timezone: str | None = None
    description: str = ""
    location: str = ""
    repeat: dict | None = None
    reminder_minutes: list[int] | None = None


@dataclass(frozen=True, repr=False)
class EventPatch:
    title: str | None = None
    start: str | None = None
    end: str | None = None
    duration_minutes: int | None = None
    timezone: str | None = None
    description: str | None = None
    location: str | None = None
    repeat: dict | None = None
    reminder_minutes: list[int] | None = None


@dataclass(frozen=True, repr=False)
class TaskPatch:
    title: str | None = None
    due: str | None = None
    notes: str | None = None
    priority: str | None = None
    completed: bool | None = None
    timezone: str | None = None
