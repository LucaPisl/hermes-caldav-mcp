"""Only a provider identifier and optional executable identity; never credentials."""

import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from .credentials import validate_profile_name
from .types import ErrorCode, SafeError
from .wallets import WalletChoice


def _path(profile):
    validate_profile_name(profile)
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
    return root / "hermes-caldav-mcp" / "keyrings" / f"{profile}.json"


def load_choice(profile):
    path = _path(profile)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except Exception:
        raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE) from None
    try:
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise ValueError("Invalid preference file")
            data = json.loads(source.read(8193))
        if (
            set(data) != {"version", "provider", "executable"}
            or type(data["version"]) is not int
            or data["version"] != 1
        ):
            raise ValueError("Invalid preference schema")
        choice = WalletChoice(data["provider"], data["executable"])
        if choice.provider == "system" and choice.executable is None:
            raise ValueError("Unpinned provider")
        return choice
    except Exception:
        raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE) from None


def save_choice(profile, choice):
    path = _path(profile)
    temporary = None
    try:
        # Validate the whole record before touching an existing preference.
        choice = WalletChoice(choice.provider, choice.executable)
        if choice.provider == "system" and choice.executable is None:
            raise ValueError("Unpinned provider")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as target:
            temporary = target.name
            json.dump(
                {
                    "version": 1,
                    "provider": choice.provider,
                    "executable": choice.executable,
                },
                target,
            )
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except Exception:
        raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE) from None
    finally:
        if temporary is not None:
            # A cleanup error must not obscure the redacted primary failure.
            with suppress(OSError):
                Path(temporary).unlink(missing_ok=True)


def remove_choice(profile):
    try:
        _path(profile).unlink(missing_ok=True)
    except Exception:
        raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE) from None
