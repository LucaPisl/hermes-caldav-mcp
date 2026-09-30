# Hermes CalDAV MCP

A small local MCP for selected Nextcloud calendars. Read events, create non-recurring events and edit supported fields without saving credentials or event bodies in project files.

Linux, Python 3.11+, HTTPS and a desktop Secret Service wallet are required. Password-protected storage is strongly recommended. Hermes starts the server over stdio; it opens no listening port. Runtime dependencies are the official MCP SDK, HTTPX, icalendar, SecretStorage and defusedxml, with a tested dependency lock.

## Install and connect

Copy the HTTPS clone URL from this repository's **Code** button and run `git clone` followed by that URL. Then:

```sh
cd hermes-caldav-mcp
uv sync --frozen --no-dev
.venv/bin/hermes-caldav-mcp setup
```

Run these commands from the repository in its final location, in a terminal inside your logged-in desktop session. Install `uv` through your distribution's software manager if needed. The first command installs the locked runtime dependencies; the second starts the guided wizard.

Setup detects the desktop, wallets, timezone and Hermes configuration. It recommends a wallet, offers a choice when several are present, and helps configure or unlock it when needed. It explains where to create a dedicated Nextcloud account, enable 2FA, share calendars and generate an app password. Enter only your site address, login name and hidden app password, then select calendars and review. Search a timezone by city if detection fails or you want to change it. Type `?` at the account prompts for help; `q` or Ctrl+C cancels. Correctable mistakes can be fixed in place.

After your final confirmation, setup saves the account in the selected wallet, adds its credential-free MCP entry through Hermes's configuration writer, and tests tool discovery without calling any calendar tools. Existing unrelated configuration and comments are preserved. A conflicting `nextcloud_calendar` entry is left untouched. Run `/reload-mcp` in a running Hermes session or start a new session. If Hermes cannot be connected automatically, the wizard provides a configuration block and recovery instructions. Retry registration with `.venv/bin/hermes-caldav-mcp connect-hermes`; this does not ask for or retrieve the app password.

See the [setup guide](docs/setup.md) for the account walkthrough, wallet protection choices, advanced options and troubleshooting.

| Tool | Purpose |
| --- | --- |
| `list_calendars` | Show enrolled calendars and effective write availability. |
| `list_events` | Read minimal summaries in a bounded interval. |
| `get_event` | Read structured details and the revision needed for editing. |
| `create_event` | Create a non-recurring event exclusively. |
| `update_event` | Patch title, description, location or times using a matching revision. |

There are no delete, invitation, file, contact, task, generic HTTP or credential-management tools. Calendar text is untrusted data and is never executed or followed as a link.

## Privacy and safety boundaries

Connection URL, username, app password and calendar selection are stored together in the keyring secret payload, with generic searchable metadata. A separate restricted local preference file contains only the selected provider and, if needed, its executable identity; it contains no connection or calendar data. Encrypted keyring IPC is required too. Missing, locked or unavailable wallets fail without switching providers. Setup checks recognized storage formats where possible and guides password setup through the native wallet manager otherwise. It does not claim universal encryption verification. An explicit, default-off override can accept unprotected storage or unavailable verification: an unprotected backend may then save the secret payload in plaintext. This is a backend storage risk, not a fallback to project files, YAML, arguments or environment variables. Encrypted IPC remains mandatory even with an override.

Use a dedicated Nextcloud integration account with only the intended calendars shared to it, plus a revocable app password. An ordinary main-account app password has broader access. Nextcloud write sharing grants deletion authority even though this MCP cannot delete.

Secrets exist briefly in process memory. An unlocked keyring can be read by other same-user processes. This MCP cannot control privileged inspection, swap, crash dumps or other programs. Returned event details enter the Hermes conversation and may reach its model provider and history; the MCP does not change Hermes retention settings. [SECURITY.md](SECURITY.md) describes the boundaries.

## Limits and unsupported edits

Queries require offset-bearing ISO datetimes without fractional seconds, span at most 93 days and return at most 250 occurrences. DAV responses are capped at 2 MiB; event resources at 256 KiB and 250 event components. Timed writes require explicit offsets matching an IANA timezone. Ambiguous local times require UTC input. Generating a new timezone definition is limited to a ten-year event span. All-day ends are exclusive.

Recurring reads require server-side expansion. Existing recurrence, exceptions, alarms and custom properties survive edits. Whole-series edits require `whole_series=true`; single-occurrence edits and recurrence creation/rule changes are unavailable. Timing changes with recurring exceptions and edits to invitation-managed events are refused.

Writes use exclusive creation or a strong ETag precondition. Conflicts require a fresh read. If a timeout leaves the outcome unknown, read the calendar before attempting another write; writes are never automatically repeated.

## Development

```sh
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv build
```

Tests use synthetic keyring data, mocked HTTPS responses and a loopback-only TLS server, including actual SDK communication over stdio and a legacy MCP handshake. See the [verification record](docs/verification.md). A real encrypted keyring and live Nextcloud instance require the private deployment checks in the setup guide. No test contacts a live calendar.

Licensed under the [GNU Affero General Public License v3.0](LICENSE) (`AGPL-3.0-only`).
