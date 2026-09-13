# TDeck

A small Python app for technical directors: scheduling service cues and firing Bitfocus Companion button presses at configured times.

This project started as a calendar-focused scheduler, and still includes a calendar scheduler component under the hood.

It supports:
- A **scheduler** that watches an events JSON file and executes triggers.
- A **CLI** (`cli.py`) for starting/stopping the scheduler and managing events.
- A **Web UI** (`webui.py`) for editing events and templates in a browser.
- A **DiGiCo Personal Mixes** web app that lets multiple phones mix permitted AUXes through one shared SD9 OSC connection.
- A native **Hisense / VIDAA TV service** for individual and ordered-group power, absolute volume, source selection, pairing, protocol detection, and automatic reconnect.
- A direct **Pixie Controls** web app for permission-scoped auditorium lighting, individual device faders/on-off controls, and scenes through one Pixie Plus Gateway.

## What this app does

- You define events in an `events.json`-style file (configurable via `config.json`).
- Each event has one or more **triggers**:
  - `BEFORE` (N minutes before the event)
  - `AT` (at the event time)
  - `AFTER` (N minutes after the event)
- When a trigger is due, the scheduler sends an HTTP POST to Bitfocus Companion’s HTTP API for a button press.

Internally, triggers are stored like `location/<page>/<row>/<column>/press`.

Important: store **paths**, not full URLs. The scheduler automatically prefixes Companion’s `/api/` base.

The Companion client posts to:

`http://<companion_ip>:<companion_port>/api/location/<page>/<row>/<column>/press`

## Requirements

- Windows/macOS/Linux
- **Python 3.10+** (this repo uses modern Python typing like `X | Y`)
- Bitfocus Companion running on the network (optional for testing, required for real trigger execution)

## Installation

1) Create and activate a virtual environment (recommended)

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

2) Install dependencies

```powershell
pip install -r requirements.txt
```

That’s it—there is no separate build step.

### Web asset delivery

Templates reference bundled CSS and JavaScript through `static_asset(...)`. TDeck
computes a SHA-256 identity from each file's bytes and appends it to the URL, so a
changed file always has a different address even when its timestamp and size are
unchanged. Valid content-addressed assets are served with a one-year
`public, immutable` policy; unversioned, missing, stale, or forged asset versions
must revalidate. HTML is private and revalidated, while `/api/*` responses and live
control state are never stored.

A reverse proxy must preserve the `v` query parameter, `ETag`, `Cache-Control`, and
`Vary: Accept-Encoding` headers. It must not apply its own cache to HTML or API
responses. Do not add a service worker that caches control or device-state requests.
Marked 12.0.2 and DOMPurify 3.2.6 are pinned under `static/vendor/`, including their
upstream licenses and `THIRD_PARTY_NOTICES.md`; the API Reference therefore works
without public internet access.

## Configuration

The app reads `config.json` from the repo root.

Common keys:
- `EVENTS_FILE`: which events file to use (default: `events.json`)
- `companion_ip`: IP/hostname for Companion (default: `127.0.0.1`)
- `companion_port`: Companion HTTP port (default in code: `8000`)
- `webserver_port`: Web UI port (default: `5000`)
- `poll_interval`: seconds between file-change checks (default: `1.0`)
- `debug`: enables more verbose logging/output

### User login lockout

In **Permissions → Users**, open a user and use **Access → Automatic login lockout**
to choose whether failed passwords can lock that account. Changes save automatically.
Lockout starts enabled for existing and new users and uses the configured failed-login
threshold. Turning it off prevents future automatic lockouts; administrators can still
lock or disable the account manually. Use **Unlock account** to remove an existing lock.
Changing the switch resets the failed-login counter.

### API security

With authentication enabled, TDeck APIs are no longer anonymous merely because
the caller is on the production LAN. Browser controls use the logged-in user's
group permissions plus automatic same-origin CSRF protection. Companion and
other external automation use separately scoped Bearer service tokens whose
plaintext is shown once and whose hashes and lifecycle metadata live in
`auth.db`.

Administrators manage tokens at **Config → API Tokens**. Create a separate token
for each integration, choose only the permissions and hardware targets it needs,
and copy the secret when TDeck displays it. The page shows active, expiring,
expired, and revoked credentials together with their last-used time, and supports
atomic rotation and immediate revocation. Paste a Companion token into
Companion's existing API-token field. TDeck's built-in Calendar scheduler uses
a private in-process dispatcher, so it needs no token or environment variable.
Never put plaintext tokens in `config.json`, event JSON, URLs, or logs.

