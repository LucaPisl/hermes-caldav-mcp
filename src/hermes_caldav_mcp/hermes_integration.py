"""Credential-free configuration through Hermes's public, preserving CLI writer."""

import json
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .credentials import validate_profile_name


class ConnectionSetupError(Exception):
    """Only fixed user-facing messages; never expose captured CLI output."""


@dataclass(frozen=True, repr=False)
class HermesTarget:
    command: tuple[str, ...]
    path: Path


def server_config(name, choice=None, python=None):
    validate_profile_name(name)
    args = ["-m", "hermes_caldav_mcp", "serve", "--profile", name]
    if choice is not None:
        args.extend(["--keyring", choice.provider])
        if choice.provider == "system":
            if choice.executable is None:
                raise ConnectionSetupError(
                    "The selected wallet's executable identity is missing."
                )
            args.extend(["--keyring-executable", choice.executable])
    return {"command": python or sys.executable, "args": args, "timeout": 40}


def _run(command, timeout=25):
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise ConnectionSetupError(
            "Hermes could not finish this command. Check its installation and try again."
        ) from None


def _config_path(command):
    result = _run([*command, "config", "path"])
    value = result.stdout.strip()
    if result.returncode or "\n" in value or not Path(value).is_absolute():
        raise ConnectionSetupError(
            "Hermes could not report a configuration path. Update Hermes or use print-config."
        )
    path = Path(value)
    try:
        if path.parent.is_symlink():
            raise ValueError
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError
    except FileNotFoundError:
        pass  # Hermes's writer can create a new config in an existing home.
    except (OSError, ValueError):
        raise ConnectionSetupError(
            "The Hermes configuration is not a regular file. Use print-config and review it manually."
        ) from None
    return path


def _servers(command):
    result = _run([*command, "config", "get", "mcp_servers", "--json"])
    if result.returncode:
        message = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
        if "Config key not set: mcp_servers" in message:
            return {}
        raise ConnectionSetupError(
            "Hermes could not read its MCP configuration. Update Hermes or use print-config."
        )
    try:
        if len(result.stdout) > 1048576:
            raise ValueError
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except ValueError:
        raise ConnectionSetupError(
            "Hermes has an unsupported MCP configuration. Use print-config and review it manually."
        ) from None


def detect_target(profile=None, executable=None):
    executable = executable or shutil.which("hermes")
    if executable is None:
        return None
    command = (executable,)
    if profile is not None:
        validate_profile_name(profile)
        command += ("--profile", profile)
    path = _config_path(command)
    _servers(command)  # Preflight compatibility before asking for credentials.
    return HermesTarget(command, path)


def register(target, entry):
    if _config_path(target.command) != target.path:
        raise ConnectionSetupError(
            "The active Hermes profile changed during setup. Reconnect after reviewing the target profile."
        )
    servers = _servers(target.command)
    if "nextcloud_calendar" in servers:
        if servers["nextcloud_calendar"] == entry:
            return
        raise ConnectionSetupError(
            "Hermes already has a different nextcloud_calendar entry. It was left untouched. Use another Hermes profile or review the existing entry before reconnecting."
        )
    result = _run(
        [
            *target.command,
            "config",
            "set",
            "mcp_servers.nextcloud_calendar",
            json.dumps(entry),
        ]
    )
    if result.returncode:
        raise ConnectionSetupError(
            "Hermes could not save the MCP configuration. The enrolled wallet profile is still available; use connect-hermes or print-config to retry."
        )
    if (
        _config_path(target.command) != target.path
        or _servers(target.command).get("nextcloud_calendar") != entry
    ):
        raise ConnectionSetupError(
            "The Hermes configuration write could not be confirmed. Review it before trying again."
        )


def test_connection(target):
    # Discovery only: Hermes initializes the MCP and reads tool schemas. It
    # does not call any calendar tool or retrieve the wallet's saved profile.
    result = _run([*target.command, "mcp", "test", "nextcloud_calendar"], timeout=45)
    return result.returncode == 0
