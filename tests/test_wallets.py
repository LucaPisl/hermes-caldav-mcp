import getpass
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from jeepney import HeaderFields
from keyring_fakes import KeyringBus

from hermes_caldav_mcp import (
    cli,
    credentials,
    hermes_integration,
    setup_helpers,
    wallet_protection,
)
from hermes_caldav_mcp.types import SafeError


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setattr(wallet_protection, "ensure_storage", lambda _: True)
    monkeypatch.setattr(
        wallet_protection,
        "inspect_storage",
        lambda _: wallet_protection.StorageStatus("unknown"),
    )
    monkeypatch.setattr(setup_helpers, "detect_timezone", lambda: "Europe/Berlin")
    monkeypatch.setattr(
        hermes_integration, "detect_target", lambda *args, **kwargs: None
    )
    value = KeyringBus()
    monkeypatch.setattr(credentials, "_connect", lambda: value)
    monkeypatch.setattr("shutil.which", lambda name: None)
    return value


def test_detects_distinct_running_wallets_without_reading_or_unlocking(bus):
    from hermes_caldav_mcp.wallets import detect_wallets

    found = detect_wallets()
    assert [(w.choice.provider, w.status, w.is_default) for w in found] == [
        ("gnome", "ready", True),
        ("kde", "ready", False),
    ]
    assert not {
        "GetSecret",
        "GetSecrets",
        "Unlock",
        "CreateCollection",
        "CreateItem",
    } & {c[1] for c in bus.calls}
    assert bus.closed


@pytest.mark.parametrize(
    "state,want",
    [
        ("locked", "locked"),
        ("plain", "unencrypted communication"),
        ("missing_default", "no default wallet"),
    ],
)
def test_detection_explains_wallets_that_cannot_store_credentials(bus, state, want):
    from hermes_caldav_mcp.wallets import detect_wallets

    getattr(bus, state).add(":1.20")
    assert (
        next(w for w in detect_wallets() if w.choice.provider == "kde").status == want
    )
    assert not any(c[1] == "GetSecret" for c in bus.calls)


def test_installed_and_activatable_wallets_are_visible_without_starting_them(
    bus, monkeypatch
):
    from hermes_caldav_mcp.wallets import detect_wallets

    del bus.names["org.kde.secretservicecompat"]
    found = detect_wallets()
    assert next(w for w in found if w.choice.provider == "kde").status == "not running"
    assert not any(c[0] == "org.kde.secretservicecompat" for c in bus.calls)
    bus.activatable.remove("org.kde.secretservicecompat")
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/ksecretd" if name == "ksecretd" else None
    )
    assert (
        next(w for w in detect_wallets() if w.choice.provider == "kde").status
        == "not running"
    )


@pytest.mark.parametrize(
    "desktop,want",
    [
        ("KDE", "kde"),
        ("LXQt", "kde"),
        ("GNOME", "gnome"),
        ("GNOME:GNOME-Classic", "gnome"),
        ("X-Cinnamon", "gnome"),
        ("MATE", "gnome"),
        ("Budgie:GNOME", "gnome"),
        ("Unity", "gnome"),
        ("XFCE", "gnome"),
        ("Hyprland", "gnome"),
        ("NOTKDE", "gnome"),
        ("", "gnome"),
    ],
)
def test_desktop_recommendation_uses_tokens_and_preserves_actual_default_for_unknown(
    bus, desktop, want
):
    from hermes_caldav_mcp.wallets import detect_wallets, recommended_wallet

    assert (
        recommended_wallet(detect_wallets(), {"XDG_CURRENT_DESKTOP": desktop}) == want
    )


def test_desktop_session_is_fallback_and_missing_preferred_wallet_uses_available_one(
    bus,
):
    from hermes_caldav_mcp.wallets import detect_wallets, recommended_wallet

    found = detect_wallets()
    assert recommended_wallet(found, {"DESKTOP_SESSION": "plasma"}) == "kde"
    assert (
        recommended_wallet(
            found, {"XDG_CURRENT_DESKTOP": "GNOME", "DESKTOP_SESSION": "plasma"}
        )
        == "gnome"
    )
    assert recommended_wallet(found[:1], {"XDG_CURRENT_DESKTOP": "KDE"}) == "gnome"


def test_generic_provider_identity_is_saved_and_duplicate_default_is_not_offered(
    bus, monkeypatch
):
    from hermes_caldav_mcp.wallets import detect_wallets

    bus.names = {"org.freedesktop.secrets": ":1.30"}
    bus.activatable.clear()
    monkeypatch.setattr(
        credentials, "_owner_executable", lambda connection, owner: "/usr/bin/keepassxc"
    )
    found = detect_wallets()
    assert len(found) == 1
    assert found[0].choice.provider == "system"
    assert found[0].choice.executable == "/usr/bin/keepassxc"
    assert found[0].status == "ready"


