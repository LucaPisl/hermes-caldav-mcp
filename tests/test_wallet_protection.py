import os

import pytest

from hermes_caldav_mcp import wallet_protection as protection
from hermes_caldav_mcp.types import SafeError
from hermes_caldav_mcp.wallets import WalletChoice


@pytest.fixture(autouse=True)
def no_native_windows(monkeypatch):
    monkeypatch.setattr(protection, "open_manager", lambda _: False)


@pytest.mark.parametrize(
    "header,state",
    [
        (b"GnomeKeyring\n\r\0\n\0\0\0\0", "protected"),
        (b"[keyring]\n", "unprotected"),
        (b"unknown format", "unknown"),
        (b"GnomeKeyring\n\r\0\n\1\0\0\0", "unknown"),
    ],
)
def test_gnome_checks_exact_collection_header_only(tmp_path, header, state):
    root = tmp_path / "keyrings"
    root.mkdir()
    (root / "login.keyring").write_bytes(header + b"synthetic secret payload")
    result = protection.inspect_file("gnome", "login", tmp_path)
    assert result.state == state
    assert "synthetic" not in repr(result)


def test_other_wallet_file_does_not_prove_selected_wallet(tmp_path):
    root = tmp_path / "keyrings"
    root.mkdir()
    (root / "other.keyring").write_bytes(b"GnomeKeyring\n\r\0\n\0\0\0\0")
    assert protection.inspect_file("gnome", "login", tmp_path).state == "unknown"
    assert protection.inspect_file("gnome", "../other", tmp_path).state == "unknown"


def test_kwallet_encrypted_header_does_not_prove_nonempty_password(tmp_path):
    root = tmp_path / "kwalletd"
    root.mkdir()
    (root / "kdewallet.kwl").write_bytes(b"KWALLET\n\r\0\r\n\0\1\0\0" + b"x" * 80)
    result = protection.inspect_file("kde", "kdewallet", tmp_path)
    assert result.state == "unknown"
    assert result.encrypted_format


@pytest.mark.parametrize(
    "version,recognized",
    [
        (bytes([0, 0, 0, 0]), True),
        (bytes([0, 1, 3, 2]), True),
        (bytes([0, 1, 2, 0]), True),
        (bytes([0, 1, 1, 0]), False),
        (bytes([9, 1, 3, 2]), False),
        (bytes([0, 1, 3, 99]), False),
    ],
)
def test_kwallet_known_ciphers_are_recognized_without_claiming_password_protection(
    tmp_path, version, recognized
):
    root = tmp_path / "kwalletd"
    root.mkdir()
    (root / "kdewallet.kwl").write_bytes(b"KWALLET\n\r\0\r\n" + version + b"x" * 80)
    result = protection.inspect_file("kde", "kdewallet", tmp_path)
    assert result.state == "unknown"
    assert result.encrypted_format is recognized


def test_symlink_and_fifo_are_not_read_as_wallets(tmp_path):
    root = tmp_path / "keyrings"
    root.mkdir()
    target = tmp_path / "other"
    target.write_bytes(b"GnomeKeyring\n\r\0\n\0\0\0\0")
    path = root / "login.keyring"
    path.symlink_to(target)
    assert protection.inspect_file("gnome", "login", tmp_path).state == "unknown"
    path.unlink()
    os.mkfifo(path)
    assert protection.inspect_file("gnome", "login", tmp_path).state == "unknown"


@pytest.mark.parametrize("state", ["unknown", "unprotected"])
def test_override_requires_explicit_nondefault_choice(monkeypatch, state):
    replies = iter(["", "3"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    monkeypatch.setattr(
        protection, "inspect_storage", lambda _: protection.StorageStatus(state)
    )
    with pytest.raises(SafeError):
        protection.ensure_storage(WalletChoice("kde"))


def test_unprotected_override_is_distinct_from_verified_storage(monkeypatch):
    replies = iter(["2"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    monkeypatch.setattr(
        protection, "inspect_storage", lambda _: protection.StorageStatus("unprotected")
    )
    assert protection.ensure_storage(WalletChoice("gnome")) is False


def test_protected_storage_needs_no_confirmation(monkeypatch):
    monkeypatch.setattr(
        protection,
        "inspect_storage",
        lambda _: protection.StorageStatus("protected", True),
    )
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Unnecessary prompt"))
    assert protection.ensure_storage(WalletChoice("gnome")) is True


def test_guided_password_setup_rechecks_before_accepting(monkeypatch):
    replies = iter(["", "1"])
    states = iter(
        [
            protection.StorageStatus("unknown"),
            protection.StorageStatus("protected", True),
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    monkeypatch.setattr(protection, "inspect_storage", lambda _: next(states))
    monkeypatch.setattr(protection, "open_manager", lambda _: True)
    assert protection.ensure_storage(WalletChoice("gnome")) is True
