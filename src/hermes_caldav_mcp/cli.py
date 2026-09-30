"""Local enrollment is kept separate from the agent-accessible tool surface."""

import argparse
import asyncio
import json
import logging
import sys

from . import hermes_integration, setup_helpers, setup_wizard, wallet_protection
from .caldav import discover_calendars
from .credentials import CredentialStore, validate_profile_name
from .preferences import load_choice, remove_choice, save_choice
from .server import CalendarService, make_server, serve
from .transport import CalDAVClient
from .types import TOOL_DEADLINE, ErrorCode, SafeError
from .wallets import LABELS, WalletChoice, detect_wallets, recommended_wallet


def render_hermes_config(
    python_executable: str, profile_name: str, choice: WalletChoice | None = None
) -> str:
    entry = hermes_integration.server_config(profile_name, choice, python_executable)
    return (
        "mcp_servers:\n  nextcloud_calendar:\n    command: "
        + json.dumps(entry["command"])
        + "\n    args: "
        + json.dumps(entry["args"])
        + "\n    timeout: 40\n"
    )


async def _discover(profile):
    async with asyncio.timeout(TOOL_DEADLINE):
        async with CalDAVClient(profile, discovery=True) as client:
            return await discover_calendars(client)


def _wallet_inventory(show=True):
    wallets = detect_wallets()
    recommendation = recommended_wallet(wallets)
    for number, wallet in enumerate(wallets, 1):
        notes = [wallet.status]
        if wallet.choice.provider == recommendation:
            notes.append("recommended")
        if wallet.is_default:
            notes.append("current Secret Service provider")
        if show:
            print(f"{number}. {wallet.label} ({', '.join(notes)})")
    if not wallets:
        print(
            "No supported wallet was detected. Configure a password-protected Secret Service wallet locally."
        )
    return wallets, recommendation


def select_wallet(requested=None):
    try:
        wallets, recommendation = _wallet_inventory(show=False)
    except SafeError:
        print(
            "The desktop wallet service cannot be reached. Run setup in your logged-in desktop\n"
            "session. Enable KWallet on KDE, or GNOME Keyring on GNOME, then try again."
        )
        raise
    if not wallets:
        print(
            "Install/enable a desktop wallet with your distribution's software manager,\n"
            "then create a password-protected default wallet and run setup again."
        )
        raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)
    if requested is not None:
        selected = next((w for w in wallets if w.choice.provider == requested), None)
        if selected is None:
            print(
                "The requested wallet was not found. Enable it locally or choose another profile."
            )
            raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)
    elif len(wallets) == 1:
        selected = wallets[0]
    else:
        default = next(
            (
                i
                for i, w in enumerate(wallets, 1)
                if w.choice.provider == recommendation
            ),
            1,
        )
        options = [
            f"{w.label} ({w.status}{'; recommended for this desktop' if w.choice.provider == recommendation else ''})"
            for w in wallets
        ]
        selected = wallets[
            setup_helpers.menu("Choose the wallet to use", options, default) - 1
        ]
    print(f"Using {selected.label}.")
    choice = selected.choice
    while selected is None or selected.status != "ready":
        status = selected.status if selected else "unavailable"
        print(
            f"{LABELS[choice.provider]} is {status}. Setup needs an unlocked default wallet with encrypted communication."
        )
        action = setup_helpers.menu(
            "Wallet recovery",
            [
                "Show steps and open wallet manager",
                "Check again after fixing the wallet",
                "Cancel setup",
            ],
        )
        if action == 3:
            raise SafeError(
                ErrorCode.CREDENTIALS_LOCKED
                if status == "locked"
                else ErrorCode.CREDENTIAL_STORE_UNAVAILABLE
            )
        if action == 1:
            wallet_protection.wallet_help(choice)
            wallet_protection.open_manager(choice)
            answer = input(
                "Press Enter after configuring/unlocking the wallet, or q to cancel: "
            ).strip()
            if answer.casefold() == "q":
                raise KeyboardInterrupt
        selected = next((w for w in detect_wallets() if w.choice == choice), None)
    return choice