def test_real_secretstorage_load_stays_in_selected_wallet_when_default_changes(
    profile, monkeypatch
):
    bus = KeyringBus(profile)
    bus.switch_default_on_read = True
    monkeypatch.setattr(credentials, "_connect", lambda: bus)
    store = credentials.CredentialStore(provider="gnome")
    assert store.load("default") == profile
    assert {c[0] for c in bus.calls if c[0] != "org.freedesktop.DBus"} == {":1.10"}
    assert bus.names["org.freedesktop.secrets"] == ":1.20"


def test_real_secretstorage_kde_save_uses_encrypted_bytes_on_kde_only(
    profile, monkeypatch
):
    bus = KeyringBus()
    monkeypatch.setattr(credentials, "_connect", lambda: bus)
    credentials.CredentialStore(provider="kde").save(
        "default", profile, verified_encryption=True
    )
    writes = [c for c in bus.calls if c[1] == "CreateItem"]
    assert len(writes) == 1
    assert writes[0][0] == ":1.20"
    secret = writes[0][2][1]
    assert secret[0].endswith("encrypted")
    assert profile.password.encode() not in secret[2]


def test_missing_selected_wallet_never_falls_back_to_other_available_wallet(bus):
    del bus.names["org.kde.secretservicecompat"]
    with pytest.raises(SafeError):
        credentials.CredentialStore(provider="kde").load("default")
    assert not any(c[1] in {"ReadAlias", "GetSecret"} for c in bus.calls)


@pytest.mark.parametrize("legacy_alias", ["org.kde.kwalletd6", "org.kde.kwalletd5"])
def test_older_kwallet_alias_is_detected_recommended_and_used_without_switching_provider(
    bus, monkeypatch, legacy_alias
):
    from hermes_caldav_mcp.wallets import detect_wallets, recommended_wallet

    del bus.names["org.kde.secretservicecompat"]
    bus.names[legacy_alias] = ":1.20"
    bus.names["org.freedesktop.secrets"] = ":1.20"
    found = detect_wallets()
    assert [(w.choice.provider, w.status) for w in found] == [
        ("gnome", "ready"),
        ("kde", "ready"),
    ]
    assert recommended_wallet(found, {"XDG_CURRENT_DESKTOP": "KDE"}) == "kde"
    bus.calls.clear()
    credentials.CredentialStore(provider="kde").check_available()
    assert {c[0] for c in bus.calls if c[1] == "ReadAlias"} == {":1.20"}


def test_generic_provider_identity_change_fails_before_secret_access(bus, monkeypatch):
    monkeypatch.setattr(
        credentials,
        "_owner_executable",
        lambda connection, owner: "/usr/bin/gnome-keyring-daemon",
    )
    with pytest.raises(SafeError):
        credentials.CredentialStore(
            provider="system", expected_executable="/usr/bin/keepassxc"
        ).load("default")
    assert not any(c[1] in {"ReadAlias", "GetSecret"} for c in bus.calls)


def test_provider_routing_does_not_mutate_other_connections(bus):
    from jeepney import DBusAddress, new_method_call

    message = new_method_call(
        DBusAddress(
            "/org/freedesktop/secrets",
            "org.freedesktop.secrets",
            "org.freedesktop.Secret.Service",
        ),
        "ReadAlias",
        "s",
        ("default",),
    )
    routed = credentials._BoundedConnection(bus, destination=":1.20")
    routed.send_and_get_reply(message)
    assert bus.calls[-1][0] == ":1.20"
    assert message.header.fields[HeaderFields.destination] == "org.freedesktop.secrets"
    credentials._BoundedConnection(bus).send_and_get_reply(message)
    assert bus.calls[-1][0] == "org.freedesktop.secrets"


def test_choice_persistence_contains_no_credentials_and_config_pins_provider(
    tmp_path, monkeypatch, profile
):
    from hermes_caldav_mcp.preferences import load_choice, save_choice
    from hermes_caldav_mcp.wallets import WalletChoice

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_choice("default", WalletChoice("kde"))
    assert load_choice("default") == WalletChoice("kde")
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    text = files[0].read_text()
    assert json.loads(text) == {"version": 1, "provider": "kde", "executable": None}
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert all(
        x not in text for x in (profile.password, profile.username, profile.base_url)
    )
    output = cli.render_hermes_config(
        "/opt/calendar/bin/python", "default", WalletChoice("kde")
    )
    assert (
        '["-m", "hermes_caldav_mcp", "serve", "--profile", "default", "--keyring", "kde"]'
        in output
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"version": 1, "provider": "unknown", "executable": None},
        {"version": 999, "provider": "kde", "executable": None},
        {"version": 1, "provider": "system", "executable": None},
        {"version": 1, "provider": "kde", "executable": None, "password": "synthetic"},
    ],
)
def test_invalid_preferences_fail_instead_of_using_system_wallet(
    tmp_path, monkeypatch, bad
):
    from hermes_caldav_mcp.preferences import load_choice, save_choice
    from hermes_caldav_mcp.wallets import WalletChoice

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_choice("default", WalletChoice("kde"))
    path = next(tmp_path.rglob("*.json"))
    path.write_text(json.dumps(bad))
    with pytest.raises(SafeError):
        load_choice("default")


