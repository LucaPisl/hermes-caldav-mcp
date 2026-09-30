import json
import sys

import pytest

from hermes_caldav_mcp import hermes_integration as integration
from hermes_caldav_mcp.wallets import WalletChoice


@pytest.fixture
def hermes_cli(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        json.dumps(
            {
                "model": {"default": "keep-me"},
                "mcp_servers": {"other": {"command": "keep-other"}},
            }
        )
    )
    script = tmp_path / "hermes"
    script.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """import json, pathlib, sys
root = pathlib.Path(__file__).parent
path = root / 'config.yaml'
args = sys.argv[1:]
if args == ['config', 'path']:
    print(path)
elif args[:2] == ['config', 'get']:
    value = json.loads(path.read_text()).get('mcp_servers')
    if value is None:
        print('Config key not set: mcp_servers', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(value))
elif args[:2] == ['config', 'set']:
    assert args[2] == 'mcp_servers.nextcloud_calendar'
    assert len(args) == 4
    if (root / 'fail-write').exists():
        print('synthetic private failure', file=sys.stderr)
        sys.exit(1)
    data = json.loads(path.read_text())
    data.setdefault('mcp_servers', {})['nextcloud_calendar'] = json.loads(args[3])
    path.write_text(json.dumps(data))
else:
    raise AssertionError(args)
"""
    )
    script.chmod(0o700)
    return script, path


def test_connect_preserves_other_settings_and_stores_no_connection_data(
    hermes_cli, profile
):
    executable, path = hermes_cli
    target = integration.detect_target(executable=str(executable))
    entry = integration.server_config(
        "default", WalletChoice("kde"), "/opt/calendar/bin/python"
    )
    integration.register(target, entry)
    data = json.loads(path.read_text())
    assert data["model"] == {"default": "keep-me"}
    assert data["mcp_servers"]["other"] == {"command": "keep-other"}
    assert data["mcp_servers"]["nextcloud_calendar"] == {
        "command": "/opt/calendar/bin/python",
        "args": [
            "-m",
            "hermes_caldav_mcp",
            "serve",
            "--profile",
            "default",
            "--keyring",
            "kde",
        ],
        "timeout": 40,
    }
    for private in (
        profile.username,
        profile.password,
        profile.base_url,
        profile.calendar_home,
    ):
        assert private not in path.read_text()
    before = path.read_bytes()
    integration.register(target, entry)
    assert path.read_bytes() == before


def test_conflicting_entry_is_not_overwritten(hermes_cli):
    executable, path = hermes_cli
    data = json.loads(path.read_text())
    data["mcp_servers"]["nextcloud_calendar"] = {"command": "different-program"}
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    target = integration.detect_target(executable=str(executable))
    with pytest.raises(integration.ConnectionSetupError):
        integration.register(
            target, integration.server_config("default", WalletChoice("kde"))
        )
    assert path.read_bytes() == before


def test_config_target_change_after_review_prevents_write(hermes_cli):
    executable, path = hermes_cli
    target = integration.detect_target(executable=str(executable))
    other = path.with_name("different.yaml")
    other.write_text("{}")
    executable.write_text(
        executable.read_text().replace(
            "root / 'config.yaml'", "root / 'different.yaml'"
        )
    )
    before = path.read_bytes()
    with pytest.raises(integration.ConnectionSetupError):
        integration.register(
            target, integration.server_config("default", WalletChoice("kde"))
        )
    assert path.read_bytes() == before
    assert other.read_text() == "{}"


def test_symlink_and_scalar_mcp_config_fail_closed(hermes_cli):
    executable, path = hermes_cli
    path.write_text('{"mcp_servers":"invalid"}')
    with pytest.raises(integration.ConnectionSetupError):
        integration.detect_target(executable=str(executable))
    other = path.with_name("other.yaml")
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(integration.ConnectionSetupError):
        integration.detect_target(executable=str(executable))


def test_write_failure_is_redacted_and_leaves_existing_config(hermes_cli):
    executable, path = hermes_cli
    target = integration.detect_target(executable=str(executable))
    path.with_name("fail-write").touch()
    before = path.read_bytes()
    with pytest.raises(integration.ConnectionSetupError) as caught:
        integration.register(
            target, integration.server_config("default", WalletChoice("kde"))
        )
    assert "synthetic private failure" not in str(caught.value)
    assert path.read_bytes() == before


def test_connect_command_registers_without_reading_wallet_secrets(
    hermes_cli, tmp_path, monkeypatch
):
    from hermes_caldav_mcp import cli
    from hermes_caldav_mcp.preferences import save_choice

    executable, path = hermes_cli
    script = executable.read_text().replace(
        "else:\n    raise AssertionError(args)",
        "elif args == ['mcp', 'test', 'nextcloud_calendar']:\n    sys.exit(0)\nelse:\n    raise AssertionError(args)",
    )
    executable.write_text(script)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    save_choice("default", WalletChoice("kde"))
    detect = integration.detect_target
    monkeypatch.setattr(
        integration,
        "detect_target",
        lambda profile=None: detect(profile, str(executable)),
    )
    monkeypatch.setattr(
        cli, "_store_for", lambda _: pytest.fail("Wallet accessed during registration")
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _: pytest.fail("Unnecessary prompt during registration"),
    )
    assert cli.main(["connect-hermes"]) == 0
    assert (
        json.loads(path.read_text())["mcp_servers"]["nextcloud_calendar"]["args"][-1]
        == "kde"
    )


def test_registration_failure_after_enrollment_reports_partial_result(
    hermes_cli, tmp_path, monkeypatch, profile, collection, capsys
):
    import getpass

    from hermes_caldav_mcp import cli, setup_helpers, wallet_protection
    from hermes_caldav_mcp.credentials import CredentialStore
    from hermes_caldav_mcp.preferences import load_choice

    executable, path = hermes_cli
    path.with_name("fail-write").touch()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    store = CredentialStore(lambda: collection)
    monkeypatch.setattr(cli, "select_wallet", lambda _: WalletChoice("kde"))
    monkeypatch.setattr(cli, "_store_for", lambda _: store)
    monkeypatch.setattr(setup_helpers, "detect_timezone", lambda: "Europe/Berlin")
    monkeypatch.setattr(wallet_protection, "ensure_storage", lambda _: True)
    monkeypatch.setattr(
        wallet_protection,
        "inspect_storage",
        lambda _: wallet_protection.StorageStatus("unknown"),
    )
    detect = integration.detect_target
    monkeypatch.setattr(
        integration,
        "detect_target",
        lambda profile=None: detect(profile, str(executable)),
    )
    answers = iter([profile.base_url, profile.username, "", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda _: profile.password)

    async def discover(_):
        return list(profile.calendars)

    monkeypatch.setattr(cli, "_discover", discover)
    assert cli.main(["setup"]) == 1
    assert store.load("default") == profile
    assert load_choice("default") == WalletChoice("kde")
    output = capsys.readouterr()
    assert "enrolled wallet profile remains available" in output.out
    assert "connect-hermes" in output.out
    assert profile.password not in str(output)
    assert "synthetic private failure" not in str(output)
    assert "nextcloud_calendar" not in json.loads(path.read_text())["mcp_servers"]