def _store_for(choice):
    if choice is None:
        # Compatibility for profiles enrolled before selection existed.
        return CredentialStore()
    return CredentialStore(
        provider=choice.provider, expected_executable=choice.executable
    )


def _configured_choice(name, requested=None, executable=None):
    if requested is None:
        if executable is not None:
            raise SafeError(ErrorCode.INVALID_INPUT)
        return load_choice(name)
    if requested != "system" or executable is not None:
        return WalletChoice(requested, executable)
    choice = load_choice(name)
    if choice is None or choice.provider != "system":
        raise SafeError(ErrorCode.CREDENTIAL_STORE_UNAVAILABLE)
    return choice


def _manual_config(name, choice):
    print(
        "Add this entry under mcp_servers in the config shown by 'hermes config path'.\n"
        "Keep other MCP entries. Then run /reload-mcp in Hermes or start a new session."
    )
    print(render_hermes_config(sys.executable, name, choice), end="")


def _finish_connection(name, choice, target):
    if target is None:
        _manual_config(name, choice)
        return
    try:
        hermes_integration.register(
            target, hermes_integration.server_config(name, choice)
        )
        print(
            "MCP configuration saved. Testing tool discovery; no calendar tools are called..."
        )
        if not hermes_integration.test_connection(target):
            raise hermes_integration.ConnectionSetupError(
                "The configuration was saved, but MCP discovery failed. Run connect-hermes again after checking the installation."
            )
        print(
            "Hermes connection checked. Calendar tools: list calendars/events, get event, create and edit.\n"
            "In a running Hermes session, run /reload-mcp, or start a new session.\n"
            "To begin: 'Use nextcloud_calendar to list my allowed calendars. Do not modify anything.'"
        )
    except hermes_integration.ConnectionSetupError:
        print(
            "Your enrolled wallet profile remains available. Retry with connect-hermes;\n"
            "no need to enter the app password again. Manual configuration is also available:"
        )
        _manual_config(name, choice)
        raise


