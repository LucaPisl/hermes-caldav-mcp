"""Read-only wallet discovery and desktop recommendations. No secret retrieval."""

import os
import shutil
from dataclasses import dataclass

from jeepney import DBus
from secretstorage.collection import get_collection_by_alias
from secretstorage.exceptions import ItemNotFoundException
from secretstorage.util import open_session

from . import credentials
from .types import ErrorCode, SafeError

LABELS = {"gnome": "GNOME Keyring", "kde": "KWallet", "system": "Other Secret Service"}
PROGRAMS = {
    "gnome": ("gnome-keyring-daemon",),
    "kde": ("ksecretd", "kwalletd6", "kwalletd5"),
}


@dataclass(frozen=True)
class WalletChoice:
    provider: str
    executable: str | None = None

    def __post_init__(self):
        if self.provider not in credentials.KEYRING_ALIASES:
            raise SafeError(ErrorCode.INVALID_INPUT)
        if self.executable is not None and (
            self.provider != "system"
            or not isinstance(self.executable, str)
            or not self.executable.startswith("/")
            or "\x00" in self.executable
            or len(self.executable) > 4096
        ):
            raise SafeError(ErrorCode.INVALID_INPUT)


@dataclass(frozen=True)
class WalletAvailability:
    choice: WalletChoice
    status: str
    is_default: bool = False

    @property
    def label(self):
        return LABELS[self.choice.provider]


def _probe(connection, owner):
    connection.destination = owner
    try:
        collection = get_collection_by_alias(connection, "default")
        if collection.is_locked():
            return "locked"
        session = open_session(connection)
        return "ready" if session.encrypted else "unencrypted communication"
    except ItemNotFoundException:
        return "no default wallet"
    except Exception:
        return "unavailable"


def detect_wallets():
    """Inventory named providers and a distinct current generic provider.

    Installed but inactive providers are shown without launching them. Running
    providers are probed only for a default collection, lock state and encrypted
    IPC. Collection labels, item lists and secret values are never displayed.
    """
    connection = None
    try:
        connection = credentials._BoundedConnection(credentials._connect())
        (names,) = credentials._bus_reply(connection, DBus().ListNames())
        (activatable,) = credentials._bus_reply(
            connection, DBus().ListActivatableNames()
        )
        names, activatable = set(names), set(activatable)
        owners = {}
        for provider, aliases in credentials.KEYRING_ALIASES.items():
            if any(name in names for name in aliases):
                try:
                    owner = credentials._keyring_owner(connection, provider)
                    owners[provider] = owner
                except Exception:
                    pass  # A provider may exit while the inventory is read.
        result = []
        for provider, programs in PROGRAMS.items():
            installed = any(
                name in activatable for name in credentials.KEYRING_ALIASES[provider]
            ) or any(shutil.which(p) for p in programs)
            if provider not in owners and not installed:
                continue
            status = (
                _probe(connection, owners[provider])
                if provider in owners
                else "not running"
            )
            result.append(
                WalletAvailability(
                    WalletChoice(provider),
                    status,
                    provider in owners and owners[provider] == owners.get("system"),
                )
            )
        if "system" in owners and owners["system"] not in {
            owners.get("gnome"),
            owners.get("kde"),
        }:
            try:
                executable = credentials._owner_executable(connection, owners["system"])
                choice = WalletChoice("system", executable)
                status = _probe(connection, owners["system"])
            except Exception:
                choice, status = WalletChoice("system"), "unavailable"
            result.append(WalletAvailability(choice, status, True))
        return result
    except Exception:
        raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE) from None
    finally:
        if connection is not None:
            connection.close()


def recommended_wallet(wallets, environment=None):
    environment = os.environ if environment is None else environment
    desktop = environment.get("XDG_CURRENT_DESKTOP") or environment.get(
        "DESKTOP_SESSION", ""
    )
    tokens = desktop.casefold().split(":")
    preferred = None
    for token in tokens:
        if token in {"kde", "plasma", "plasmawayland", "plasmax11", "lxqt"}:
            preferred = "kde"
            break
        if token in {
            "gnome",
            "gnome-classic",
            "gnome-xorg",
            "cinnamon",
            "x-cinnamon",
            "mate",
            "budgie",
            "budgie-desktop",
            "unity",
            "xfce",
            "xfce4",
        }:
            preferred = "gnome"
            break
    if any(w.choice.provider == preferred for w in wallets):
        return preferred
    default = next((w for w in wallets if w.is_default), None)
    if default:
        return default.choice.provider
    ready = next((w for w in wallets if w.status == "ready"), None)
    return ready.choice.provider if ready else None
