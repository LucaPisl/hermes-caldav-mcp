import json

import pytest

from hermes_caldav_mcp.credentials import CredentialStore, validate_profile_name
from hermes_caldav_mcp.types import SafeError


def test_only_secret_payload_contains_connection_data(profile, collection, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("filesystem secret write")

    monkeypatch.setattr("builtins.open", forbidden)
    store = CredentialStore(lambda: collection)
    store.save("default", profile, verified_encryption=True)
    assert store.load("default") == profile
    assert profile.password not in repr(profile)
    item = collection.items[0]
    assert item.attributes == {"application": "hermes-caldav-mcp", "profile": "default"}
    assert profile.username not in str(item.attributes)
    assert item.secret_content_type == "application/json"


@pytest.mark.parametrize(
    "failure", ["missing", "locked", "item-locked", "unavailable", "bad", "large"]
)
def test_credential_failures_are_safe(failure, profile, collection):
    store = CredentialStore(lambda: collection)
    codes = {
        "missing": "CREDENTIALS_MISSING",
        "locked": "CREDENTIALS_LOCKED",
        "item-locked": "CREDENTIALS_LOCKED",
    }
    if failure != "missing":
        store.save("default", profile, verified_encryption=True)
    if failure == "locked":
        collection.locked = True
    elif failure == "item-locked":
        collection.items[0].locked = True
    elif failure == "unavailable":

        def unavailable():
            raise RuntimeError(profile.password)

        store = CredentialStore(unavailable)
    elif failure in ("bad", "large"):
        collection.items[0].secret = b"invalid" if failure == "bad" else b"x" * 32769
    with pytest.raises(SafeError) as caught:
        store.load("default")
    assert caught.value.code.value == codes.get(failure, "CREDENTIAL_STORE_UNAVAILABLE")
    assert profile.password not in str(caught.value)


def test_unverified_enrollment_and_invalid_names_write_nothing(profile, collection):
    with pytest.raises(SafeError):
        CredentialStore(lambda: collection).save(
            "default", profile, verified_encryption=False
        )
    assert not collection.items
    for name in ("../private", "", "x" * 65, "calendar user", "x\n"):
        with pytest.raises(SafeError):
            validate_profile_name(name)


def test_forget_only_selected_profile(profile, collection):
    store = CredentialStore(lambda: collection)
    store.save("default", profile, verified_encryption=True)
    store.save("other", profile, verified_encryption=True)
    store.forget("default")
    assert store.load("other") == profile
    with pytest.raises(SafeError):
        store.load("default")


def test_versioned_profile_rejects_unexpected_shape(profile, collection):
    store = CredentialStore(lambda: collection)
    store.save("default", profile, verified_encryption=True)
    data = json.loads(collection.items[0].secret)
    data["version"] = 999
    collection.items[0].secret = json.dumps(data).encode()
    with pytest.raises(SafeError):
        store.load("default")


def test_unencrypted_ipc_session_cannot_store_secrets(profile, collection):
    collection.session.encrypted = False
    with pytest.raises(SafeError):
        CredentialStore(lambda: collection).save(
            "default", profile, verified_encryption=True
        )
    assert not collection.items


def test_explicit_storage_override_still_requires_encrypted_ipc(profile, collection):
    store = CredentialStore(lambda: collection)
    store.save(
        "default", profile, verified_encryption=False, allow_unverified_storage=True
    )
    assert store.load("default") == profile
    collection.items.clear()
    collection.session.encrypted = False
    with pytest.raises(SafeError):
        store.save(
            "default", profile, verified_encryption=False, allow_unverified_storage=True
        )
    assert not collection.items


def test_runtime_bus_can_use_nonsecret_user_socket_without_env(monkeypatch):
    import hermes_caldav_mcp.credentials as credentials

    seen = []
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(
        credentials,
        "open_dbus_connection",
        lambda bus, auth_timeout: seen.append((bus, auth_timeout)),
    )
    credentials._connect()
    assert seen[0][0].startswith("unix:path=/run/user/")
    assert seen[0][0].endswith("/bus")
    assert seen[0][1] <= 10
