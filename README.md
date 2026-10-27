# Hermes CalDAV MCP

A small local MCP for selected Nextcloud calendars and task lists. Read, search, create and edit events (including repeating ones and reminders), manage tasks, and optionally delete with user confirmation, without saving credentials or event bodies in project files. Tools are flat and forgiving so small local models can use them.

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

## Tools

`serve` offers a **lite** tier by default, sized for small models:

`list_calendars`, `upcoming_events`, `list_events`, `search_events`, `get_event`, `create_event`, `update_event`, `list_tasks`, `create_task`, `complete_task`, `doctor`

`--tools full` adds `find_free_time`, `update_task` and custom repeat rules (`repeat_interval`, `repeat_days`). `--allow-delete` adds `delete_event` and `delete_task` to either tier.

Calendars are named by name, a unique prefix or `calendar_id`; ambiguous names list the candidates. Times may be local (`2026-11-03T09:30`), with an offset, or a date for all day; they are interpreted in the event's timezone or the profile default. Listings are sorted by start, show times in the event's zone with a `timezone` field, mark `recurring` events with an `occurrence`, and stop at 250 with `truncated` and a hint. An unreadable item is reported as `unreadable` rather than failing the list. Errors name the field and say what to send instead.

```json
{"title": "Team call", "start": "2026-11-02T10:00", "repeat": "weekly", "reminder_minutes": 10}
{"event_id": "<event_id>", "scope": "this", "occurrence": "<occurrence>", "location": "Room 2"}
{"event_id": "<event_id>", "scope": "series", "start": "2026-11-02T11:00"}
{"title": "Pay rent", "due": "2026-11-01", "priority": "high"}
```

Repeat presets are `daily`, `weekdays`, `weekly`, `monthly` and `yearly`, with `repeat_count` or `repeat_until`. Reminders become alarms. Changing only `start` keeps the length; changing only `timezone` keeps the clock times in the new zone. Recurrence is expanded locally, so Nextcloud need not support server-side expansion.

Serve options (add them to the `args` of the Hermes MCP entry):

| Flag | Effect |
| --- | --- |
| `--tools lite\|full` | Tool tier (default `lite`). |
| `--default-calendar NAME` | Calendar used when a write names none. |
| `--allow-delete` | Offer the delete tools. |
| `--delete-calendars "A,B"` | With `--allow-delete`: only these calendars. |
| `--max-deletes N` | Deletes per server process, 1 to 100 (default 5). |

### Deleting safely

Delete tools exist only with `--allow-delete`. Each delete re-reads the item, asks the human through MCP elicitation with a yes/no form, and refuses ("Not deleted: ...") on decline, cancel, timeout, or when the client cannot ask. Before deleting, the original is saved to `$XDG_STATE_HOME/hermes-caldav-mcp/deleted/<timestamp>-<calendar>-<uid>.ics` (folder 0700, file 0600) and a line is appended to `audit.jsonl` there. The delete uses `If-Match`, so an item changed since confirmation is kept. Whole repeating series are never deleted; one occurrence is removed by adding an exclusion. Undo with:

```sh
.venv/bin/hermes-caldav-mcp restore ~/.local/state/hermes-caldav-mcp/deleted/<file>.ics [--calendar NAME]
```

Restore creates a new item and never overwrites. The confirmation waits up to 120 seconds, so set the Hermes tool timeout for this server to at least 180 seconds. Confirmation needs a client using the initialize handshake (Hermes does); stateless clients cannot be asked and every delete is refused.

### Doctor

`.venv/bin/hermes-caldav-mcp doctor` (and the `doctor` tool) checks the wallet, profile, connection, calendar permissions and the Nextcloud trash bin, printing `[PASS]`, `[WARN]` or `[FAIL]` lines; it exits 1 if any check fails. It never prints credentials or URLs.

Calendar text is untrusted data and is never executed or followed as a link. There are no invitation, file, contact, generic HTTP or credential-management tools.

## Privacy and safety boundaries

Connection URL, username, app password and calendar selection are stored together in the keyring secret payload, with generic searchable metadata. A separate restricted local preference file contains only the selected provider and, if needed, its executable identity; it contains no connection or calendar data. Encrypted keyring IPC is required too. Missing, locked or unavailable wallets fail without switching providers. Setup checks recognized storage formats where possible and guides password setup through the native wallet manager otherwise. It does not claim universal encryption verification. An explicit, default-off override can accept unprotected storage or unavailable verification: an unprotected backend may then save the secret payload in plaintext. This is a backend storage risk, not a fallback to project files, YAML, arguments or environment variables. Encrypted IPC remains mandatory even with an override.

Use a dedicated Nextcloud integration account with only the intended calendars shared to it, plus a revocable app password. An ordinary main-account app password has broader access. Nextcloud write sharing grants deletion authority; this MCP deletes only with `--allow-delete` and per-item confirmation.

Secrets exist briefly in process memory. An unlocked keyring can be read by other same-user processes. This MCP cannot control privileged inspection, swap, crash dumps or other programs. Returned event details enter the Hermes conversation and may reach its model provider and history; the MCP does not change Hermes retention settings. [SECURITY.md](SECURITY.md) describes the boundaries.

## Limits and unsupported edits

Queries span at most 366 days. DAV responses are capped at 2 MiB; event resources at 256 KiB and 250 event components. Local times skipped or repeated by a daylight-saving change are refused; send an offset instead. Generating a new timezone definition is limited to a ten-year event span. All-day ends are exclusive. Unknown timezone names in existing events fall back to the profile timezone.

Existing exceptions, alarms and custom properties survive edits. Editing a repeating event requires `scope` (`this` with an `occurrence`, or `series`). Edits to invitation-managed events are refused.

Writes use exclusive creation or a strong ETag precondition. Conflicts require a fresh read. If a timeout leaves the outcome unknown, read the calendar before attempting another write; writes are never automatically repeated.

## Development

```sh
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv build
```

Tests use synthetic keyring data, mocked HTTPS responses and a loopback-only TLS server, including actual SDK communication over stdio and a legacy MCP handshake. A real encrypted keyring and live Nextcloud instance require the private deployment checks in the setup guide. No test contacts a live calendar.

Licensed under the [GNU Affero General Public License v3.0](LICENSE) (`AGPL-3.0-only`).
