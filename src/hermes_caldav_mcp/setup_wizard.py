"""Guided enrollment, entirely local and outside the MCP tool surface."""

import asyncio
import getpass
import warnings
from dataclasses import replace
from urllib.parse import quote

from . import setup_helpers as ui
from . import wallet_protection
from .types import ConnectionProfile, ErrorCode, SafeError
from .wallets import LABELS


def account_help():
    print(
        "\nRecommended: give Hermes its own Nextcloud account.\n"
        "1. An administrator opens the avatar menu > Accounts (Users on older versions)\n"
        "   > New account. Create a user such as hermes with a strong password.\n"
        "   Do not add it to the admin group. If you cannot create users, ask your administrator.\n"
        "2. Sign in as that user. Open avatar > Personal settings > Security.\n"
        "   Strongly recommended: enable two-factor authentication and save backup codes safely.\n"
        "   If no second factor is offered, ask the administrator to enable a 2FA provider.\n"
        "3. Sign in to your main account and open Calendar. Next to each wanted calendar,\n"
        "   open its sharing menu (share icon or ... > Edit/Sharing, depending on version).\n"
        "   Share with the hermes login name and enable editing for create/edit access.\n"
        "   Share only the calendars you want Hermes to access; use a user share, not a public link.\n"
        "4. Sign back in as hermes. Open Personal settings > Security > Devices & sessions.\n"
        "   Name a new app password 'Hermes calendar' and click Create new app password.\n"
        "   Copy the shown login and app password into this wizard. The password is shown once;\n"
        "   if you lose it, revoke it and create another. You can revoke it here at any time.\n"
        "2FA protects browser login. The app password works without a second factor, so protect it.\n"
        "Nextcloud editing permission also allows deletion, though this MCP has no delete tool.\n"
        "Help: https://docs.nextcloud.com/server/latest/user_manual/en/session_management.html\n"
        "      https://docs.nextcloud.com/server/latest/user_manual/en/user_2fa.html\n"
        "      https://docs.nextcloud.com/server/latest/user_manual/en/groupware/calendar.html\n"
        "      https://docs.nextcloud.com/server/latest/admin_manual/configuration_user/user_configuration.html\n"
    )


def _address(default=None):
    while True:
        suffix = f" [{ui.display(default)}]" if default else ""
        value = input(
            f"Nextcloud web address{suffix} (? for account help; q to cancel): "
        ).strip()
        if value == "?":
            account_help()
            continue
        if value.casefold() == "q":
            raise KeyboardInterrupt
        try:
            return ui.normalize_address(value or default or "")
        except SafeError:
            print(
                "Paste your HTTPS Nextcloud site address, including its installation folder if any.\n"
                "A Calendar page address also works. Do not use a public share, DAV resource,\n"
                "password in the address, or an address with query parameters. Example: cloud.example.test/nextcloud"
            )