The recovery CLI is a separate process. Commands that call the running Web UI,
or the discouraged standalone `cli.py start calendar` mode, remain external API
clients and require `TDECK_INTERNAL_API_TOKEN` when authentication is enabled.
Normal `python webui.py` operation does not use that variable.

The `python cli.py service-tokens create|list|rotate|revoke` commands remain as
an emergency/recovery and automation interface, but normal token management no
longer requires the CLI. See `API_REFERENCE.md` for the complete migration
sequence, temporary expiring legacy flag, v1 endpoint aliases, limits, and
scope semantics.

### Groups and testing user access

In **Permissions → Groups**, select a group and tick its page access. The tabs
below show additional settings only for enabled pages. **General** contains the
idle timeout. Turning off a page hides its tab while preserving its settings.
Enable **Routing**, then use its **Routing** tab to enable **Allow selecting
media** and optionally **Allow uploading media**. Media access requires Routing;
uploads additionally require media selection access. Existing saved grants are
preserved, including when the Routing tab is hidden. Manage the image library in
**Config → Media**, and saved routing presets in **Config → Routing presets**.
For preset access, enable **Allow using presets** in the group's Routing tab and
tick each preset that group may run. No selection means no presets; new presets
are Admin-only until assigned. Preset access does not grant general media browsing
or uploads. Grants combine across groups, and the normal output/media-input
restrictions still apply.

To test another user's experience, sign in with an account in the **Admin**
group, open **Permissions → Users → select a user**, and choose **View as user**.
The banner identifies the user being tested and provides **Return to admin**.
Controls and uploads perform real actions with that user's current permissions.
The Activity Log records the initiating administrator and the selected user,
including completion of background media jobs. No target password is required,
and the target's own sessions are unchanged.

The target must be active, unlocked, and have completed any required password
change. Account credentials cannot be changed while testing. Administrator
session revocation or loss of Admin access ends the test; changes that invalidate
the target's account also stop it. Switching identities refreshes the form
security token, so reload any other TDeck tabs before using them.

DiGiCo settings are managed from **Config → DiGiCo Mixer**. They are stored in `config.json` and therefore travel with the normal TDeck config export/import.

Hisense TVs are managed from **Config → TVs**. TDeck connects directly to each TV's VIDAA MQTT service; a separate Mosquitto broker and Companion Generic MQTT connection are not required. Normal setup only asks for a TV name, stable IP address, and the TV's MAC address. Saving automatically enables the service, while the backend handles polling, reconnects, protocol/authentication detection, and selection of installed legacy/current VIDAA support files.

TV and group order are explicit. The setup page uses a collapsible tree: each TV belongs to one group or the Ungrouped root, its group dropdown moves it between branches, and arrow buttons order siblings. Collapsed group/root rows show online totals and error counts. The visible TV name is used throughout TDeck and Companion. A slug-style internal ID is generated from that name for new TVs and retained internally so renaming a TV does not break saved Companion buttons.

Keep the TV's own MAC address for Wake-on-LAN. Newer VIDAA authentication also requires a separate, case-sensitive paired-device UUID: the Bluetooth/Wi-Fi MAC or UUID of a phone/device paired with that TV through the official VIDAA app. The paired-device UUID is not the TV MAC and appears only in the TV's contextual **Pair or repair** panel. Protocol generation is detected automatically, but TDeck cannot derive this UUID or redistribute the official app's private client key.

When TDeck or Companion successfully powers a TV off, TDeck records it as intentionally off and pauses that TV's reconnect loop. The TV page shows a normal **Off (intentional)** state and does not create an offline warning in the Activity Log. The state survives a TDeck restart; power-on, reconnect, or another control clears it so an unexpected network or authentication failure is still reported.

The TDeck Companion module exposes both group and individual targets for power, volume, source, reconnect, feedbacks, and variables.

Some newer VIDAA firmware accepts an MQTT connection but rejects pairing requests made with certificates from an older RemoteNOW/VIDAA app generation, displaying a “TV model is no longer compatible” message on the screen. That condition is not returned to TDeck over MQTT, so it cannot be inferred from the IP address or software version alone. Administrators place approved support files at `hisense_certs/vidaa_client.pem` / `.key` (legacy) and `hisense_certs/vidaa_current.pem` / `.key` (current); TDeck tries installed pairs in the appropriate order automatically. Certificate/private-key files remain local and must not be committed.

