import pytest

from hermes_caldav_mcp import setup_helpers as helpers
from hermes_caldav_mcp.types import SafeError


def test_timezone_uses_valid_local_metadata_and_ignores_invalid_hint(tmp_path):
    zones = tmp_path / "zoneinfo"
    (zones / "Europe").mkdir(parents=True)
    (zones / "Europe/Berlin").touch()
    localtime = tmp_path / "localtime"
    localtime.symlink_to(zones / "Europe/Berlin")
    assert (
        helpers.detect_timezone(
            {"TZ": "Invalid/City"}, localtime, tmp_path / "missing", zones
        )
        == "Europe/Berlin"
    )
    assert (
        helpers.detect_timezone(
            {"TZ": "America/New_York"}, localtime, tmp_path / "missing", zones
        )
        == "America/New_York"
    )
    localtime.unlink()
    metadata = tmp_path / "timezone"
    metadata.write_text("Europe/Berlin\n")
    assert helpers.detect_timezone({}, localtime, metadata, zones) == "Europe/Berlin"
    metadata.write_text("invalid")
    assert helpers.detect_timezone({}, localtime, metadata, zones) is None


def test_city_picker_recovers_and_needs_no_iana_identifier(monkeypatch):
    replies = iter(["Nowhere", "berlin", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    assert helpers.choose_timezone() == "Europe/Berlin"


@pytest.mark.parametrize(
    "value,want",
    [
        ("cloud.example.test/nextcloud/", "https://cloud.example.test/nextcloud"),
        (
            "https://cloud.example.test/nextcloud/index.php/apps/calendar/",
            "https://cloud.example.test/nextcloud",
        ),
        (
            "https://cloud.example.test/nextcloud/apps/calendar",
            "https://cloud.example.test/nextcloud",
        ),
        (
            "https://cloud.example.test/nextcloud/index.php/apps/calendar/dayGridMonth/now",
            "https://cloud.example.test/nextcloud",
        ),
    ],
)
def test_address_accepts_browser_calendar_url_and_adds_https(value, want):
    assert helpers.normalize_address(value) == want


@pytest.mark.parametrize(
    "value",
    [
        "http://cloud.example.test",
        "https://name:secret@cloud.example.test",
        "https://cloud.example.test/#token",
        "https://cloud.example.test/../other",
        "",
        "https://cloud.example.test/remote.php/dav/calendars/user/",
    ],
)
def test_address_keeps_security_boundaries(value):
    with pytest.raises(SafeError):
        helpers.normalize_address(value)


def test_calendar_selection_ranges_retry_without_default_all(monkeypatch):
    replies = iter(["", "1,1", "4", "1-2,3"])
    monkeypatch.setattr("builtins.input", lambda _: next(replies))
    assert helpers.choose_calendars(["a", "b", "c"]) == ("a", "b", "c")


def test_single_calendar_can_be_selected_with_enter(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert helpers.choose_calendars(["a"]) == ("a",)


def test_display_escapes_terminal_and_bidi_controls():
    rendered = helpers.display("Calendar\x1b[2J\n\u202ehidden")
    assert "\x1b" not in rendered and "\n" not in rendered and "\u202e" not in rendered
    assert "Calendar" in rendered


def test_private_ca_validation_rejects_missing_file(tmp_path):
    with pytest.raises(SafeError):
        helpers.validate_ca(str(tmp_path / "missing"))
    # Real certificate validation, not just filesystem existence.
    path = tmp_path / "invalid.pem"
    path.write_text("not a certificate")
    with pytest.raises(SafeError):
        helpers.validate_ca(str(path))
    assert helpers.validate_ca(None) is None
