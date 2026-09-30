import getpass
import json

import pytest

from hermes_caldav_mcp import cli, hermes_integration, setup_helpers, wallet_protection
from hermes_caldav_mcp.types import ErrorCode, SafeError
from hermes_caldav_mcp.wallets import WalletChoice


@pytest.fixture(autouse=True)
def isolated_wallet_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(wallet_protection, "ensure_storage", lambda _: True)
    monkeypatch.setattr(setup_helpers, "detect_timezone", lambda: "Europe/Berlin")
    monkeypatch.setattr(
        hermes_integration, "detect_target", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        cli, "select_wallet", lambda requested=None: WalletChoice("kde")
    )


def test_generated_config_contains_only_program_and_profile(profile):
    output = cli.render_hermes_config("/opt/calendar/.venv/bin/python", "default")
    assert "mcp_servers:" in output
    assert json.dumps("/opt/calendar/.venv/bin/python") in output
    assert "serve" in output
    for secret in (
        profile.username,
        profile.password,
        profile.base_url,
        profile.calendar_home,
    ):
        assert secret not in output


def test_help_needs_no_keyring(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    assert "setup" in capsys.readouterr().out


@pytest.mark.parametrize(
    "failure", ["cancel", "auth", "unverified", "visible-password"]
)
def test_failed_setup_never_saves(profile, monkeypatch, failure, capsys):
    saved = []

    class Store:
        def __init__(self, **kwargs):
            pass

        def check_available(self):
            pass

        def load(self, name):
            raise SafeError(ErrorCode.CREDENTIALS_MISSING)

        def save(self, *args, **kwargs):
            saved.append(args)

    monkeypatch.setattr(cli, "CredentialStore", Store)
    if failure == "unverified":

        def reject(_):
            raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)

        monkeypatch.setattr(wallet_protection, "ensure_storage", reject)
    answers = iter([profile.base_url, profile.username, "3"])

    def prompt(text):
        if failure == "cancel":
            raise KeyboardInterrupt
        return next(answers)

    monkeypatch.setattr("builtins.input", prompt)

    def hidden(text):
        if failure == "visible-password":
            import warnings

            warnings.warn("would echo", getpass.GetPassWarning, stacklevel=2)
        return profile.password

    monkeypatch.setattr(getpass, "getpass", hidden)

    async def discover(p):
        if failure == "auth":
            raise SafeError(ErrorCode.AUTH_FAILED)
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) != 0
    assert not saved
    assert profile.password not in str(capsys.readouterr())


def test_successful_setup_saves_only_explicit_calendar_selection(profile, monkeypatch):
    saved = []

    class Store:
        def __init__(self, **kwargs):
            pass

        def check_available(self):
            pass

        def load(self, name):
            raise SafeError(ErrorCode.CREDENTIALS_MISSING)

        def save(self, *args, **kwargs):
            saved.append((args, kwargs))

    monkeypatch.setattr(cli, "CredentialStore", Store)
    answers = iter([profile.base_url, profile.username, "", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _: profile.password)

    async def discover(p):
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 0
    assert saved[0][0][1].calendars == profile.calendars
    assert saved[0][1] == {"verified_encryption": True}


def test_cancel_at_final_review_never_saves(profile, monkeypatch):
    saved = []

    class Store:
        def __init__(self, **kwargs):
            pass

        def check_available(self):
            pass

        def load(self, name):
            raise SafeError(ErrorCode.CREDENTIALS_MISSING)

        def save(self, *args, **kwargs):
            saved.append(args)

    monkeypatch.setattr(cli, "CredentialStore", Store)
    answers = iter([profile.base_url, profile.username, "", "3"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _: profile.password)

    async def discover(_):
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 1
    assert saved == []


def test_reuse_profile_needs_no_account_or_password_input(profile, monkeypatch):
    saved = []

    class Store:
        def __init__(self, **kwargs):
            pass

        def check_available(self):
            pass

        def load(self, name):
            return profile

        def save(self, *args, **kwargs):
            saved.append(args[1])

    monkeypatch.setattr(cli, "CredentialStore", Store)
    answers = iter(["", "", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(
        getpass,
        "getpass",
        lambda _: pytest.fail("Password requested while reusing settings"),
    )

    async def discover(_):
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 0
    assert saved == [profile]


def test_storage_override_is_passed_as_override_not_verification(profile, monkeypatch):
    saved = []

    class Store:
        def __init__(self, **kwargs):
            pass

        def check_available(self):
            pass

        def load(self, name):
            raise SafeError(ErrorCode.CREDENTIALS_MISSING)

        def save(self, *args, **kwargs):
            saved.append(kwargs)

    monkeypatch.setattr(cli, "CredentialStore", Store)
    monkeypatch.setattr(wallet_protection, "ensure_storage", lambda _: False)
    answers = iter([profile.base_url, profile.username, "", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _: profile.password)

    async def discover(_):
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 0
    assert saved == [{"verified_encryption": False, "allow_unverified_storage": True}]


def test_saved_timezone_is_used_when_system_detection_is_unavailable(
    profile, collection, monkeypatch
):
    from hermes_caldav_mcp.credentials import CredentialStore

    store = CredentialStore(lambda: collection)
    store.save("default", profile, verified_encryption=True)
    monkeypatch.setattr(cli, "_store_for", lambda _: store)
    monkeypatch.setattr(setup_helpers, "detect_timezone", lambda: None)
    monkeypatch.setattr(
        setup_helpers,
        "choose_timezone",
        lambda: pytest.fail("Already have a saved timezone"),
    )
    answers = iter(["", "", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(
        getpass, "getpass", lambda _: pytest.fail("Already have a saved password")
    )

    async def discover(_):
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 0
    assert store.load("default").default_timezone == "Europe/Berlin"


def test_correctable_address_and_calendar_input_can_be_fixed_in_place(
    profile, collection, monkeypatch
):
    from dataclasses import replace

    from hermes_caldav_mcp.credentials import CredentialStore

    store = CredentialStore(lambda: collection)
    monkeypatch.setattr(cli, "_store_for", lambda _: store)
    other = replace(
        profile.calendars[0],
        id="calendar-2",
        href=profile.calendar_home + "second/",
        name="Work",
    )
    answers = iter(
        [
            "http://cloud.example.test",
            profile.base_url,
            profile.username,
            "",
            "1,1",
            "2",
            "1",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _: profile.password)

    async def discover(_):
        return [profile.calendars[0], other]

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 0
    assert store.load("default").calendars == (other,)


def test_review_can_change_timezone_by_city(profile, collection, monkeypatch):
    from hermes_caldav_mcp.credentials import CredentialStore

    store = CredentialStore(lambda: collection)
    monkeypatch.setattr(cli, "_store_for", lambda _: store)
    answers = iter(
        [profile.base_url, profile.username, "", "2", "1", "new york", "", "1"]
    )
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _: profile.password)

    async def discover(_):
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 0
    assert store.load("default").default_timezone == "America/New_York"


def test_discovery_deadline_can_be_retried_without_reentering_password(
    profile, collection, monkeypatch
):
    from hermes_caldav_mcp.credentials import CredentialStore

    store = CredentialStore(lambda: collection)
    monkeypatch.setattr(cli, "_store_for", lambda _: store)
    answers = iter([profile.base_url, profile.username, "1", "", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    passwords = iter([profile.password])
    monkeypatch.setattr(getpass, "getpass", lambda _: next(passwords))
    requests = []

    async def discover(value):
        requests.append(value)
        if len(requests) == 1:
            raise TimeoutError
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 0
    assert store.load("default") == profile
    assert len(requests) == 2