## Pixie Controls

Pixie Plus is managed from **Config → Pixie**. TDeck connects directly to one local Gateway in Disabled, Observe only, or Control enabled mode. Setup discovers physical devices and scenes, arranges devices into ordered TDeck auditoriums, keeps missing inventory records so names and permissions survive an outage, and supports automatic or administrator-overridden Dimmable/On/Off classification. Pixie credentials are retained in the ignored local `pixie_secrets.json`; the password is excluded from config export and Activity Log details.

The operator page is `/pixie`. Users with one accessible auditorium go directly to its controls; users with several see an auditorium tile chooser. Device state refreshes once per second only while an auditorium's Devices view is visible. TDeck obtains live reachability and level/on-off feedback from the authenticated Pixie Home status maps through one server-side one-second cache shared by every browser; controls continue to use the local Gateway directly. A newly commanded level is preserved while feedback catches up, rather than being overwritten by the Gateway inventory's stale setup-era value. Offline devices are shown disabled. Brief status-service interruptions retain the last result for 30 seconds and then show **Status unknown** without preventing control. Faders stream throttled changes and final values, while On/Off devices in a mixed selection react only at 0% or 100%. Scene access is configured separately.

Permissions are configured in **Permissions → Groups**. Grant the **Pixie Controls** page, then choose auditoriums, all devices or individual devices within each granted auditorium, and scenes. Multiple group grants union, but a device grant is only effective from a group that also grants its auditorium. Every Pixie read and control API enforces login, page access, auditorium/device or scene scope, and CSRF for writes. Admin remains unrestricted.

TDeck never uses the Gateway's native group-address command. A prior G3 Gateway interpreted an experimental group packet as a building-wide broadcast, so each TDeck auditorium action fans out to validated physical-device IDs only.

## Run (Web UI)

Start the Web UI server:

```powershell
python webui.py
```

It reads `webserver_port` from `config.json` and prints the URL at startup.

The Web UI also has controls to start/stop registered apps (including the calendar scheduler) from the browser.
Starting `webui.py` starts the calendar scheduler automatically. Do not also run `cli.py start calendar` for the same installation; that would create a second scheduler process and can duplicate cues.

## Media library and ATEM still players

In **Routing**, choose an output, choose **Media**, then select an existing image
or open **Upload an image**. Uploading saves the image to TDeck and displays it
on the selected output. The page returns to the output list only after confirmed
completion. If display fails, the saved image remains available for retry.
The operator pages show images and progress; player assignments, connection
status and library management live in **Config → Media**. Saved preset setup
lives in **Config → Routing presets**.

The signal path is: **TDeck library → ATEM still slot → media player → manually
configured ATEM output/feed → VideoHub input → TV**. A still slot stores an image;
a media player selects one slot. TDeck updates the image/player and routes the
VideoHub input to the chosen output. ATEM AUX/output routing stays under your
manual control; no AUX assignment is needed in TDeck.
TDeck automatically chooses a mapped player whose VideoHub input is unused by
other outputs. It can replace the image on the selected output's own exclusive
player. If every eligible player feeds another output, it refuses the request.
Different simultaneous images need different players. Images are not shared
across players automatically in this version.

### Windows setup

