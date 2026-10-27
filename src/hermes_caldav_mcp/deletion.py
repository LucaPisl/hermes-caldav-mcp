"""Opt-in deletes: local backup and audit trail before anything is removed."""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .types import ErrorCode, SafeError

CONFIRM_TIMEOUT = 120
CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {
        "confirm": {
            "type": "boolean",
            "title": "Delete",
            "description": "Yes, delete it",
            "default": False,
        }
    },
}


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME", "")
    # Relative values are invalid per the XDG specification and are ignored.
    root = Path(base) if os.path.isabs(base) else Path.home() / ".local/state"
    return root / "hermes-caldav-mcp"


@dataclass(repr=False)
class DeletePolicy:
    calendars: tuple[str, ...] = ()
    max_deletes: int = 5
    directory: Path = field(default_factory=state_dir)
    used: int = 0

    def allows(self, calendar) -> bool:
        if not self.calendars:
            return True
        return any(
            v == calendar.id or v.casefold() == calendar.name.casefold()
            for v in self.calendars
        )


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)[:64] or "x"


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def backup(policy: DeletePolicy, calendar_name: str, uid: str, data: bytes) -> Path:
    """Write the raw iCalendar before deleting; failure aborts the delete."""
    try:
        folder = _private_dir(policy.directory / "deleted")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = folder / f"{stamp}-{_safe(calendar_name)}-{_safe(uid)}.ics"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except OSError:
        raise SafeError(
            ErrorCode.NOT_DELETED, "the local backup could not be written."
        ) from None


def audit(policy: DeletePolicy, *, required=True, **entry) -> None:
    """Append one line; only a required line (before a delete) aborts on failure."""
    try:
        _private_dir(policy.directory)
        line = json.dumps(
            {"timestamp": datetime.now(timezone.utc).isoformat(), **entry},
            ensure_ascii=True,
        )
        fd = os.open(
            policy.directory / "audit.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        with os.fdopen(fd, "a") as handle:
            handle.write(line + "\n")
    except OSError:
        if required:
            raise SafeError(
                ErrorCode.NOT_DELETED, "the audit log could not be written."
            ) from None
