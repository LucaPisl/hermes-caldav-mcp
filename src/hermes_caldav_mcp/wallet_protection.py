"""Conservative local storage evidence and human-readable wallet recovery.

Only the trusted setup CLI uses these functions. No passwords are requested here;
the native wallet manager handles them. Header recognition is evidence about a
known backend, not an attestation against a compromised service or filesystem.
"""

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from secretstorage.collection import get_collection_by_alias

from . import credentials
from .setup_helpers import menu
from .types import ErrorCode, SafeError

GNOME_HEADER = b"GnomeKeyring\n\r\0\n\0\0\0\0"
KDE_HEADER = b"KWALLET\n\r\0\r\n"


@dataclass(frozen=True)
class StorageStatus:
    state: str
    encrypted_format: bool = False


def inspect_file(provider, identifier, root):
    # Unusual/encoded identifiers have no trustworthy filename mapping here.
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", identifier):
        return StorageStatus("unknown")
    if provider == "gnome":
        path = root / "keyrings" / (identifier + ".keyring")
    elif provider == "kde":
        path = root / "kwalletd" / (identifier + ".kwl")
    else:
        return StorageStatus("unknown")
    try:
        # Refuse indirect directories, symlinks, devices and pipes. Never read
        # entries or the payload, even for a plaintext keyring.
        if path.parent.is_symlink() or root.is_symlink():
            return StorageStatus("unknown")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return StorageStatus("unknown")
            header = source.read(20)
        if provider == "gnome":
            if header == GNOME_HEADER:
                # This backend writes binary only with a nonempty master
                # password. Its empty-password format starts with [keyring].
                return StorageStatus("protected", True)
            if header.startswith(b"[keyring]\n"):
                return StorageStatus("unprotected")
        elif info.st_size >= 60 and len(header) >= 16 and header.startswith(KDE_HEADER):
            version = header[12:16]
            # KDE backendpersisthandler.cpp: legacy/current Blowfish (0/3)
            # with SHA1/PBKDF2 (0/2), or GPG (2) with hash marker 0.
            known_cipher = (version[2] in {0, 3} and version[3] in {0, 2}) or version[
                2:
            ] == b"\x02\x00"
            if version[0] == 0 and version[1] in {0, 1} and known_cipher:
                # An encrypted KWallet file can still have an empty password.
                return StorageStatus("unknown", True)
    except (OSError, ValueError):
        pass
    return StorageStatus("unknown")


def inspect_storage(choice):
    connection = None
    try:
        if choice.provider not in {"gnome", "kde"}:
            return StorageStatus("unknown")
        connection = credentials._BoundedConnection(credentials._connect())
        owner = credentials._keyring_owner(connection, choice.provider)
        executable = credentials._owner_executable(connection, owner)
        expected = {
            "gnome": {"gnome-keyring-daemon"},
            "kde": {"ksecretd", "kwalletd6", "kwalletd5"},
        }
        if Path(executable).name not in expected[choice.provider]:
            return StorageStatus("unknown")
        connection.destination = owner
        collection = get_collection_by_alias(connection, "default")
        if choice.provider == "gnome":
            identifier = collection.collection_path.removeprefix(
                "/org/freedesktop/secrets/collection/"
            )
        else:
            # Simple, unique KDE labels map directly to wallet filenames.
            identifier = collection.get_label()
        data_home = os.environ.get("XDG_DATA_HOME", "")
        root = (
            Path(data_home)
            if Path(data_home).is_absolute()
            else Path.home() / ".local/share"
        )
        return inspect_file(choice.provider, identifier, root)
    except Exception:
        return StorageStatus("unknown")
    finally:
        if connection is not None:
            connection.close()


def wallet_help(choice):
    if choice.provider == "kde":
        print(
            "Open KWallet Manager from your application menu. Open the default wallet (usually kdewallet).\n"
            "Choose File > Change Password, and give it a nonempty, strong password.\n"
            "For a new wallet, use File > New Wallet, choose password-based encryption and set a password.\n"
            "If using a GPG wallet, protect its private key with a passphrase.\n"
            "In System Settings > KDE Wallet, enable the wallet service and select your protected default wallet.\n"
            "Keep this terminal open, unlock the wallet there, then return here. Do not enter the wallet password here.\n"
            "If KWallet Manager is missing, install kwalletmanager with your distribution's software manager."
        )
    elif choice.provider == "gnome":
        print(
            "Open Passwords and Keys (Seahorse) from your application menu.\n"
            "Under Passwords, right-click the default keyring (usually Login) and choose Change Password.\n"
            "Set a nonempty, strong password. For a new keyring, use + > Password Keyring,\n"
            "set a password, then right-click it and choose Set as default. Unlock it and return here.\n"
            "Do not enter the keyring password here. If the manager is missing, install seahorse\n"
            "with your distribution's software manager."
        )
    else:
        print(
            "Open your password manager, enable its Secret Service integration, and unlock a\n"
            "password-protected default database. The manager's documentation explains these settings.\n"
            "Return here when ready; do not enter its master password in this terminal."
        )


def open_manager(choice):
    programs = {
        "kde": ("kwalletmanager", "kwalletmanager6", "kwalletmanager5"),
        "gnome": ("seahorse",),
    }
    for program in programs.get(choice.provider, ()):
        executable = shutil.which(program)
        if executable:
            try:
                subprocess.Popen(
                    [executable],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return True
            except OSError:
                pass
    return False


def ensure_storage(choice):
    status = inspect_storage(choice)
    while True:
        if status.state == "protected":
            print("Password-protected storage detected for this wallet.")
            return True
        if status.state == "unprotected":
            print(
                "This wallet saves passwords without encryption. Anyone who can read its file could read your app password."
            )
            override = "Continue with unprotected storage (not recommended)"
        else:
            print(
                "Encrypted wallet format detected, but its password protection cannot be checked automatically."
                if status.encrypted_format
                else "This wallet's password protection could not be checked automatically."
            )
            override = "Continue without verifying wallet protection (not recommended)"
        action = menu(
            "Wallet protection",
            ["Help me protect my wallet (recommended)", override, "Cancel setup"],
        )
        if action == 3:
            raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)
        if action == 2:
            print(
                "Storage override selected. The MCP will use this wallet; it will not save credentials in Hermes YAML or project files."
            )
            return False
        wallet_help(choice)
        open_manager(choice)
        action = menu(
            "After finishing in the wallet manager",
            ["Check again", "I have set a wallet password; continue", "Cancel setup"],
        )
        if action == 3:
            raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)
        status = inspect_storage(choice)
        if action == 2 and status.state != "unprotected":
            print(
                "Using your wallet password setup. Automatic verification may remain unavailable."
            )
            return True