1. Install the [Node.js 24 LTS runtime](https://nodejs.org/en/download) on the
   TDeck server, with Node.js on the server account's PATH.
2. In the Calendar folder, using TDeck's Python environment, run:

   ```powershell
   python -m pip install -r requirements.txt
   npm ci --omit=dev
   ```

3. Restart TDeck and open **Config → Media** (`/config/atem-media`). The
   switcher address/port come from **Config → ATEM**. Uploads start disabled;
   there are no preselected players or still slots.
4. Add each player that TDeck may control, give it a useful label, and reserve
   at least two distinct still slots per player. Slot lists cannot overlap.
   Set the **VideoHub input** that already receives that media player's signal.
   Set up the ATEM output routing and cabling manually. Players, inputs and
   slot reservations must be distinct. Use displayed numbering (player 1,
   still 1, input 1). Enable media display and save. Existing players without
   a VideoHub input remain usable
   for Config testing, but cannot be selected automatically for TV display.
   Previously saved AUX assignments are ignored and removed on the next Media
   setup save; existing VideoHub input mappings continue working.
   An administrator can optionally specify the trusted server Node.js
   executable in Advanced setup if PATH discovery is unavailable.
5. In **Permissions → Groups**, enable **Routing**. Inside the group's
   **Routing** tab, enable **Allow selecting media**, then optionally
   **Allow uploading media** if they may add images.
   Their existing Routing allow-lists must permit the target output and at
   least one mapped VideoHub input. Config access grants setup and image
   management even without a Media grant. Routing preset changes require Admin.
   Direct player testing also
   requires Routing and media selection access. Admin has full access. The old
   separate display and image-management grants are no longer used; existing
   Media and upload grants
   remain in place but require Routing to take effect. Port access combines
   across Routing-enabled groups; a separate Media-only group does not expand
   it. Blank/all in any Routing group means unrestricted ports. Invalid port
   entries are rejected without changing the saved permissions.
6. In Config → Media, add images. For recurring graphics such as Mother's Day or
   Team Night, create a preset in **Config → Routing presets** (or select an image
   and choose **Create routing preset**). Test the operator flow through Routing.

TDeck detects the connected ATEM's video mode and player/still counts, including
the distinction between 1080p59.94 and 1080p60. Images keep their aspect ratio and
are centered on an opaque black frame; no automatic cropping or stretching.
Supported uploads are JPEG, PNG, WebP, HEIC and HEIF, up to 20 MiB and 40
megapixels. Animated files are rejected. Stored PNGs have corrected orientation,
sRGB color and no embedded EXIF/location metadata.

Reserve player images and still slots for TDeck, and keep each configured
VideoHub input receiving the corresponding player. You may share that player's
signal through manually managed AUXes or other feeds. Changing the image also
updates every destination already receiving that player. TDeck does not inspect
or verify those manual signal paths; its free-player check covers only the
configured VideoHub inputs and their current VideoHub output routes.
For each load, TDeck chooses an unselected reserved slot, waits for transfer and
image-hash confirmation, then selects and verifies the player. It protects
every player's retained still selection and refuses a load when no reserved
slot is free. The display coordinator uses complete, fresh VideoHub routes to
allocate a channel, confirms the player, then routes and verifies the
selected output. Other TDeck web/API route and preset writes return a busy
response while a display is running. ATEM transfers have a three-minute deadline;
the complete display job has a 200-second deadline with bounded network calls.
Requests are never automatically replayed after reconnection or restart.

These steps are not an atomic transaction against external ATEM/VideoHub clients
or separate CLI processes. Do not change the player image, configured input feed or target output during
a display. A failed verification may follow a hardware change, so check the
output before retrying. Actual ATEM/VideoHub compatibility still needs a live test.

The library works while the ATEM is offline. Deleting a library image does not
clear the copy already in the switcher. An image referenced by a routing preset
cannot be deleted until that preset is changed or removed. Images and `index.json` live together
under `media_library/` beside the application on Windows, or `/data/media_library`
when `/data` exists on Linux. Set `TDECK_MEDIA_DIR` in the server environment to
use another durable folder. Back up this entire folder; the existing Config ZIP
does not yet include the image library. Uploaded media stays out of Git.

### Routing presets

An administrator creates a preset under **Config → Routing presets**, choosing
its name, existing image and optional fixed VideoHub output. Leave the output as
**Ask the user to choose** to use Routing's existing permitted-output picker.
Add an optional description and ordered extra TDeck API actions, each with a
friendly description, method, local `/api/` (or `/api/v1/`) path and optional JSON
body, just like scheduler API actions. For example, a POST to `/api/timers/apply`
with `{"preset": 1}` applies timer preset 1. Saving does not run anything.

Operators choose **Routing → Presets**, select an assigned preset, choose an
output if needed, and review the image, destination and action descriptions.
Only **Confirm & apply** starts it. Fixed destinations must also be allowed by
the operator's Routing permissions. Presets can be granted separately from
general image browsing/uploading through **Permissions → Groups → Routing**.

The image loads through the existing free-player selection and verified
VideoHub routing. After that succeeds, extra actions run once in order with
administrator-approved authority, even when the operator cannot access those
controls directly. They use private in-process authentication, so no service
token is needed or exposed to the browser. Actions use the scheduler's
operational API boundary; account, setup, credential and browser-only endpoints
cannot be delegated. Config users without protected Admin membership may view
the preset editor but cannot change presets or their actions.

If an action fails, later actions stop and the image/earlier actions remain
applied. Some APIs acknowledge an asynchronous command before the device has
finished. TDeck reports partial completion and never automatically replays the
preset; check the result before confirming a new attempt. Confirmations expire
after five minutes, are bound to the initiating session and saved preset
revision, and become invalid when TDeck restarts. Permission checks run again
when applying. Activity Log records the operator and any View as administrator.

Preset definitions live in `routing_presets.json` inside the media library
folder. Back up that folder together with `auth.db` to retain both presets and
group assignments; Config ZIP does not include the media folder. Previous
images marked as presets are imported once when this file is first created,
with no fixed output or extra actions; assign their group access before use.
Limits are 500 presets, 20 actions per preset and 32 KB per action body. One
preset/display runs at a time. There is no startup or scheduled execution of
routing presets.

The media transport uses a TDeck-managed private Node child process with pinned
[`atem-connection`](https://github.com/Sofie-Automation/sofie-atem-connection).
Its transfer support is separate from the existing PyATEMMax audio integration.
No extra HTTP service, Companion action or manually launched helper is needed.
Hardware compatibility still needs a controlled first test with your switcher's
firmware and explicitly reserved players/slots; automated tests use fakes.

If Config reports that the media worker stopped, its status includes the exit
code and a compact error from the worker when available. In the deployed
Calendar folder, check the installed worker without connecting to hardware:

```powershell
node -e "require('./atem_media_worker.cjs'); console.log('Media worker imports OK')"
```

`Cannot find module 'atem-connection'` means the Node packages are missing from
that installation. Run `npm ci --omit=dev` in the folder containing
`atem_media_worker.cjs` and `package-lock.json`, then restart TDeck. Installing
Node.js alone does not install these packages. If Python cannot import
`media_library`, install `requirements.txt` using the Python environment that
runs TDeck. Record Audio uses a separate connection; an audio connection timeout
still needs checking even after the media dependencies are repaired.

For a home demo, run `python tests/media_ui_harness.py` and open
`http://127.0.0.1:5063/routing`. Choose Foyer, Media, then an image; the simulated
display returns to the output list. Config → Media is available at
`http://127.0.0.1:5063/config/atem-media`. Data resets when the demo stops, and
outbound hardware traffic is blocked. The demo has no sign-in or real transfers.

For browser checks, install tools with `npm ci`, run the demo, then in another
terminal set `TDECK_BASE_URL=http://127.0.0.1:5063` and run
`npx playwright test tests/browser/media.spec.js tests/browser/media_config.spec.js --workers=1`.

## DiGiCo Personal Mixes

TDeck can act as the one remote device connected to a DiGiCo console while serving a separate browser mixer to multiple worship-team devices. The desk-side connection is standard OSC over UDP; browsers use TDeck's normal HTTP server and do not open their own desk sockets.

Setup:

1. Open **Config → DiGiCo Mixer**.
2. Enable the integration and enter the SD9 IP and OSC port (normally `9000`).
3. Choose a free local UDP listen port (default `8000`). Allow that UDP port through the TDeck computer's firewall when using native iPad/OSC relay devices.
4. Wait for discovery to report the input-channel and AUX counts.
5. Use the **AUXes** and **Channels** tabs to set visibility and labels, choose a unified icon for each AUX and input (plus a colour for each AUX), and arrange items with the arrow buttons. A channel's optional **Section heading** appears immediately above that channel and applies visually until the next heading.
6. In **Permissions → Groups**, grant **Personal Mixes** and optionally select which AUXes that group may control. No AUX selection means all enabled AUXes.
7. Team members open `/personal-mixes`, sign in, and choose their assigned mix.

The optional **iPad / OSC Relay** tab recreates WebMixer's native OSC proxy behavior. Only explicitly configured device IPs can send packets through TDeck to the desk. Leave the device list empty when everyone uses the web page.

Operational notes:

- Run only one TDeck/OSCWebMixer instance on the configured local UDP port.
- A low-rate heartbeat keeps desk status accurate while nobody is moving a fader.
- Personal-mix faders stream coalesced updates while they move and send a final value when released.
- Every channel has a mute/unmute control that reads its initial state from the selected AUX before it can be changed.
- Personal Mix polling uses revision-only responses while a mix is unchanged, and hidden browser tabs stop polling. Fader writes remain immediate and do not wait for the read poll.
- Set request spacing to `0.025` for the fastest initial AUX-value loading. Higher values reduce desk query traffic but take proportionally longer to populate uncached mixes.
- Mixer writes and AUX permissions are enforced by the server API, not only by the browser UI.
- Diagnostics show binding errors, discovery progress, last desk packet age, packet counts, relay traffic and OSC parse errors.
- If a phone cannot load, verify it can open another TDeck page first, then check the DiGiCo diagnostics. A phone loading the page does not consume extra SD9 bandwidth; the backend shares one desk cache and UDP socket.
- Permissions and Routing render from cached/fallback hardware metadata while slow ATEM or VideoHub refreshes run in background threads, preventing hardware timeouts from holding a page request open.
- Integration status indicators share one background refresh across browsers and the periodic monitor, keeping the previous result while hardware checks run. ATEM is marked offline after three failed refreshes; additional browsers do not multiply probes or connection attempts.

Relevant configuration keys:

- `digico_enabled`, `digico_ip`, `digico_port`
- `digico_listen_address`, `digico_listen_port`
- `digico_request_interval`, `digico_retry_interval`, `digico_stale_after`
- `digico_auxes`, `digico_channels`, `digico_external_devices`

## Run (CLI)

List available apps:

```powershell
python cli.py apps
```

Start the calendar scheduler in the foreground:

```powershell
python cli.py start calendar
```

Start the calendar scheduler in the background (writes `calendar.pid`):

```powershell
python cli.py start calendar --background
```

Stop a background scheduler:

```powershell
python cli.py stop
```

## Managing events (CLI)

List events:

```powershell
python cli.py list
```

Show an event:

```powershell
python cli.py show 1
```

Add an event with triggers (examples):

```powershell
python cli.py add --name "Sunday Service" --day Sunday --date 2025-12-28 --time 10:00:00 --repeating \
  --trigger 10,BEFORE,location/1/0/1/press \
  --trigger 0,AT,location/1/0/2/press
```

Enable/disable an event:

```powershell
python cli.py disable 1
python cli.py enable 1
```

Manually fire a trigger immediately:

```powershell
python cli.py trigger 1 --which 1
```

For the full CLI reference, see `CLI_REFERENCE.md`.

Tip: for button presses, use `location/<page>/<row>/<column>/press` (or in the Web UI you can enter a short form like `1/0/1`, which it converts to `location/1/0/1/press`).

## Events file format

By default, events are stored in `events.json` (or whatever `EVENTS_FILE` points to). Each entry looks like:

```json
{
  "id": 1,
  "name": "Sunday Service",
  "day": "Sunday",
  "date": "2025-12-28",
  "time": "10:00:00",
  "repeating": true,
  "active": true,
  "times": [
    {"minutes": 10, "typeOfTrigger": "BEFORE", "buttonURL": "location/1/0/1/press"},
    {"minutes": 0,  "typeOfTrigger": "AT",     "buttonURL": "location/1/0/2/press"}
  ]
}
```

Notes:
- `minutes` is always a non-negative integer. The `BEFORE`/`AFTER` meaning comes from `typeOfTrigger`.
- If `active` or `id` are missing, the loader will fill reasonable defaults.

## Outputs and logs

- `calendar.log`: rolling log file (POST successes/failures and connectivity notes)
- `calendar_triggers.json`: snapshot of upcoming scheduled triggers (written by the scheduler)
- `calendar.pid`: pidfile used by `python cli.py stop` when running in background

## Troubleshooting

- If triggers are not firing, confirm the scheduler is running and the event is `active=true`.
- Check the scheduler's real worker/queue health from PowerShell:
  ```powershell
  Invoke-RestMethod http://127.0.0.1:5000/api/scheduler_status | ConvertTo-Json -Depth 4
  ```
  `healthy` should be `true`; confirm `next_trigger_due`, `next_trigger_event`, `queued_triggers`, and `last_trigger_success`. This reports a stopped worker or file watcher as unhealthy.
- If you see Companion connectivity errors:
  - verify `companion_ip` and `companion_port` in `config.json`
  - ensure Companion’s HTTP API is enabled/reachable
  - check `calendar.log` for POST results

- If Personal Mixes shows **Waiting for desk**:
  - confirm the SD9 remote-control IP and OSC port on both the console and TDeck
  - confirm no other process is using `digico_listen_port`
  - check Windows Firewall for the configured UDP listen port
  - use **Config → DiGiCo Mixer → Diagnostics** to identify the missing discovery request and last socket error

Run the deterministic scheduler reliability regression (including independent 8 AM and 10 AM repeating services) with:

```powershell
python -m unittest discover -s tests -p "test_scheduler_reliability.py" -v
```

## Tests

Run the OSC codec, desk-simulator integration and protected web API tests with:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

`tests/digico_ui_harness.py` starts a self-contained simulated desk and TDeck site on `http://127.0.0.1:5057` for local browser checks. It does not write its changes to the production `config.json`.

For group editor browser checks, run `python tests/permissions_ui_harness.py`.
This uses a temporary database and simulated catalogs, with outbound hardware
traffic blocked. Open `http://127.0.0.1:5064/admin/permissions?tab=groups` or, in a
second terminal, run:

```powershell
$env:TDECK_BASE_URL = "http://127.0.0.1:5064"
npx playwright test tests/browser/group_permissions.spec.js --workers=1
```

The test fixture marker is required before changing groups; use a unique
`--output` directory if OneDrive holds an earlier test artifact open.

To exercise View as user with real browser authentication and simulated media
devices, run `python tests/permissions_ui_harness.py --view-as` instead. Open
`http://127.0.0.1:5065/admin/users/2` and sign in with the fixture-only account
`fixture-admin` / `fixture-password`. For automated checks, set
`TDECK_BASE_URL=http://127.0.0.1:5065` and run
`npx playwright test tests/browser/view_as.spec.js --workers=1`. This mode uses
temporary users/media and blocks outbound hardware traffic.

For Routing presets, run `python tests/permissions_ui_harness.py --presets`.
Open `http://127.0.0.1:5066/admin/users/2` with the same fixture-only admin
credentials, or sign in as `media-operator` / `fixture-password` to test assigned
presets without general Media access. This includes simulated image display and
an authenticated local API action. From a second terminal:

```powershell
$env:TDECK_BASE_URL = "http://127.0.0.1:5066"
npx playwright test tests/browser/routing_presets.spec.js --workers=1
python -m unittest discover -s tests -p test_routing_presets.py -v
```

### Cross-browser page and failure tests

The Playwright suite exercises Chromium, Firefox, WebKit, mobile Chromium and mobile WebKit. Routing and Record Audio use intercepted device-state responses, including outage/recovery and hidden-tab polling checks, and never issue live hardware-control commands. The general page smoke is read-only.

Install the separate browser-test dependency and browser binaries on a development or CI machine:

```powershell
npm install
npm run test:browser:install
```

Start an isolated local TDeck instance, then run:

```powershell
$env:TDECK_BASE_URL = "http://127.0.0.1:5000"
$env:TDECK_USERNAME = "browser-test-user"       # only when auth is enabled
$env:TDECK_PASSWORD = "set-this-in-a-CI-secret" # only when auth is enabled
npm run test:browser
```

Use an account whose page grants match the pages being tested. Override the comma-separated smoke list with `TDECK_SMOKE_PATHS`. A non-loopback URL is rejected unless `TDECK_ALLOW_REMOTE_BROWSER_TESTS=1` is explicitly set; use that override only for an approved staging/test server, not the production control server. `TDECK_IGNORE_HTTPS_ERRORS=1` is available for a staging certificate that the test runner has not yet trusted.

Browser `error` and `unhandledrejection` events are reported as rate-limited `client.error` warnings in the Activity Log. Reports contain only a sanitized message/stack, route, source path, browser User-Agent, build ID and correlation ID. Unknown payload fields, query strings and common secret values are discarded. Set `TDECK_BUILD_ID` to the deployed commit or release identifier so reports can be matched to a release; a local content-derived ID is used when it is unset.

Known failures from browser-injected reader, night-mode and wallet scripts (`__firefox__`, `DarkReader`, and the specific `window.ethereum.selectedAddress = undefined` error) are filtered before they create new Activity Log entries. The browser filter prevents these reports from using its error-report allowance; the server also filters reports from already-open pages. Errors pointing to TDeck's `/static/` scripts, unrelated errors and generic `Script error.` messages remain visible. Existing log history is retained. Run the isolated telemetry checks with `node --test tests/client_telemetry.test.cjs` and `python -m unittest discover -s tests -p "test_client_telemetry.py" -v`.

## ProPresenter timers (optional)

This project can also act as a small “glue” service between Bitfocus Companion and ProPresenter timers:

- You maintain a list of timer presets in the Web UI (each has a name + time).
- When you save presets, the app writes the names to Companion custom variables:
  - Variable names are `companion_timer_name` + `1..N` (e.g. `timer_name_1`, `timer_name_2`, ...)
  - Variable values are formatted like: `timer_name_1: 08:15am`
- Companion buttons call the app endpoint to apply a preset (always 1-based) which sets and starts the configured ProPresenter timer.

### Timers Setup

1) Configure the app (config.json)

- `propresenter_ip`: ProPresenter host
- `propresenter_port`: ProPresenter API port
- `propresenter_timer_index`: which ProPresenter timer/clock to update
- `propresenter_is_latest`: set `true` for normal timer flow, set `false` to enable the legacy start workaround sequence
- `propresenter_timer_wait_stop_ms`: legacy-only delay after stop (default 200ms)
- `propresenter_timer_wait_set_ms`: legacy-only delay after setting time (default 600ms)
- `propresenter_timer_wait_reset_ms`: legacy-only delay after reset (default 1000ms)
- `stream_start_preset`: (optional) 1-based timer preset used to build the stream-start stage message
- `companion_ip` / `companion_port`: Companion host/port
- `companion_timer_name`: prefix for Companion timer-name variables (default: `timer_name_`)

2) Configure presets in the Web UI

