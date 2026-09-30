"""Small terminal helpers for trusted local setup; no persisted input."""

import os
import re
import ssl
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from .transport import endpoint
from .types import ErrorCode, SafeError


def display(value):
    """Keep Unicode names readable without allowing terminal/bidi control codes."""
    return "".join(
        f"\\u{ord(c):04x}" if unicodedata.category(c).startswith("C") else c
        for c in str(value)
    )


def menu(prompt, options, default=1):
    for number, label in enumerate(options, 1):
        print(f"  {number}. {label}")
    while True:
        answer = input(f"{prompt} [{default}; q to cancel]: ").strip()
        if answer.casefold() in {"q", "quit", "cancel"}:
            raise KeyboardInterrupt
        if not answer:
            return default
        if answer.isascii() and answer.isdecimal() and len(answer) <= 3:
            number = int(answer)
            if 1 <= number <= len(options):
                return number
        print(
            f"Choose a number from 1 to {len(options)}, or press Enter for {default}."
        )


def valid_timezone(value):
    if not isinstance(value, str) or len(value) > 200:
        return False
    try:
        ZoneInfo(value)
        return True
    except (ValueError, KeyError):
        return False


def detect_timezone(
    environment=None,
    localtime=Path("/etc/localtime"),
    timezone=Path("/etc/timezone"),
    zone_root=Path("/usr/share/zoneinfo"),
):
    environment = os.environ if environment is None else environment
    hint = environment.get("TZ", "").removeprefix(":")
    if valid_timezone(hint):
        return hint
    try:
        key = localtime.resolve(strict=True).relative_to(zone_root.resolve()).as_posix()
        if valid_timezone(key):
            return key
    except (OSError, ValueError):
        pass
    try:
        with timezone.open("r", encoding="utf-8") as source:
            key = source.read(201).strip()
        if valid_timezone(key):
            return key
    except (OSError, UnicodeError):
        pass
    return None


def choose_timezone():
    zones = sorted(
        z for z in available_timezones() if not z.startswith(("posix/", "right/"))
    )
    while True:
        query = input(
            "Search for your city (for example Berlin or New York; q to cancel): "
        ).strip()
        if query.casefold() == "q":
            raise KeyboardInterrupt
        if not query:
            print("Enter a city name, or UTC if that is the time you want.")
            continue
        normalized = query.casefold().replace("_", " ")
        matches = [z for z in zones if normalized in z.casefold().replace("_", " ")]
        if not matches:
            print("No match. Try a nearby major city in the same timezone.")
            continue
        if len(matches) > 30:
            print("Many matches. Enter a more specific city or region.")
            continue
        index = menu("Choose your timezone", [z.replace("_", " ") for z in matches])
        return matches[index - 1]


def normalize_address(value):
    value = value.strip().rstrip("/")
    if not value or len(value) > 8192:
        raise SafeError(ErrorCode.INVALID_INPUT)
    if "://" not in value:
        value = "https://" + value
    # Validate the whole pasted address before trimming known UI suffixes.
    origin, base, _ = endpoint(value)
    calendar_page = re.fullmatch(r"(.*?)/(?:index\.php/)?apps/calendar(?:/.*)?", base)
    if calendar_page:
        base = calendar_page[1] or "/"
    else:
        for suffix in ("/index.php/login", "/login"):
            if base.endswith(suffix):
                base = base.removesuffix(suffix) or "/"
                break
    if "/remote.php/" in base:
        raise SafeError(ErrorCode.INVALID_INPUT)
    return origin + base.rstrip("/")


def choose_calendars(calendars, current=()):
    for number, calendar in enumerate(calendars, 1):
        if hasattr(calendar, "name"):
            access = "read and edit" if calendar.writable else "read only"
            print(f"  {number}. {display(calendar.name)} ({access})")
    default = [
        i for i, c in enumerate(calendars, 1) if getattr(c, "id", None) in current
    ]
    if len(calendars) == 1:
        default = [1]
    suffix = f" [{','.join(map(str, default))}]" if default else ""
    while True:
        answer = input(
            f"Allow which calendars? Numbers or ranges, e.g. 1,3-4{suffix}; q to cancel: "
        ).strip()
        if answer.casefold() == "q":
            raise KeyboardInterrupt
        try:
            numbers = []
            if not answer and default:
                numbers = default
            elif len(answer) <= 1000:
                for piece in answer.split(","):
                    match = re.fullmatch(
                        r"\s*([0-9]{1,3})(?:\s*-\s*([0-9]{1,3}))?\s*", piece
                    )
                    if not match:
                        raise ValueError
                    first = int(match[1])
                    last = int(match[2] or match[1])
                    if not 1 <= first <= last <= len(calendars):
                        raise ValueError
                    numbers.extend(range(first, last + 1))
            if not numbers or len(set(numbers)) != len(numbers):
                raise ValueError
            return tuple(calendars[i - 1] for i in numbers)
        except ValueError:
            print(
                "Choose at least one listed calendar. Use numbers or ranges without duplicates."
            )


def validate_ca(value):
    if not value:
        return None
    try:
        path = Path(value).expanduser().resolve(strict=True)
        ssl.create_default_context(cafile=str(path))
        return str(path)
    except (OSError, ValueError):
        raise SafeError(ErrorCode.INVALID_INPUT) from None
