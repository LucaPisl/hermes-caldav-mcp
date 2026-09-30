# Setup and everyday use

## 1. Install and start the wizard

Use a terminal in your logged-in Linux desktop session. You need Python 3.11 or newer, `uv`, a desktop wallet and a working HTTPS Nextcloud site. Install missing software through your distribution's software manager. Put the repository where you intend to keep it: Hermes will use its installed interpreter there.

From the repository directory:

```sh
uv sync --frozen --no-dev
.venv/bin/hermes-caldav-mcp setup
```

`uv sync` installs the exact locked runtime dependencies. Setup automatically detects local settings and asks only for missing information. Enter or paste your normal Nextcloud web address; a bare hostname is upgraded to HTTPS, and a Calendar-page address is recognized. Include any installation folder such as `/nextcloud`. Use the direct address with a valid certificate; HTTP, redirects, passwords in URLs, public-share URLs, query parameters and DAV resource URLs are not accepted.

Use `?` at the address prompt for the walkthrough below. Use `?` at the login prompt for help finding your login name. Press Enter to accept a displayed default. `q` at ordinary prompts or Ctrl+C cancels. The hidden password prompt supports Ctrl+C; characters do not appear while typing or pasting. Setup refuses a password prompt that would echo input.

No events are read or modified during setup. The connection check only discovers calendars; the final Hermes check only discovers MCP tools. Nothing is saved until you approve the review screen. Rerunning setup can reuse the saved account and app password, replace its app password, change its account, or adjust calendars and timezone. Credentials are not displayed.

## 2. Prepare a Nextcloud account for Hermes

A dedicated non-admin account limits the damage if its app password is exposed. An app password for your main account can grant access to much more than calendars. The MCP's local calendar selection restricts this MCP, not other software using the same password.