- Run the Web UI: `python webui.py`
- Open the Timers page: `http://127.0.0.1:<webserver_port>/timers`
- Add/update **Name** and **Time** rows, then click **Save**

Presets are stored in `timer_presets.json`.

Stage message setup (same page):
- In the **Stage Message** section, choose which preset represents your stream start time.
- The UI shows a preview like `STREAM START 9:30AM` and has buttons to send/clear the message.

3) Configure Companion

- Create custom variables for as many timers as you want to display:
  - `timer_name_1`, `timer_name_2`, `timer_name_3`, ...
- Create buttons whose text uses those custom variables (so the button labels update after you save presets).
- For each button, add actions in this order:
  1. (Optional) “Set Custom Variable” if you want to track state in Companion
  2. “HTTP Request” to trigger the preset (details below)

### Correct Companion API Call To Trigger A Timer

Endpoint (Web UI):

- Method: `POST`
- URL: `http://<app_host>:<webserver_port>/api/timers/apply`
- Header: `Content-Type: application/json`
- Body:
```json
{"TimerIndex": 1}
```

Notes:
- `preset` is ALWAYS 1-based (`1` selects the first preset).
- You can also send it as a query param (still using POST): `.../api/timers/apply?preset=1`
- For compatibility with some Companion setups, the API also accepts `TimerIndex` (case-insensitive):
  - body: `{"TimerIndex": 1}`
  - query: `.../api/timers/apply?TimerIndex=1`

Quick test from PowerShell:

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5000/api/timers/apply" -ContentType "application/json" -Body '{"preset":1}'
```

### Stage Message (Stream Start)

Endpoint (Web UI):

- Method: `POST`
- URL: `http://<app_host>:<webserver_port>/api/propresenter/stage/stream_start`
- Body: none required

This uses the configured stream-start preset to send a stage display message like `STREAM START 9:30AM`.

Clear the stage message:

- Method: `POST`
- URL: `http://<app_host>:<webserver_port>/api/propresenter/stage/clear`

This repo also includes a small ProPresenter HTTP API client focused on timer control.

Example:

```python
from propresentor import ProPresenter

pp = ProPresenter(host="127.0.0.1", port=50001)

# List configured timers
timers = pp.list_timers()

# Start/stop/reset by name, UUID, or index
pp.start_timer("Countdown Timer")
pp.increment_timer("Countdown Timer", -10)  # subtract 10s
pp.stop_timer("Countdown Timer")
pp.reset_timer("Countdown Timer")

# Read current timer values/states
current = pp.get_current_timer_times()
```