def _username(default=None):
    while True:
        suffix = f" [{ui.display(default)}]" if default else ""
        value = input(
            f"Nextcloud login name{suffix} (? for help; q to cancel): "
        ).strip()
        if value == "?":
            print(
                "Use the login name shown with the newly generated app password, not the account's display name."
            )
            continue
        if value.casefold() == "q":
            raise KeyboardInterrupt
        value = value or default or ""
        if (
            value
            and len(value) <= 8192
            and ":" not in value
            and not any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            return value
        print("Enter a login name without control characters or a colon.")


def _password():
    print(
        "In this Nextcloud account: Personal settings > Security > Devices & sessions >\n"
        "Create new app password. Use the app password, not your browser login password.\n"
        "Paste it below; nothing will appear while you type. Ctrl+C cancels."
    )
    while True:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                password = getpass.getpass("Nextcloud app password (hidden): ")
            except getpass.GetPassWarning:
                print(
                    "A hidden password prompt is unavailable. Run setup in a normal interactive terminal."
                )
                raise SafeError(ErrorCode.INVALID_INPUT) from None
        if (
            password
            and len(password) <= 8192
            and not any(ord(c) < 32 or ord(c) == 127 for c in password)
        ):
            return password
        print(
            "Paste a nonempty app password without line breaks, or press Ctrl+C to cancel."
        )


def _account(zone, ca, previous=None, password_only=False):
    url = (
        previous.base_url
        if password_only
        else _address(previous.base_url if previous else None)
    )
    user = (
        previous.username
        if password_only
        else _username(previous.username if previous else None)
    )
    password = _password()
    # normalization has already validated the URL and preserved the subpath.
    from .transport import endpoint

    origin, base, _ = endpoint(url)
    home = base.rstrip("/") + "/remote.php/dav/calendars/" + quote(user, safe="") + "/"
    return ConnectionProfile(
        origin + base.rstrip("/"), user, password, home, (), zone, ca
    )


def _discover(profile, discover):
    while True:
        print(
            "Checking the connection and finding calendars (no events will be read or changed)..."
        )
        try:
            calendars = asyncio.run(discover(profile))
            if calendars:
                return profile, calendars
            print(
                "No calendars are available. Share a calendar with this account, then check again."
            )
        except (SafeError, TimeoutError) as caught:
            error = (
                caught
                if isinstance(caught, SafeError)
                else SafeError(ErrorCode.NETWORK_ERROR)
            )
            print(f"{error.code.value}: {error}")
            if error.code == ErrorCode.AUTH_FAILED:
                print(
                    "Check the login name shown with your app password. Use a new app password for this account.\n"
                    "Nextcloud may delay repeated failed logins; wait before retrying."
                )
            elif error.code in {
                ErrorCode.NETWORK_ERROR,
                ErrorCode.BAD_RESPONSE,
                ErrorCode.NOT_FOUND,
            }:
                print(
                    "Check the direct HTTPS site address and its installation folder. Open it in a browser\n"
                    "to check its certificate. Redirects and invalid certificates are not accepted.\n"
                    "For a private certificate authority, choose the certificate-bundle option below."
                )
            elif error.code == ErrorCode.PERMISSION_DENIED:
                print(
                    "Share the wanted calendars with this Nextcloud account and grant the required access."
                )
        action = ui.menu(
            "Connection recovery",
            [
                "Retry after fixing settings in Nextcloud",
                "Change account/address or app password",
                "Cancel setup",
                "Use a private certificate-authority bundle",
            ],
        )
        if action == 3:
            raise SafeError(ErrorCode.INVALID_INPUT)
        if action == 2:
            profile = _account(profile.default_timezone, profile.ca_bundle, profile)
        elif action == 4:
            profile = replace(profile, ca_bundle=_ca_prompt())


def _ca_prompt():
    while True:
        value = input(
            "Path to your trusted CA certificate bundle (Enter for system certificates; q to cancel): "
        ).strip()
        if value.casefold() == "q":
            raise KeyboardInterrupt
        try:
            return ui.validate_ca(value)
        except SafeError:
            print(
                "That file is missing or is not a valid certificate bundle. Ask your server administrator for the CA certificates."
            )


def prepare_profile(
    name, choice, store, discover, *, timezone=None, ca_bundle=None, hermes_target=None
):
    print(
        "\nNextcloud calendar setup\n"
        "Credentials stay in your selected desktop wallet. Returned calendar details can enter\n"
        "Hermes conversations and reach its model provider; check Hermes's privacy settings.\n"
        "Use a dedicated non-admin Nextcloud account with 2FA, and share only wanted calendars.\n"
        "Type ? at the address prompt for step-by-step account, sharing and app-password help.\n"
    )
    verified = wallet_protection.ensure_storage(choice)
    try:
        previous = store.load(name)
    except SafeError as error:
        if error.code != ErrorCode.CREDENTIALS_MISSING:
            raise
        previous = None
    zone = (
        timezone
        or (previous.default_timezone if previous else None)
        or ui.detect_timezone()
    )
    if zone is not None and not ui.valid_timezone(zone):
        raise SafeError(ErrorCode.INVALID_INPUT)
    if zone:
        print(
            f"Detected/default timezone: {ui.display(zone.replace('_', ' '))}. You can change it on the review screen."
        )
    else:
        print(
            "Your computer's timezone could not be identified. Find yours by city name."
        )
        zone = ui.choose_timezone()
    ca = ui.validate_ca(ca_bundle)
    if previous:
        print(
            f"Existing account: {ui.display(previous.username)} at {ui.display(previous.base_url)}"
        )
        action = ui.menu(
            "Account settings",
            [
                "Use saved account and app password",
                "Replace app password",
                "Change account/address",
            ],
        )
        if action == 1:
            profile = replace(
                previous,
                default_timezone=timezone or previous.default_timezone,
                ca_bundle=ca if ca_bundle is not None else previous.ca_bundle,
            )
        else:
            profile = _account(
                timezone or previous.default_timezone,
                ca if ca_bundle is not None else previous.ca_bundle,
                previous,
                password_only=action == 2,
            )
    else:
        print(
            "Nextcloud address: paste the address you use in your browser.\n"
            "Need an account? Avatar > Accounts/Users > New account (administrator only).\n"
            "Enable 2FA in that account's Personal settings > Security. From your main account,\n"
            "open Calendar > the calendar's sharing menu and share with that login, with editing enabled."
        )
        profile = _account(zone, ca)
    profile, calendars = _discover(profile, discover)
    chosen = ui.choose_calendars(
        calendars, tuple(c.id for c in previous.calendars) if previous else ()
    )
    while True:
        print(
            f"\nReview\n  Account: {ui.display(profile.username)} at {ui.display(profile.base_url)}\n"
            f"  Wallet: {LABELS[choice.provider]}\n  Timezone: {ui.display(profile.default_timezone)}\n"
            f"  Calendars: {', '.join(ui.display(c.name) for c in chosen)}\n"
            f"  Certificates: {'custom trusted bundle' if profile.ca_bundle else 'system certificates'}\n"
            f"  Storage: {'protected format detected or wallet password set by you' if verified else 'protection override selected; storage may be unprotected'}"
        )
        print(
            f"  Hermes configuration: {ui.display(hermes_target.path)}"
            if hermes_target
            else "  Hermes: manual configuration block will be printed"
        )
        action = ui.menu(
            "Finish setup",
            [
                "Save and connect to Hermes"
                if hermes_target
                else "Save and show Hermes configuration",
                "Change settings",
                "Cancel without saving",
            ],
        )
        if action == 3:
            raise SafeError(ErrorCode.INVALID_INPUT)
        if action == 1:
            if (
                verified
                and wallet_protection.inspect_storage(choice).state == "unprotected"
            ):
                # A known downgrade since the earlier check requires fresh consent.
                verified = wallet_protection.ensure_storage(choice)
            return replace(profile, calendars=chosen), verified
        change = ui.menu(
            "Change",
            [
                "Timezone (search by city)",
                "Allowed calendars",
                "Account/address or app password",
                "Certificate-authority bundle",
                "Back to review",
            ],
        )
        if change == 1:
            profile = replace(profile, default_timezone=ui.choose_timezone())
        elif change == 2:
            chosen = ui.choose_calendars(calendars, tuple(c.id for c in chosen))
        elif change in {3, 4}:
            profile = (
                _account(profile.default_timezone, profile.ca_bundle, profile)
                if change == 3
                else replace(profile, ca_bundle=_ca_prompt())
            )
            profile, calendars = _discover(profile, discover)
            chosen = ui.choose_calendars(calendars, tuple(c.id for c in chosen))