def test_multiple_wallets_require_a_choice_and_enter_accepts_desktop_recommendation(
    bus, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr("builtins.input", lambda _: "")
    chosen = cli.select_wallet()
    assert chosen.provider == "kde"
    output = capsys.readouterr().out
    assert "GNOME Keyring" in output
    assert "KWallet" in output
    assert "recommended" in output


def test_single_ready_wallet_selects_it_without_a_choice_prompt(bus, monkeypatch):
    del bus.names["org.kde.secretservicecompat"]
    bus.activatable.discard("org.kde.secretservicecompat")
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Unexpected prompt"))
    assert cli.select_wallet().provider == "gnome"


@pytest.mark.parametrize("answer", ["0", "3", "invalid", "-1"])
def test_invalid_wallet_selection_is_refused_before_credentials(
    bus, monkeypatch, answer
):
    replies = iter([answer, "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    with pytest.raises(KeyboardInterrupt):
        cli.select_wallet()


def test_wallet_inventory_command_reads_no_secrets_and_saves_no_preferences(
    bus, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert cli.main(["wallets"]) == 0
    assert "KWallet" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
    assert not any(c[1] in {"GetSecret", "GetSecrets", "CreateItem"} for c in bus.calls)


def test_setup_remembers_selected_wallet_and_print_config_ignores_later_desktop_changes(
    bus, tmp_path, monkeypatch, profile, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    answers = iter(["", profile.base_url, profile.username, "", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _: profile.password)

    async def discover(_):
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 0
    assert any(c[0] == ":1.20" and c[1] == "CreateItem" for c in bus.calls)
    preference = next(tmp_path.rglob("*.json")).read_text()
    assert json.loads(preference)["provider"] == "kde"
    assert profile.password not in preference
    capsys.readouterr()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    assert cli.main(["print-config"]) == 0
    assert '"--keyring", "kde"' in capsys.readouterr().out


def test_failed_auth_preserves_existing_wallet_choice_and_secret(
    bus, tmp_path, monkeypatch, profile
):
    from hermes_caldav_mcp.preferences import save_choice
    from hermes_caldav_mcp.types import ErrorCode
    from hermes_caldav_mcp.wallets import WalletChoice

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_choice("default", WalletChoice("kde"))
    original = next(tmp_path.rglob("*.json")).read_bytes()
    answers = iter([profile.base_url, profile.username, "3"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _: profile.password)

    async def fail(_):
        raise SafeError(ErrorCode.AUTH_FAILED)

    monkeypatch.setattr(cli, "_discover", fail)
    assert cli.main(["setup", "--keyring", "kde"]) != 0
    assert next(tmp_path.rglob("*.json")).read_bytes() == original
    assert not any(c[1] == "CreateItem" for c in bus.calls)


def test_preferences_refuse_symlinks_oversized_files_and_nonregular_files(
    tmp_path, monkeypatch
):
    from hermes_caldav_mcp.preferences import load_choice, save_choice
    from hermes_caldav_mcp.wallets import WalletChoice

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_choice("default", WalletChoice("kde"))
    path = next(tmp_path.rglob("*.json"))
    path.write_bytes(b"x" * 8193)
    with pytest.raises(SafeError):
        load_choice("default")
    path.unlink()
    target = tmp_path / "other.json"
    target.write_text('{"version":1,"provider":"gnome","executable":null}')
    path.symlink_to(target)
    with pytest.raises(SafeError):
        load_choice("default")
    path.unlink()
    path.mkdir()
    with pytest.raises(SafeError):
        load_choice("default")


def test_runtime_uses_saved_wallet_and_forget_removes_only_that_wallet(
    bus, tmp_path, monkeypatch, profile
):
    from hermes_caldav_mcp.preferences import load_choice, save_choice
    from hermes_caldav_mcp.wallets import WalletChoice

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_choice("default", WalletChoice("kde"))
    bus.payload = json.dumps({"version": 1, "profile": asdict(profile)}).encode()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setattr(cli, "make_server", lambda service: service)

    async def run(service):
        assert service.profile_loader() == profile

    monkeypatch.setattr(cli, "serve", run)
    assert cli.main(["serve"]) == 0
    assert {c[0] for c in bus.calls if c[1] == "GetSecret"} == {":1.20"}
    bus.calls.clear()
    assert cli.main(["forget"]) == 0
    assert {c[0] for c in bus.calls if c[1] == "Delete"} == {":1.20"}
    assert load_choice("default") is None


def test_explicit_configuration_keeps_wallet_when_preference_file_is_missing(
    bus, tmp_path, monkeypatch, profile, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    bus.payload = json.dumps({"version": 1, "profile": asdict(profile)}).encode()
    monkeypatch.setattr(cli, "make_server", lambda service: service)

    async def run(service):
        assert service.profile_loader() == profile

    monkeypatch.setattr(cli, "serve", run)
    assert cli.main(["serve", "--keyring", "kde"]) == 0
    assert {c[0] for c in bus.calls if c[1] == "GetSecret"} == {":1.20"}
    assert cli.main(["print-config", "--keyring", "system"]) == 1
    assert "CREDENTIAL_STORE_UNAVAILABLE" in capsys.readouterr().err


def test_failed_preference_commit_leaves_previous_file_and_no_temporary_files(
    tmp_path, monkeypatch
):
    from hermes_caldav_mcp.preferences import load_choice, save_choice
    from hermes_caldav_mcp.wallets import WalletChoice

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_choice("default", WalletChoice("gnome"))

    def fail(*_):
        raise OSError("synthetic")

    monkeypatch.setattr("os.replace", fail)
    with pytest.raises(SafeError):
        save_choice("default", WalletChoice("kde"))
    assert load_choice("default") == WalletChoice("gnome")
    assert len(list((tmp_path / "hermes-caldav-mcp/keyrings").iterdir())) == 1


def test_cleanup_failure_keeps_preference_error_redacted(tmp_path, monkeypatch):
    from hermes_caldav_mcp.preferences import save_choice
    from hermes_caldav_mcp.wallets import WalletChoice

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def fail(*args, **kwargs):
        raise OSError("synthetic filesystem details")

    monkeypatch.setattr("os.replace", fail)
    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(SafeError) as caught:
        save_choice("default", WalletChoice("kde"))
    assert "synthetic filesystem details" not in str(caught.value)


def test_setup_reports_saved_secret_with_recovery_config_if_preference_write_fails(
    bus, tmp_path, monkeypatch, profile, capsys
):
    from hermes_caldav_mcp.types import ErrorCode

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    answers = iter([profile.base_url, profile.username, "", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _: profile.password)

    async def discover(_):
        return list(profile.calendars)

    def unavailable(*_):
        raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)

    monkeypatch.setattr(cli, "_discover", discover)
    monkeypatch.setattr(cli, "save_choice", unavailable)
    assert cli.main(["setup", "--keyring", "kde"]) == 1
    output = capsys.readouterr()
    assert "profile was stored" in output.out
    assert '"--keyring", "kde"' in output.out
    assert profile.password not in str(output)
    assert len([c for c in bus.calls if c[1] == "CreateItem"]) == 1


def test_locked_wallet_fails_before_requesting_nextcloud_credentials(
    bus, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    bus.locked.add(":1.20")
    monkeypatch.setattr(
        "builtins.input",
        lambda _: "3",
    )
    assert cli.main(["setup", "--keyring", "kde"]) == 1
    assert not any(c[1] == "CreateItem" for c in bus.calls)


def test_changing_wallet_for_existing_profile_requires_explicit_removal_or_new_profile(
    bus, tmp_path, monkeypatch
):
    from hermes_caldav_mcp.preferences import save_choice
    from hermes_caldav_mcp.wallets import WalletChoice

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    save_choice("default", WalletChoice("gnome"))
    monkeypatch.setattr(
        "builtins.input",
        lambda _: pytest.fail("Credentials requested during an unintended migration"),
    )
    assert cli.main(["setup", "--keyring", "kde"]) == 1
    assert json.loads(next(tmp_path.rglob("*.json")).read_text())["provider"] == "gnome"
    assert not any(c[1] == "CreateItem" for c in bus.calls)


def test_locked_wallet_can_be_unlocked_locally_without_restarting(bus, monkeypatch):
    bus.locked.add(":1.20")

    def manager(_):
        bus.locked.remove(":1.20")
        return True

    monkeypatch.setattr(wallet_protection, "open_manager", manager)
    answers = iter(["1", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert cli.select_wallet("kde").provider == "kde"
    assert not any(
        c[1] in {"GetSecret", "GetSecrets", "CreateItem", "Unlock"} for c in bus.calls
    )