1. **Create an account.** As a Nextcloud administrator, open the avatar menu, choose **Accounts** (**Users** on older versions), then **New account**. Choose a login such as `hermes` and a strong password. Do not add it to the `admin` group or give it group-admin rights. If you cannot create accounts, ask the server administrator. Existing restricted accounts can also be used. [Nextcloud user management](https://docs.nextcloud.com/server/latest/admin_manual/configuration_user/user_configuration.html).
2. **Strongly recommended: enable 2FA.** Sign in to that account. Open **Personal settings > Security**, enable an offered two-factor method and store its recovery codes somewhere safe. If no method is offered, ask the administrator to enable a 2FA provider. Do not give the wizard your 2FA secret or recovery codes. [Nextcloud 2FA guide](https://docs.nextcloud.com/server/latest/user_manual/en/user_2fa.html).
3. **Share the wanted calendars.** Sign in to your main account and open **Calendar**. Beside a wanted calendar, open its sharing controls (share icon, or its edit/sharing menu, depending on Calendar version). Add the Hermes account's login name and allow editing if you want Hermes to create and edit events. Repeat only for the intended calendars. Use a share to that user, not a public link or delegation of all calendars. Check the shared calendars are visible after signing back in as Hermes. Read-only shares remain read-only in the MCP. Nextcloud's editing permission also permits deletion, although the MCP provides no delete tool. [Calendar sharing guide](https://docs.nextcloud.com/server/latest/user_manual/en/groupware/calendar.html#sharing-calendars).
4. **Create an app password in the Hermes account.** Open **Personal settings > Security > Devices & sessions**. Enter an app name such as **Hermes calendar** and choose **Create new app password**. Use the displayed login name and app password in the wizard. The generated password is shown only once. If you lose it, revoke it and create another. Revoke it from the same page when removing access. [Nextcloud device-password guide](https://docs.nextcloud.com/server/latest/user_manual/en/session_management.html).

2FA protects interactive login. App passwords work without asking for the second factor, so protecting the generated password remains essential. Browser passwords, app passwords and wallet passwords are different: only the **Nextcloud app password** goes into the hidden terminal prompt. Wallet passwords belong in the native wallet manager.

Menu labels vary by Nextcloud version and server configuration. The wizard repeats the relevant directions alongside its prompts and supplies documentation links when you request help.

## 3. Wallet selection and protection

Setup detects GNOME Keyring, KWallet and a distinct running Secret Service provider. With multiple providers, it offers a numbered choice and recommends the one appropriate to your desktop. A previous profile keeps its selected provider. The recommendation does not change your desktop-wide default or move secrets between wallets. The `wallets` command lists availability without retrieving any saved secret.

If the wallet is locked, inactive or lacks a default collection, setup explains the problem, offers to open its manager, and lets you retry without starting over. Missing or unencrypted IPC cannot be bypassed. Setup never silently uses another provider.

For protection on disk, setup uses conservative evidence:

- **Known encrypted GNOME Keyring format:** when the running GNOME provider and its selected collection can be mapped to an owned, regular wallet file, setup recognizes the versioned header. This backend writes its binary format only with a nonempty master password. This avoids an extra question in the normal supported case.
- **Known unprotected GNOME Keyring format:** setup explains that the backend saves passwords without encryption and recommends fixing it.
- **KWallet or another unverifiable case:** KWallet can have an encrypted file with an empty password. Detecting the file format does not prove protection. An ambiguous filename, missing file, unfamiliar provider/version or inaccessible metadata is reported as unverified, not protected.

These checks read only a bounded file header and provider/collection metadata. They do not retrieve unrelated secrets, test master passwords, lock the wallet, or decrypt its contents. They are evidence about a maintained local backend, not a security attestation against compromised software. They do not measure password strength or prove GPG private-key passphrase protection.

When protection is unavailable or uncertain, choose **Help me protect my wallet** (the default):

- **KDE:** open **KWallet Manager**, open the default wallet (usually `kdewallet`), then choose **File > Change Password** and set a nonempty strong password. For a new wallet, choose **File > New Wallet**, password-based encryption, and a password. Enable the wallet service and select a protected default wallet in **System Settings > KDE Wallet**. Protect a GPG wallet's private key with a passphrase instead. Install `kwalletmanager` only if the manager is missing.
- **GNOME:** open **Passwords and Keys** (Seahorse). Under **Passwords**, right-click the default keyring (usually **Login**) and choose **Change Password**. Set a nonempty strong password. For a new keyring, choose **+ > Password Keyring**, set a password, and choose **Set as default** from its menu. Install `seahorse` only if the manager is missing.
- **Another provider:** use that manager's password and Secret Service integration settings.

Return to the terminal and select **Check again**. Where automatic verification remains unavailable, **I have set a wallet password; continue** records your understandable action instead of asking you to judge encryption or type `YES`. It is an acknowledgement, not cryptographic verification, and is not accepted while the check still finds known unprotected storage.

You can deliberately choose **Continue with unprotected storage** or **Continue without verifying wallet protection**, depending on the result. These options are off by default and are clearly repeated on the final review. **An unprotected backend may save your credentials in plaintext.** There is still no credential fallback to project files, Hermes YAML, command arguments or environment variables, and encrypted IPC remains mandatory. Use this override only if you accept the stated backend risk.

## 4. Review and connect Hermes

Setup detects the system timezone locally, without contacting a location service. An existing profile's timezone is reused. If detection fails, search by a city such as **Berlin** or **New York** and choose from the results. You can change it on the review screen without looking up an IANA identifier.

The wizard shows the calendar names and their access. Select numbers or ranges, for example `1,3-4`. It does not automatically enroll every calendar. A single available calendar, or a previously selected set, is suggested; the final review explicitly confirms that selection. The review shows account, wallet, timezone, calendars, certificate choice, storage override if any, and the target Hermes configuration. Choose **Change settings** to correct any of these editable account/calendar/timezone/certificate settings before saving.

After confirmation, the complete connection bundle is saved as one item in the selected wallet. A restricted preference file at `$XDG_CONFIG_HOME/hermes-caldav-mcp/keyrings/<profile>.json` (normally under `~/.config`) contains only the provider identifier and, for an unnamed provider, its executable identity. No connection or calendar data is saved there. It is written atomically with mode `0600`.

Setup then uses the installed Hermes CLI to add only `mcp_servers.nextcloud_calendar`. Hermes's own writer preserves other settings, comments and restrictive file permissions. The entry contains the interpreter path, profile and provider arguments, and a timeout, with no app password or Nextcloud account details. Matching entries are reused; conflicting entries are not overwritten. Setup checks that the active configuration has not changed, confirms the saved entry, and tests discovery through `hermes mcp test nextcloud_calendar` without calling calendar tools.

In a running Hermes session, run `/reload-mcp`, or start a new session. A safe first prompt is:

> Use nextcloud_calendar to list my allowed calendars. Do not create or modify anything.

If Hermes is absent, unsupported, or cannot read/write its configuration, enrollment can still finish and the wizard prints a manual block. Install/fix Hermes, then reconnect without re-entering credentials:

```sh
.venv/bin/hermes-caldav-mcp connect-hermes
```

`connect-hermes` uses only the nonsecret provider preference and tool discovery. It does not read the wallet's app password. It refuses a conflicting entry. If registration or discovery fails after enrollment, the wizard reports the partial outcome; the wallet profile remains available. Inspect a conflicting entry yourself before changing it, or target a different Hermes profile. Never replace the whole `mcp_servers` section and lose other servers.

Manual fallback:

```sh
.venv/bin/hermes-caldav-mcp print-config
hermes config path
```

Add the generated `nextcloud_calendar` entry to the file reported by Hermes, preserving existing entries. Do not copy credentials into it. Keep the installed interpreter in its final location. If the provider preference could not be written after saving the wallet item, the wizard prints an explicit provider-pinned block for recovery.

Advanced options, normally unnecessary:

```sh
.venv/bin/hermes-caldav-mcp setup --profile work --hermes-profile work
.venv/bin/hermes-caldav-mcp setup --keyring kde
.venv/bin/hermes-caldav-mcp setup --timezone Europe/Berlin
.venv/bin/hermes-caldav-mcp setup --ca-bundle /path/to/trusted-ca.pem
.venv/bin/hermes-caldav-mcp setup --no-hermes
.venv/bin/hermes-caldav-mcp connect-hermes --hermes-profile work
```

`--profile` is this MCP's local wallet profile; `--hermes-profile` chooses the Hermes configuration receiving the entry. Omit both for normal setup. Use a new local profile or explicitly forget the old profile before changing wallet providers. Setup never migrates secrets. Custom CA bundles can also be selected on the review or connection-recovery screen. There is no insecure TLS switch; redirects and ambient proxies remain disabled.

Tool descriptions and field schemas include argument layouts, timezone/date rules, generic examples, revision handling and write-recovery guidance. Load the full schema with `tool_describe` before the first create or edit when Hermes uses deferred tools. This guidance does not depend on conversation memory or enrolled account details.

## Dates, edits and uncertain outcomes

For timed creation, provide title, start/end with explicit offsets, and an IANA timezone when different from the profile default. Example: `2025-07-01T10:00:00+02:00` in `Europe/Berlin`. Offset/local-time mismatches, nonexistent/ambiguous local times, naive datetimes and fractional seconds fail. For an ambiguous fall daylight-saving time, supply its UTC instant and `timezone="UTC"`; iCalendar local timestamps cannot reliably retain the fold choice. Edits retain an existing UTC event's timezone independently of the profile default.

Generating a new named-timezone definition is bounded to a ten-year event span. Longer spans can use UTC or an existing timezone definition. All-day events have no timezone, and a timezone patch for them is refused.

For all-day events use `all_day=true` with ISO dates: start `2025-07-01`, end `2025-07-02` represents one day. An empty description/location clears that text; an omitted or null patch field leaves it unchanged. Get the event's revision before editing and send it as `expected_revision`. For recurring resources, explicitly set `whole_series=true`; detached overrides are preserved.

Recurring timing changes with exceptions, individual-occurrence edits, invitation-managed edits and recurrence rule changes are unsupported. If the server ignores requested expansion, recurring listing fails explicitly. Missing/weak revisions permit reads but prevent unconditional edits. A successful write without a new ETag returns a null revision; read again before editing.

After `UNKNOWN_WRITE_OUTCOME`, inspect the calendar before attempting another write. A timed-out create may already exist. Do not ask the agent to repeat it automatically. `CONFLICT` means the revision became stale; read and reconsider the patch.

## Rotation, removal and deployment checks

Run `setup` again to replace a profile privately after successful discovery. To remove local access:

```sh
.venv/bin/hermes-caldav-mcp forget --profile default
```

Remove its Hermes configuration block and revoke the app password in Nextcloud. `forget` removes only the local item in the selected provider and its provider preference, not the server credential or any event. Locking the keyring prevents subsequent calls; an already running request may finish.

Before using important data, enroll only a disposable test calendar and perform these private checks yourself:

1. Confirm real password-protected keyring storage, enrollment and an unlocked read. Lock it and confirm new calls fail; unlock locally and confirm recovery.
2. Confirm only selected calendars appear. Read an existing timed, all-day and recurring fixture on your server.
3. Create one disposable event, read its revision, edit its title, and verify the changes in Nextcloud. Change it independently, then confirm a stale edit fails rather than overwriting it.
4. Check unrelated properties remain after a supported edit. Remove the disposable event manually in Nextcloud; this MCP has no deletion method.
5. Revoke the dedicated app password and confirm authentication fails. Re-enroll privately only if keeping the integration.

The repository's automated tests cannot prove a particular keyring backend encrypts at rest or that a particular reverse proxy/Nextcloud deployment behaves correctly. Never paste credentials or real calendar exports into an issue report.