def _setup(
    name,
    requested=None,
    *,
    timezone=None,
    ca_bundle=None,
    no_hermes=False,
    hermes_profile=None,
):
    if timezone is not None and not setup_helpers.valid_timezone(timezone):
        print(
            "Unknown timezone. Omit --timezone to detect it or choose a city in setup."
        )
        raise SafeError(ErrorCode.INVALID_INPUT)
    setup_helpers.validate_ca(ca_bundle)
    previous = load_choice(name)
    if (
        previous is not None
        and requested is not None
        and previous.provider != requested
    ):
        print(
            "Use a new profile name, or forget the previous profile before switching wallets. Existing credentials were not moved or deleted."
        )
        raise SafeError(ErrorCode.INVALID_INPUT)
    choice = select_wallet(requested or (previous.provider if previous else None))
    if previous is not None and previous != choice:
        raise SafeError(ErrorCode.INVALID_INPUT)
    store = _store_for(choice)
    store.check_available()
    target = None
    if not no_hermes:
        try:
            target = hermes_integration.detect_target(hermes_profile)
        except hermes_integration.ConnectionSetupError as error:
            print(
                f"Automatic Hermes connection is unavailable: {error}\nSetup can still save the wallet profile and show a manual configuration block."
            )
    if target is None:
        print(
            "Hermes will be connected manually using the configuration block printed at the end."
        )
    profile, verified = setup_wizard.prepare_profile(
        name,
        choice,
        store,
        _discover,
        timezone=timezone,
        ca_bundle=ca_bundle,
        hermes_target=target,
    )
    options = {"verified_encryption": verified}
    if not verified:
        options["allow_unverified_storage"] = True
    store.save(name, profile, **options)
    try:
        save_choice(name, choice)
    except SafeError:
        print(
            "The profile was stored, but the provider preference could not be saved. Use this explicit Hermes configuration to retain the selected wallet:"
        )
        print(render_hermes_config(sys.executable, name, choice), end="")
        raise
    print(
        "Profile stored in the selected wallet."
        if verified
        else "Profile stored with your wallet-protection override."
    )
    _finish_connection(name, choice, target)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Local Nextcloud calendar MCP with desktop-wallet profiles."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "wallets", help="List installed/available wallets without reading saved secrets"
    )
    for command in ("setup", "serve", "forget", "print-config", "connect-hermes"):
        help_text = {
            "setup": "Guided account enrollment and Hermes connection",
            "serve": "Start the stdio MCP; normally launched by Hermes",
            "forget": "Remove the local wallet profile; revoke its app password separately",
            "print-config": "Print credential-free configuration for manual use",
            "connect-hermes": "Retry Hermes registration without reading the saved app password",
        }[command]
        p = sub.add_parser(command, help=help_text, description=help_text)
        p.add_argument(
            "--profile",
            default="default",
            help="Local MCP wallet-profile label (default: default)",
        )
        p.add_argument(
            "--keyring",
            choices=["gnome", "kde", "system"],
            help="Explicit provider; otherwise setup asks and runtime uses the saved choice",
        )
        if command == "setup":
            p.add_argument(
                "--timezone",
                help="Override detected timezone; otherwise choose a city on the review screen",
            )
            p.add_argument(
                "--ca-bundle",
                help="Advanced: trusted private CA bundle; system certificates are the default",
            )
            p.add_argument(
                "--no-hermes",
                action="store_true",
                help="Enroll only; print configuration instead of connecting Hermes",
            )
        if command in {"setup", "connect-hermes"}:
            p.add_argument(
                "--hermes-profile",
                help="Target a named Hermes profile instead of its active profile",
            )
        if command != "setup":
            p.add_argument(
                "--keyring-executable",
                help="Expected identity for an otherwise unnamed Secret Service provider",
            )
    args = parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    try:
        if args.command == "wallets":
            wallets, _ = _wallet_inventory()
            return 0 if wallets else 1
        name = validate_profile_name(args.profile)
        if args.command == "setup":
            _setup(
                name,
                args.keyring,
                timezone=args.timezone,
                ca_bundle=args.ca_bundle,
                no_hermes=args.no_hermes,
                hermes_profile=args.hermes_profile,
            )
        elif args.command == "connect-hermes":
            choice = _configured_choice(name, args.keyring, args.keyring_executable)
            if choice is None:
                print(
                    "Run setup first, or specify the wallet explicitly for a previously enrolled profile."
                )
                raise SafeError(ErrorCode.CREDENTIALS_MISSING)
            _finish_connection(
                name, choice, hermes_integration.detect_target(args.hermes_profile)
            )
        elif args.command == "serve":
            choice = _configured_choice(name, args.keyring, args.keyring_executable)
            store = _store_for(choice)
            asyncio.run(serve(make_server(CalendarService(lambda: store.load(name)))))
        elif args.command == "forget":
            choice = _configured_choice(name, args.keyring, args.keyring_executable)
            _store_for(choice).forget(name)
            if load_choice(name) == choice:
                remove_choice(name)
            print(
                "Local profile removed. Revoke its app password in Nextcloud separately."
            )
        else:
            choice = _configured_choice(name, args.keyring, args.keyring_executable)
            print(render_hermes_config(sys.executable, name, choice), end="")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Operation cancelled.", file=sys.stderr)
        return 1
    except hermes_integration.ConnectionSetupError as error:
        print(str(error), file=sys.stderr)
        return 1
    except SafeError as error:
        print(f"{error.code.value}: {error}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "Operation failed. Check the keyring, connection and local input.",
            file=sys.stderr,
        )
        return 1
