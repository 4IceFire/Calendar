# Repository Guidelines

This repo contains TDeck, a Python app for scheduling service cues, controlling production integrations, and serving operator/personal-control pages. Keep changes focused on the scheduler, CLI, Web UI, VideoHub and DiGiCo flows described in `README.md`.

## Keeping These Instructions Current
- Treat this file as part of the implementation contract. Whenever a change alters architecture, security, runtime configuration, API behavior, browser support, deployment steps, or test commands, update the relevant `AGENTS.md` guidance in the same change.
- Remove or rewrite superseded guidance instead of leaving contradictory historical rules in place.

## Project Structure & Module Organization
- `package/`: Python package code. `package/apps/calendar/` contains scheduler, storage, and utilities.
- Entry points: `webui.py` (Flask Web UI), `cli.py` (CLI), `companion.py`/`propresentor.py` (external integrations), `digico.py` (DiGiCo OSC transport/cache/relay), and `hisense.py` (native VIDAA TV workers, protocol compatibility, groups, target aggregation, and safe setup preflight).
- Shared infrastructure: `api_security.py` owns service-token lifecycle/constraints and `device_snapshot.py` owns process-wide stale-while-refresh hardware snapshots.
- `static/`: front-end JS/CSS assets, the shared `api_client.js` CSRF wrapper, `client_telemetry.js`, the administrator token manager in `api_tokens.js`, and pinned local vendor assets. `templates/`: HTML templates.
- Browser tests live in `tests/browser/`, with projects configured by `playwright.config.js`; `package.json`/`package-lock.json` are source-controlled for this test runner.
- Data/config: `config.json`, `events.json`, `timer_presets.json`, `videohub_presets.json`, `videohub_rooms.json`, `auth.db`.
- VideoHub room images: local uploads live in `videohub_room_images/` and should remain ignored by Git.
- Logs/runtime files: `calendar.log`, `calendar_triggers.json`, and `calendar.pid`.
- Config import rollback zips live in `config_import_backups/` and should remain ignored by Git.

## Build, Test, and Development Commands
- Create venv and install deps:
  - `python -m venv .venv`
  - `pip install -r requirements.txt`
- Run Web UI: `python webui.py` (uses `webserver_port` in `config.json`).
- Run CLI:
  - `python cli.py apps` (list apps)
  - `python cli.py start calendar --background` (scheduler)
  - `python cli.py stop` (stop background scheduler)
- Docker:
  - `docker build -t tdeck-calendar:latest .`
  - `docker compose up --build`
- Browser test setup and execution:
  - `npm install`
  - `npm run test:browser:install`
  - `npm run test:browser`
- Isolated browser telemetry regression checks: `node --test tests/client_telemetry.test.cjs` and `python -m unittest discover -s tests -p "test_client_telemetry.py" -v`. Shared message fixtures live in `tests/fixtures/client_error_noise.json`; keep browser/server classification consistent.

## Coding Style & Naming Conventions
- Python: 4-space indentation, PEP 8-style naming. Use `snake_case` for functions/vars, `CapWords` for classes, `UPPER_CASE` for constants.
- JavaScript (in `static/`): prefer `camelCase` for variables and functions.
- No formatter or linter is enforced in-repo; keep changes consistent with surrounding files.

## Testing Guidelines
- Tests use the standard-library `unittest` runner: `python -m unittest discover -s tests -p "test_*.py" -v`.
- DiGiCo tests include an in-process UDP desk simulator and Flask API/page coverage; keep tests independent of real church hardware and production config.
- Hisense tests use fake VIDAA clients/protocol detection and Flask API coverage. They must remain independent of real TVs, local certificates, and the production network.
- Hardware snapshot tests must use fakes and prove non-blocking responses, single-flight refresh, last-known-good retention, backoff, and recovery without contacting production devices.
- Frontend compatibility tests protect the explicit page-initializer pattern and the supported-browser fallbacks. Do not reintroduce page functions inside top-level conditional blocks.
- Playwright projects cover Chromium, Firefox, WebKit, mobile Chromium, and mobile WebKit. They refuse non-loopback targets unless `TDECK_ALLOW_REMOTE_BROWSER_TESTS=1`; use that override only for an approved staging server, never production.
- Browser-test authentication uses `TDECK_USERNAME` / `TDECK_PASSWORD`; deployment identity can use `TDECK_BUILD_ID`.
- Group editor browser tests use `tests/permissions_ui_harness.py` on loopback port 5064 with temporary database/catalogs and blocked hardware traffic. Run `group_permissions.spec.js` with `--workers=1`; tests require the fixture marker before writing. Its optional `--view-as` mode uses real browser auth and simulated media on port 5065 with `view_as.spec.js` and the same worker/fixture safeguards. Auth overlay tests in `test_user_impersonation.py` must use temporary databases, real session checks and faked hardware, including original Admin revocation, target scopes, CSRF rotation, account invalidation and background audit attribution.
- For manual checks: start `python webui.py`, load the UI, and run a CLI command like `python cli.py list`.
- Place new tests under `tests/` with `test_*.py` and document any additional runner in `README.md`.

## Commit & Pull Request Guidelines
- Existing commits use short, sentence-case summaries (e.g., `Updated UI`, `Added authentication to app`). Follow the same style.
- PRs should include: summary of changes, config/data file updates (`config.json`, `events.json`), and screenshots for UI changes.
- Note any migration steps or new dependencies.

## Configuration & Security Tips
- Keep secrets and environment-specific values out of Git. Use documented ignored secret stores or process environment variables for credentials; use `config.json` only for non-secret instance configuration.
- Never store service-token plaintext in `config.json`, events, URLs, logs, or source. Token hashes and metadata live in `auth.db`; plaintext is displayed only once by the Config token manager or CLI after creation/rotation.
- If you change `webserver_port`, update Docker port mappings (`docker-compose.yml`) accordingly.
- Keep `videohub_room_images/` out of source control; room backgrounds are local media, not repo assets.

## Frontend Initialization and Browser Compatibility
- Page features in `static/app.js` use explicit initializer functions such as `_initRoutingPage()` and `_initFoyerAudioPage()`. Each initializer locates its root and returns when the page is absent.
- Do not put nested function declarations inside top-level conditional blocks. Older Safari/WebKit can mis-scope their captured `const`/`let` bindings and produce errors such as `Can't find variable: elOutputs`.
- Preserve compatibility fallbacks checked by `tests/test_frontend_compatibility.py`: avoid untranspiled `Array.at()`, `Promise.finally()`, and `replaceChildren()`; retain the `<dialog>` fallback and CSS fallbacks where used.
- `static/api_client.js` must load before telemetry and page scripts. It adds CSRF only to same-origin mutating `/api/*` requests and must not modify external fetches.
- `static/client_telemetry.js` reports sanitized `error` and `unhandledrejection` events to `/api/client-errors`. Keep field allow-listing, truncation, query removal, secret redaction, authentication, CSRF/origin checks, and rate limiting intact.
- Suppress only the exact known injected reader (`__firefox__`), night-mode (`DarkReader`), and wallet (`window.ethereum.selectedAddress = undefined`) failures in both browser telemetry and server ingestion. Filter before browser dedupe/rate accounting; server filtering remains after request security/rate checks so already-open pages are covered. Preserve reports with `/static/` source/stack evidence and generic `Script error.` reports. Do not suppress all Brave errors or prevent their normal browser-console reporting.

## Static Assets and Response Caching
- `static_asset(...)` uses a SHA-256 digest of file bytes, not mtime/size. Only a URL with the current 64-character `v` digest is `public, max-age=31536000, immutable`; missing, stale, forged, or unversioned assets must revalidate.
- Keep the same cache policy on static `200` and conditional `304` responses. HTML is private/revalidated and `/api/*` plus sensitive downloads are `no-store`.
- Preserve gzip/ETag correctness and `Vary: Accept-Encoding`. Reverse proxies must preserve the digest query, ETag, Cache-Control, and Vary headers and must not independently cache HTML or live APIs.
- Runtime pages must not depend on public CDNs. Marked and DOMPurify are pinned under `static/vendor/` with license/notice files; API Reference rendering must fail closed rather than inserting unsanitized HTML.
- Do not add a general service worker that caches device/control state.

## Shared Hardware State Delivery
- `device_snapshot.SharedSnapshotCache` is the common process-wide stale-while-refresh primitive: one background refresh, immediate last-known/fallback response, exponential failure backoff, and `stale`/`refreshing`/`sampledAt`/`ageMs`/`lastError` metadata.
- HTTP request handlers must not wait on slow hardware or let browser count multiply device reads. Preserve process-wide singleton managers and shared caches.
- Integration summary and legacy status endpoints return cached/fallback state immediately and share one nonblocking refresh with the periodic monitor. Hold the refresh guard through probes, cache publication and transition logging; ATEM's three-failure offline threshold counts completed refreshes, not browser requests. Regression coverage lives in `tests/test_status_snapshots.py`.
- ATEM control state is shared across browsers; `/api/atem/audio/meters` is the compact high-rate contract and overlays the independent UDP meter data without a full PyATEMMax state read.
- VideoHub `/api/videohub/state` and `/api/videohub/labels` share one cached device snapshot. Cache misses return immediate fallback/last-known data and start one background refresh.
- Successful VideoHub route/preset commands must invalidate/update cached state and verify consolidated readback where safe; a mismatch is a failure, not a successful unconfirmed write.

## Config Export/Import
- Config transport lives under the normal Config page access: `/config/export` and `/config/import`.
- Do not add a separate permission for config transport; anyone who can access Config can export/import selected setup items.
- Exports are a single TDeck zip containing `manifest.json` plus selected payload files/folders.
- Exportable setup items include `config.json`, the configured events file, `timer_presets.json`, `trigger_templates.json`, `button_templates.json`, `calendar_triggers.json`, `companion_surfaces.json`, the configured VideoHub presets file, `videohub_rooms.json`, `home_state.json`, `auth.db`, and optional `videohub_room_images/` media.
- VideoHub room backgrounds depend on both `videohub_rooms.json` and `videohub_room_images/`; selecting VideoHub rooms should automatically carry room media when that media exists in the export/import package.
- When importing `videohub_room_images/`, keep the folder itself and replace its contents; deleting/recreating the root folder can fail with access denied on Windows/OneDrive.
- Import inspects the zip first, then lets the user choose which contained items to overwrite on the target instance.
- Imports overwrite selected files/folders instead of merging. Before replacing anything, the app creates a timestamped rollback zip in `config_import_backups/`.
- Config transport actions should log to the server console and persistent Activity Log with a `[CONFIG]` prefix.
- The old standalone Auth DB backup UI should stay removed; user/group/session data is transported via the `auth.db` item in Config export/import.

## Activity Logging
- The user-facing log system is the persistent Activity Log, shown at `/console` for route/backward-compatibility but titled Activity Log in the UI.
- Use `log_event(...)` in `webui.py` for every user-visible state change, external action, permission/security event, config transport action, and hardware/control action.
- Let `log_event(...)` infer the logged-in actor from `current_user` whenever possible. For non-user actions, set `source` to one of `web`, `api`, `companion`, `scheduler`, or `system`.
- Use stable dotted action names such as `videohub.preset.apply`, `config.import`, `user.password_reset`, `timers.preset.apply`, and `propresenter.timer.start`.
- Use `status` values `success`, `failure`, `warning`, or `info`.
- Put human-readable text in `summary`; put structured context in `details` so the Activity Log can show expandable diagnostic data.
- Never log secrets, passwords, tokens, session cookies, CSRF values, or raw credentials. The helper redacts common sensitive keys, but callers should still avoid passing secrets.
- Sanitized browser telemetry is recorded as `client.error` warnings with route, build IDs, browser User-Agent, and correlation ID. Preserve its allow-listing/redaction and never add request bodies, storage, cookies, or arbitrary client fields.
- Do not use `print(...)`, `_console_append(...)`, or raw Python logging as the primary user-facing activity record. Keep `calendar.log` and Python logging for low-level runtime diagnostics only.

## Auth Model Notes
- UI pages are protected by group-based page access in the Web UI (`require_page` checks).
- When authentication is enabled, signed-out pages must not render or poll the shared integration-status indicators. Keep the login form's standards-based username/current-password autocomplete hints so mobile password managers can recognize it.
- Users can belong to multiple groups. A user's effective page permissions are the union of all non-admin groups they belong to.
- The `Admin` group is the only protected full-access group. It grants every page and management permission and should remain non-deletable.
- Legacy `roles` / `role_pages` data may still exist in `auth.db` only as a migration source. New permissions work should use `groups`, `group_pages`, and `user_groups`.
- User management lives on `/admin/permissions` for browsing/creating users and groups, and `/admin/users/<id>` for per-user profile, access, security, sessions, and activity management.
- Account security state lives on the `users` table: email/full name, active/locked status, failed login count, force-password-change flag, password timestamps, and session version.
- `users.lockout_enabled` defaults to `1` for existing and new users. The per-user Access panel under Permissions autosaves this switch through `/api/admin/users/<id>`; an omitted API field or legacy access form preserves the policy. Failed passwords are always counted/audited, but only enabled accounts lock automatically and revoke sessions at the configured threshold. Read the current policy and count in one write transaction. Policy changes reset the failure counter; existing locks require the explicit Unlock account action, and manual locks/disabled accounts remain enforced. Record policy changes in the `user.access.update` Activity Log event.
- Logged-in sessions are tracked in `user_sessions`; revoking sessions or forcing password changes should use that table/session-version flow.
- Login return destinations must resolve to an accessible local UI page; auth helpers, login/logout, API/static/media resources, and external URLs fall back to the user's permitted landing page. Preserve required password changes before other destinations.
- Authenticated GET/HEAD visits to `/login` validate revocation and idle expiry before redirecting; credential POSTs must still work with an expired old session. Background `/auth/ping` and `/auth/touch` URLs must never become login return destinations; the browser supplies the actual page (including query/hash).
- When authentication is enabled, every `/api` route is fail-closed through `_api_policy`. Any new API route must receive an explicit scope/page policy or it remains denied.
- Browser API calls use the logged-in session. Reads require the associated page capability; writes additionally require CSRF and a trusted same-origin `Origin`/`Referer`. Resource-specific server checks remain mandatory for VideoHub ports/presets/editing, ATEM sources/solo/monitor, DiGiCo AUXes, Pixie resources, and similar allow-lists.
- Automation uses scoped Bearer service tokens from `api_security.py`. Only token hashes are stored in `auth.db`; support expiry, revocation, atomic rotation, last-used metadata, and optional path/TV/VideoHub/ATEM constraints.
- Normal lifecycle management lives at administrator-only `/config/api-tokens`; `python cli.py service-tokens create|list|rotate|revoke` remains the recovery/automation interface. The backing `/api/config/service-tokens*` routes are browser-session only, require the protected Admin group plus Config access, and must never accept a service token—even one with `admin` or `*` scope.
- List responses must never expose token hashes or plaintext. Creation/rotation may return the new plaintext exactly once; clear it from the UI when the one-time dialog closes and never include it in Activity Log details. Keep create/rotate/revoke Activity Log events, expiry/last-used visibility, and atomic replacement-before-revocation behavior.
- `/api/v1/...` aliases mirror the existing `/api/...` contracts and add `X-TDeck-API-Version: 1`; keep legacy routes during migration so saved Companion actions continue working.
- The temporary `api_legacy_anonymous_enabled` escape hatch requires `api_legacy_anonymous_until` no more than 31 days ahead and is restricted to the former Companion status/home/timer/VideoHub/TV/CCB contract. It must never expose Admin, Config, telemetry, ATEM, Pixie, DiGiCo, ProPresenter, Calendar editing, or new endpoints.
- `/videohub/monitor` requires normal authenticated VideoHub access; do not restore an unauthenticated shell backed by protected state APIs.
- API writes are rate-limited per principal and request-size limited. Preserve consistent JSON `401`/`403`/`413`/`429` responses and Activity Log events without secrets.
- VideoHub room metadata is global for all presets and users. Access control applies to who can manage the room layout UI, not to the room data itself.
- Preserve the group editor's top-level page checkboxes and Users/Groups layout. Lower settings use conditional tabs with General always available; hide tabs/panels for ungranted pages without clearing their saved values. Media selection and upload are subordinate grants inside the Routing tab, excluded from the top page list; hide upload options without clearing their value when media selection is off. Keep autosave, scoped selectors, accessible keyboard tabs, mobile overflow handling and Pixie auditorium/device dependencies intact.
- View as user is an interactive per-browser target overlay on a retained original Admin login, not a new target login. `/admin/users/<id>/view-as` and `/auth/view-as/stop` are same-origin, CSRF-protected browser-only POST transitions requiring current protected Admin membership, with no token/scheduler/anonymous/nested access. Revalidate original session/revocation/authority plus target active/unlocked/password-change/session-version state on every request before applying the overlay; never fall back to Admin and execute a stale target action. Target page/resource permissions govern all normal actions. Block credential mutations, rotate CSRF at transitions, and show a persistent identity banner with Return to admin, including denied pages. Preserve original and target session isolation and the central page/API gates.
- Activity logging during View as user records the original administrator and effective target. Use `capture_activity_actor()` before background dispatch and pass its captured identity/context to terminal `log_event` calls so attribution survives request completion.

## Scheduler and Internal API Authentication
- `webui.py` starts the Calendar scheduler automatically. Do not also run `cli.py start calendar` for the same installation; duplicate schedulers can duplicate cues.
- The built-in scheduler receives `_execute_scheduler_internal_action` from `webui.py` through `CalendarApp.set_internal_action_executor`. It dispatches inside the Flask process with a non-network `scheduler` principal, runs normal route validation, and needs no bearer token or environment variable.
- The scheduler principal is allowed only on explicitly classified operational APIs. It must remain denied from Admin, Config, service-token management, client telemetry, activity-log management, TV pairing/preflight, Pixie setup/discovery, and DiGiCo setup/restart/discovery.
- `_auth_gate` may recognize the scheduler only through the private Flask `g._tdeck_scheduler_principal` marker installed by the in-process dispatcher. Never derive this authorization from a loopback address, header, cookie, request body, URL secret, or environment token.
- `ClockScheduler` retains authenticated HTTP fallback only for a separately launched recovery/CLI scheduler process. That process is an external API client, uses `TDECK_INTERNAL_API_TOKEN`, and must not run alongside `webui.py`.
- Companion button triggers do not call the TDeck API and do not use a TDeck service token; they remain direct posts to Companion.
- Never bypass API authentication based only on loopback/`127.0.0.1`; another local process could exploit that trust.
- Keep the internal dispatcher and HTTP routes on the same endpoint validation, device queues, and Activity Logging paths. Standalone/external scheduler processes must continue using a token or authenticated IPC.

## Hisense / VIDAA TV Control
- `hisense.py` owns the process-wide manager, one serialized/reconnecting worker per TV, Wake-on-LAN, protocol/authentication selection, certificate-profile fallback, ordered group fan-out, and aggregate group state.
- A successful TDeck/Companion power-off records that TV in the backend-managed `hisense_power_state.json` runtime file. While marked intentionally off, its worker pauses reconnects, reports `expectedOff: true`/`healthy: true`, and does not create connection errors or Activity Log offline warnings. Power-on, toggle-on, reconnect, pairing, or another control clears the marker so genuine connection failures remain visible.
- Power-on must remain idempotent even though VIDAA exposes power as `KEY_POWER`, which is a toggle. If a connected TV explicitly reports on, do nothing. If that same connection explicitly reports `fake_sleep_0`, one power-key command is safe. If state is missing/timed out or the TV is offline, use Wake-on-LAN only and never fall back to a blind power key. Keep repeated on requests idempotent while confirmation is pending, and report an unconfirmed wake as pending/a warning rather than a successful TV response.
- TV setup lives at `/config/tvs` in `templates/hisense_setup.html` and `static/hisense_setup.js`. Configuration/pairing/preflight APIs require a Config-authorized browser session; operational TV/target APIs require either an authorized browser session or a service token with the `tvs` scope.
- Config lives in the main `config.json`:
  - `hisense_tvs` is the ordered TV list. Normal setup exposes only the human-readable name, IP/host, and television MAC; it auto-generates and internally preserves the slug-style ID so Companion targets remain stable. Saving through the simplified UI enables TVs and resets authentication/certificate selection to automatic. `uuid` is the separate case-sensitive paired-client UUID used by dynamic VIDAA authentication and is exposed only in the contextual Pair or repair panel.
  - `hisense_tv_groups` is the ordered group list; each group's `tv_ids` is also ordered. Membership is exclusive: a TV belongs to the first configured group that contains it, or the Ungrouped root.
  - `hisense_certificate_profiles` remains a backward-compatible backend list for custom certificate/key pairs and is not operator-configurable in the TV page. Runtime discovery always adds the standard local pairs `hisense_certs/vidaa_current.pem` / `.key` and `hisense_certs/vidaa_client.pem` / `.key`, deduplicates configured paths, and prefers current or legacy credentials based on the detected protocol and paired UUID.
  - `hisense_compatible_models`, polling, and reconnect intervals are backend-maintained values rather than normal TV-page controls.
  - Legacy `hisense_cert_path` / `hisense_key_path` remain synchronized to the first profile for config backward compatibility.
- Certificate/private-key files are local secrets and must stay ignored by Git. Do not bundle, log, export as ordinary source, or commit extracted vendor private keys.
- With authentication set to automatic, preserve static legacy first for advertised protocols below 3000 (the confirmed A7G path); newer protocols use pyvidaa's detected legacy/middle/modern dynamic authentication and persisted refresh/access tokens. A manual reconnect must redetect the protocol.
- Protocol generation can be inferred from the TV's UPnP descriptor. A TV-screen “model is no longer compatible with the current app” pairing warning cannot be observed over MQTT and can indicate an outdated client certificate generation; do not claim TDeck can infer or obtain that certificate from IP, model, or firmware alone. An administrator supplies an approved current pair at the standard backend filename, after which automatic selection retries it.
- The TV setup page mirrors the Button Templates tree interaction: compact group/root rows, persisted group and TV collapse state, per-TV group dropdown movement, and up/down sibling ordering. Deleting a group moves its TVs to Ungrouped. Collapsed group and Ungrouped rows summarize `online/total` and the number of member errors.
- Keep both identity fields distinct. The TV MAC is used only for Wake-on-LAN power-on. Newer dynamic VIDAA authentication passes the separate paired-device `uuid` to pyvidaa's historically named `mac_address` argument. Never substitute the TV MAC for the paired UUID.
- The setup page's **Run safe preflight** is serialized through the TV worker and is strictly read-only. It may check configured identity, TCP reachability, UPnP protocol generation, approved local support-pair availability, auth selection, authenticated readiness, and harmless state/capability reads; it must never wake the TV or change power, volume, mute, or source.
- Preflight states include ready, expected-off, needs-uuid, unreachable, timeout, auth-rejected, support-unavailable, and read-failed. Return actionable repair steps without returning/logging UUID values, certificate paths, refresh/access tokens, PINs, or credentials.
- Rerun/update preflight after reconnect and PIN submission. Tests must cover missing UUID, static legacy, dynamic auth, timeout, authentication rejection, unreachable, and success with fake clients only.
- `GET /api/tvs` returns TVs, groups, target choices, compatible-model notes, cached compatibility state, and the last safe preflight report. Unified operational routes use `/api/tv-targets/<target_id>/...` with `tv:<id>` or `group:<id>`; keep `/api/tvs/<id>/...` working for backward compatibility.
- Group commands enqueue once per enabled member in configured order. Group feedback semantics are strict: `connected` requires every enabled member; power/source/volume expose a shared value only when members agree, otherwise mixed state.
- The separate `companion-module-tdeck` repository consumes the unified target routes. Preserve existing action option IDs so saved Companion actions continue working; dropdown values may be legacy raw TV IDs or prefixed target IDs.

## Pixie Controls
- `pixie.py` owns direct Pixie Plus Gateway discovery, account-assisted provisioning, encrypted local inventory, the authenticated persistent control session/heartbeat, and safe physical-device/scene commands. Dependency: `pycryptodome`.
- The Pixie local protocol is unofficial and reverse-engineered. Never add or transmit a native Pixie group-address packet. A G3 Gateway previously interpreted that packet as a building-wide broadcast. TDeck auditoriums must always fan out as validated individual physical-device IDs below `0x8000`.
- `/pixie` uses `page:pixie_controls`; `/config/pixie` uses normal Config access. All `/api/pixie/*` routes enforce authentication/page access when auth is enabled, server-side auditorium/device or scene scope, and CSRF on writes.
- One Gateway/Home serves all TDeck auditoriums. Config lives in `config.json`: `pixie_network_mode`, Gateway/Home/Net/Mesh connection identifiers, plus ordered `pixie_auditoriums`, `pixie_devices`, and `pixie_scenes` arrays. Auditorium membership is exclusive and deleting an auditorium moves its devices to Ungrouped.
- Retained account credentials live only in ignored `pixie_secrets.json`. Do not include the password in config transport, API responses, source, or logs. Keep any future instance-specific Pixie JSON/runtime files ignored by Git.
- Group permission columns are `pixie_allowed_auditoriums`, `pixie_allowed_devices`, and `pixie_allowed_scenes`. Device permissions are scoped inside an auditorium grant from the same TDeck group; effective access unions across groups. `"*"` means all current/future devices in that auditorium. Admin and auth-disabled operation allow all.
- Device control types are Automatic, Dimmable, or On/Off. Automatic uses inventory classification; unknown automatic devices stay off the operator page until classified or overridden. In a mixed selection, dimmers follow 0–100% while On/Off devices receive commands only at 0% or 100%.
- The Gateway inventory's numeric `online` field is internal mesh metadata, not current reachability, and its brightness state may be stale. Live reachability and level/on-off feedback come from the authenticated Pixie Home `onlineList` (with `onlineList2` only as a fallback), fetched once per process and cached for all browsers on a one-second interval. Preserve optimistic command state through local inventory refreshes and briefly while cloud feedback catches up. Brief failures retain the last result for 30 seconds, then expose reachability as unknown without disabling direct Gateway control. Never turn the inventory's non-zero numeric value into `true`; explicitly offline devices are disabled in the operator UI and rejected by the control API.
- The operator UI polls inventory every second only while a visible Devices view has an auditorium open. Do not let feedback polling overwrite the local fader while it is being dragged. Log final device changes, scene activation, setup/permission changes, connection changes and failures; do not log selections or intermediate fader writes.

## ATEM Record Audio Controls
- The Record Audio page controls a Blackmagic ATEM 4 M/E Broadcast Studio 4K used as an audio switcher. The page route remains `/foyer-audio` and the nav label is `Record Audio`.
- Keep ATEM code in separate integration files, matching the repo's external-integration style:
  - `atem.py`: PyATEMMax-backed control/state wrapper for volume, ON/mix option, solo, monitor dim/mute/volume, labels, and source discovery.
  - `atem_meter.py`: independent legacy UDP metering client for `SALN`/`AMLv`; do not enable PyATEMMax audio level streaming for this switcher.
- Config keys live in `config.json` / the Config page:
  - `atem_ip` defaults to `127.0.0.1`
  - `atem_port` defaults to `9910`
  - `atem_timeout` defaults to `3`
- Dependency: `PyATEMMax` is used for ATEM controls. Metering is implemented locally because PyATEMMax's `AMLv` parser can crash with this older ATEM model.
- ATEM source labels should come from the switcher when available. Fallback source IDs include master, inputs 1-20, XLR 1001, AES/EBU 1101, RCA 1201, and media players.
- Group permissions live on `groups`:
  - `atem_allowed_audio_sources`: JSON list of allowed source IDs; `master` is the master volume ID. Empty list/no checked channels means no audio strips for non-admin users.
  - `atem_can_solo_audio`: allows headphone solo buttons.
  - `atem_can_monitor_audio`: allows monitor On/Dim/Volume controls.
- A user in any admin group (`groups.is_admin=1`, normally the protected `Admin` group) can always access Record Audio and see/control every source, solo, and monitor control. This is group-based, not tied to the username `admin`.
- Non-admin users only see the union of source IDs granted by their groups. Solo and monitor permissions also union across groups.
- Monitor On/Off in TDeck must use `setAudioMixerMonitorMute(...)`, inverted so On means `mute=False` and Off means `mute=True`. Do not use `setAudioMixerMonitorMonitorAudio(...)` for the TDeck On button, because that disables the monitor path and can break solo / route normal audio through headphones loudly on this switcher.
- The monitor controls are intentionally styled like TDeck controls, not like the ATEM Software Control panel.
- The Record Audio UI gets full control state on a slower cadence and compact levels from `/api/atem/audio/meters` while visible. Preserve one in-flight read, `AbortController` timeouts, recursive scheduling, visibility-aware 30-second hidden refresh, failure backoff/jitter, immediate visible refresh, and the backward-compatible full-state fallback.
- Valid cached ATEM state should render without a routine last-known-state or background-refresh notification. Reserve the Record Audio status area for actionable refresh failures and metering/connection warnings.
- The backend shares one ATEM control snapshot across all browsers and overlays the latest independent UDP meter data. Browser count must not multiply PyATEMMax state reads.
- Keep slider writes responsive/coalesced and invalidate/refresh the shared snapshot after successful commands so confirmed hardware state wins.
- `/foyer-audio/debug` is intentionally kept for production diagnosis. It reports effective permissions, ATEM sources, metering status, packet counters, and current levels. It is protected by Record Audio page access.
- All ATEM APIs pass through the central API policy. Browser writes enforce Record Audio page access plus server-side source, solo, and monitor permissions; automation requires an `atem`-scoped token and may be constrained to source IDs/actions.

## Media Library and ATEM Still Uploads
- `media_library.py` owns validated JPEG/PNG/WebP/HEIC uploads, normalized PNGs, thumbnails, atomic metadata, names/presets and opaque RGBA frame preparation. Storage is `media_library/` next to the app on Windows, `/data/media_library` on Linux with `/data`, or `TDECK_MEDIA_DIR`. Keep media ignored by Git and out of the Docker build context. Back up the full folder; it is not yet included in Config ZIP transport.
- `atem_media.py` owns a process-wide asynchronous manager and private JSON-lines IPC to `atem_media_worker.cjs`, using pinned `atem-connection`. Preserve independent ATEM audio/metering. Node subprocesses must be hidden on Windows; do not expose a second HTTP service.
- Drain worker stderr continuously with bounded retention (4,096 characters), and report a compact exception/exit-code diagnostic through Config's protected media state and pending IPC failures. Preserve the original startup error through manager cleanup; never infer a missing dependency from every worker exit. Join pipe readers during shutdown and keep retries from replaying media actions. `tests/test_atem_media.py` covers fake child startup/runtime crashes and large stderr without hardware access.
- Routing's input-step toolbar has a distinct Media action beside Change output linking to `/media?output=N`; the simple picker and separate `/media/upload` keep the output through the flow and return to `/routing` only after confirmed display. `media.html`/`media.js` show images, upload and compact job progress only. Configuration and hardware state polling belong to `media_config.html`/`media_config.js` at `/config/atem-media`, including image management and an optional player test. Routing preset setup is separate at `/config/routing-presets`. The main Routing page retains the standard shared connection indicators and cached status polling. The separate Media/upload/Presets pickers omit those indicators.
- The `page:media` grant is the Allow selecting media option inside Routing and requires `page:routing` for browsing/selecting saved images; display also requires Routing and the initiating user's allowed output/mapped VideoHub input. Optional `page:media_upload` remains in `group_pages` with union semantics but is edited below media selection in the Routing tab, excluded from the top page list. Operator upload requires Routing + Media + upload. Enforce these dependencies in both cached `_User.allows_page` and database `_user_allows_page`, covering pages, APIs, image URLs, landing redirects and View as user. Existing grants combine across groups and remain stored while their parent is off. Config grants library CRUD/read independently; direct player testing requires Config + Routing + Media. Legacy `page:media_load`/`page:media_manage` rows are inert and excluded from the registry. No new SQL permission columns.
- All media APIs remain browser-session only, including v1 aliases; service tokens and the scheduler are denied. Detailed ATEM state/setup require Config. Image/thumbnail routes allow Library OR Config through explicit `_required_any_page_keys` metadata plus the read wrapper; preset-only operators may read only images referenced by enabled, individually granted presets with allowed fixed outputs. Preserve the central non-API page gate. Setup executable-path changes additionally require protected Admin.
- Enforce upload byte limits on Flask's request stream before CSRF/multipart parsing, including absent Content-Length. Decode and limit pixels; never accept a filename extension as content validation. Keep normalized media metadata free of original image EXIF/location data.
- Setup keys are `atem_media_enabled` (false initially), `atem_media_node_path` (blank for PATH discovery), and `atem_media_destinations` (`player`, `label`, `slots`, optional `videohub_input`). Only players with a VideoHub input participate in display; unmapped players remain testable in Config. Ignore legacy destination `aux` values and remove them on normalized save, preserving the VideoHub input. UI/API numbers are 1-based; only transport converts indexes. Players, VideoHub inputs and still reservations must be distinct, with at least two exclusive slots per player; media capacities and video format are read from ATEM.
- HTTP state returns cached data immediately. One load job may run per process, with a finite deadline, transfer/hash confirmation, player readback and one terminal Activity Log event attributed to the initiator. Reconnect/startup must never upload or select an image automatically. Generation/revision checks prevent stale IPC feedback from replacing newer state.
- `media_routing.py` owns asynchronous output display, recent jobs and the nonblocking VideoHub write reservation. Only `VideohubClient.get_routing_state_strict()` with real port counts/complete routes is suitable for player allocation; never use the shared UI fallback snapshot. Ordinary Media display avoids inputs feeding other outputs and prefers the target's existing exclusive channel. Confirmed presets use the private `reuse_current_player=True` option to prefer the target's current allowed, configured player even when shared, updating all existing receivers. Never take a player serving only other outputs or bypass input grants. Always load/verify the requested still; skip only the VideoHub write when the route is already correct, with final route/player readback still required. Compare the set of other receivers before/after loading and on final readback so external routing changes cannot silently expand a shared update. Job APIs expose only compact operator fields; diagnostics, shared receiver IDs and captured initiating actor belong in Activity Log.
- ATEM AUX/output routing is manually managed by the site. TDeck must never select an AUX or reject a player because another AUX uses it; the worker may report AUX state but must ignore legacy AUX command fields. Each configured VideoHub input must already carry its media player through the site's chosen signal path. Every other destination already receiving that player also sees image changes; TDeck cannot isolate or verify manually shared feeds outside the configured VideoHub input mapping. Keep this explanation in Config rather than cluttering the operator flow.
- The reservation covers normal web/API direct and preset route writes (including token/internal dispatch) plus Config raw player tests; contention returns 409 rather than blocking HTTP. Hardware callbacks are bounded, config/device addresses are captured for the job, and the lock stays held until in-flight writes finish. Complete display deadline is 200s; underlying transfer deadline is 180s. Reserve player images/still slots for TDeck and maintain the configured VideoHub input feeds manually. This is not atomic against external controllers/separate CLI processes. Do not automatically replay a failed or interrupted display.
- Preserve `peek_atem_media_job()` for job checks without constructing a manager: invalid saved setup must remain repairable and must not block unrelated local image management. Serialize normal Config and media setup saves so stale full-config writes cannot revert reservations.
- Tests: `python -m unittest discover -s tests -p "test_*.py" -v` includes the library, protected APIs, strict VideoHub parsing, display coordinator, fake worker and actual child IPC. Node/npm dependencies are required for full transport tests. `tests/media_ui_harness.py` offers `/routing` and `/config/atem-media` at 127.0.0.1:5063 with temporary data, simulated per-session devices and blocked outbound hardware traffic. Run `tests/browser/media.spec.js` and `media_config.spec.js` with `--workers=1` because the real coordinator's reservation is process-wide. Both specs require the fixture marker before writes. Use a unique output directory for locked OneDrive artifacts.

## Routing Presets
- `routing_presets.py` owns versioned, atomic `routing_presets.json` in the media library directory, immutable per-save revisions, and the process-local asynchronous runner. Back up the entire media directory plus `auth.db`; the media directory is not included in Config ZIP. On first creation only, import legacy image `preset` flags without actions/destinations/grants. Corrupt storage fails closed and must not be silently reset. Images referenced by saved presets cannot be deleted.
- `/config/routing-presets` and `/api/config/routing-presets*` allow Config reads, but mutations additionally require protected Admin membership because saved actions delegate authority. Never execute on creation, selection, startup or config save. Keep the simple gallery/confirmation in `routing_presets.html`/`.js` and setup in `routing_presets_config.html`/`.js`.
- Routing's output-step Presets button requires `page:routing_presets`, subordinate to `page:routing`. Configure that grant and each `preset:<opaque UUID>` grant under the group's Routing tab using existing `group_pages`, not new SQL columns. No individual selections means none; grants union across groups, Admin has all. Keep hidden/unavailable saved grants while editing unrelated settings. Preset grants never imply `page:media` or upload. APIs recheck individual access, enabled state and output/mapped-input restrictions; fixed outputs cannot be overridden and optional outputs reuse the existing restricted Routing picker.
- `prepare` is browser-session/CSRF/origin protected and only signs a five-minute confirmation bound to session owner, preset revision, destination and execution ID. `apply` takes only that token, validates current permissions/config under `_media_operation_lock`, and deduplicates execution IDs for longer than token validity. A process-specific signing epoch invalidates old tokens on restart because job storage is in-memory. The browser may poll after an uncertain POST but must never replay it automatically; jobs and completion notices are owner and resource scoped.
- Use the existing `MediaRoutingManager` to confirm the image/player and VideoHub output first, enabling shared current-player reuse only from preset application. The preset confirmation explains that other screens receiving that player will also show the new image. Ordinary Media API callers cannot supply this override. Only the display success callback may execute ordered saved actions. Hold the active-preset guard through actions, stop on first failure, report partial completion without undo/replay. Device/API `202` may mean accepted rather than hardware-completed. Bound data to 500 presets, 20 actions, 32 KB JSON per action and 64 KB per preset. No external URLs, arbitrary headers or operator-supplied actions.
- Extra actions use a private identity-sentinel request marker and immutable approved action through Flask's internal dispatcher. `_api_routing_preset_internal_gate` reuses the scheduler's operational boundary and request-size limits, requiring an existing local API route/method; deny account/config/credential/browser-only endpoints, recursive preset execution and token/scheduler access to preset APIs. Never derive elevated authority from request headers, URLs, cookies or body fields. `_api_request_is_automation_principal` recognizes this private principal so approved actions may exceed the initiating user's direct control permissions. Preserve initiating actor and View as attribution in background/action Activity Log events; never log raw action bodies, query credentials or confirmation tokens.
- Regression checks: `python -m unittest discover -s tests -p test_routing_presets.py -v`, plus scheduler, API security, media, group and impersonation suites for shared guards. For real-auth browser checks run `python tests/permissions_ui_harness.py --presets` on 127.0.0.1:5066, then `TDECK_BASE_URL` pointing there with `npx playwright test tests/browser/routing_presets.spec.js --workers=1`. Require the fixture marker; fixtures use temporary users/media and blocked outbound hardware. Use all five browser projects for preset flow changes.

## VideoHub Group Controls (Where To Look)
- Storage: group settings live in `auth.db` table `groups` and are migrated/used in `webui.py`.
- Preset storage remains in `videohub_presets.json`; global room metadata is stored separately in `videohub_rooms.json`.
- Room background uploads are served from `/media/videohub_room_images/<filename>` and stored in `videohub_room_images/`.
- Routing page allow-lists (per group):
  - Columns: `videohub_allowed_outputs`, `videohub_allowed_inputs`
  - Semantics: blank/NULL/"all" => allow all; otherwise JSON list or CSV of 1-based port numbers.
  - UI: configured in the Routing tab on the Groups tab of `/admin/permissions`; enforced on `/routing` and by browser-session routing writes on the server. Single positive numbers, JSON lists and comma/whitespace-separated numbers are accepted. Validate submitted restrictions before other permission changes; malformed/non-positive values must show an error, never become allow-all. Partial updates preserve omitted restrictions. Permission lookup failures must not render unrestricted controls.
  - If a user has multiple groups, blank/all in any applicable group means all ports are allowed; otherwise restricted lists are unioned.
- VideoHub presets visibility (per group):
  - Column: `videohub_allowed_presets`
  - Semantics: blank/NULL/"all" => all presets visible; otherwise JSON list or CSV of 1-based preset IDs.
  - UI config: `templates/admin_permissions.html` + autosave payload in `static/app.js`.
  - Enforcement: hide unavailable presets in the UI and reject browser-session preset application outside the effective server-side allow-list. Service-token calls require the `videohub` scope and may have preset constraints.
- VideoHub preset editing toggle (per group):
  - Column: `videohub_can_edit_presets` (INTEGER, default allow when NULL for backward compatibility).
  - Meaning: when off, VideoHub page allows viewing/applying presets but disables create/save/delete/lock and room-based routing edits.
  - UI config: checkbox on the Groups tab of `/admin/permissions`; autosave in `static/app.js`.
  - Enforcement: `webui.py` passes `can_edit_presets` into `templates/videohub.html` and rejects browser-session edit writes on the server. Applying an allowed preset and an allowed direct route remain distinct from preset/room editing.
- VideoHub Rooms management:
  - The room editor is a separate page at `/videohub/rooms`, but it is not a page-access permission that can be assigned independently in Access Levels.
  - Access is derived from existing VideoHub access plus `videohub_can_edit_presets`.
  - Keep this behavior intact: users with VideoHub access but without edit permission can still view/apply presets on `/videohub`, but cannot manage rooms.
- Current VideoHub UI structure:
  - `/videohub`: room-based preset editor and viewer.
  - `/videohub/input-select`: dedicated input grid used when changing a single output route.
  - `/videohub/rooms`: global room/background/output-position/input-filter management.
  - `templates/videohub.html`, `templates/videohub_rooms.html`, and `templates/videohub_input_select.html` are the main templates for this flow.
- Room-layout semantics:
  - Rooms are global, shared across all presets.
  - An output can belong to only one room; outputs with no room assignment appear under `Unassigned`.
  - Room pages control output placement and background image only; they do not save routing.
  - Preset editing stages routing changes in the UI and only persists them when the user clicks Save Preset.
- Input filter semantics:
  - Filtered inputs are global and stored in `videohub_rooms.json`.
  - Input selection defaults to the filtered list and can toggle to show all inputs.
- State delivery:
  - `/api/videohub/state` and `/api/videohub/labels` share one process-wide non-blocking cache. Preserve stale/refreshing metadata, one-flight background refresh, last-known-good retention, backoff, and immediate fallback labels.
  - Successful route and preset operations update/invalidate cached routing and verify consolidated device readback. Do not reintroduce per-output verification reads or synchronous label cache misses.

## Companion Surface Embeds
- Surface definitions live in `companion_surfaces.json`.
  - Use the object format with `surfaces` and `surface_controls`.
  - `surfaces` is the reusable catalogue; each entry should include `id`, `label`, and `layout`.
  - `layout` is the Companion button-grid size as rows x columns, such as `3x5` or `2x5`. TDeck uses it to calculate the iframe display box.
  - `surface_controls` represents the hardcoded display slots on the standalone `/surface-controls` page; each entry should include `surface_id`, `label`, and `size`.
  - Display `label` values describe where that surface is being shown in TDeck. They are for config/admin clarity and should not be rendered above the surface unless the containing page explicitly wants labels.
  - Do not store crop settings in `companion_surfaces.json`; adjust the surface `layout` and display `size` instead.
  - A surface can appear multiple times in `surface_controls` with different display labels or scale values.
  - `surface_id` maps to the Bitfocus Companion surface ID and is embedded as `/emulator/<surface_id>`.
  - The Companion base URL uses `companion_ip` / `companion_port` from `config.json`; optional `companion_surface_ip` / `companion_surface_port` override keys are supported if a separate endpoint is ever needed.
- Reusable UI lives in `templates/_companion_surface.html`.
  - Import it with `{% from '_companion_surface.html' import companion_surface with context %}` so the macro can access the Companion URL helper.
  - Render with `{{ companion_surface(surface, can_click=can_click_companion_surface(surface.id)) }}` or pass per-display overrides such as `width`, `height`, and `size`.
  - The macro renders only the surface iframe/blocker; page labels/layout belong in the containing template.
- `/surface-controls` is the test page and renders every configured surface.
- `/config/companion-surfaces` is the TDeck editor for `companion_surfaces.json`.
  - It is protected by the normal Config page permission.
  - The backing API is `/api/companion-surfaces-config`.
  - The editor auto-saves shortly after changes and only shows visible status for validation/save errors; `/surface-controls` picks up layout changes on refresh.
  - The editor can add/remove surfaces from the catalogue, but should not add/remove/reorder `/surface-controls` display slots. Those slots are page-owned and hardcoded by the display entries already in `companion_surfaces.json`.
- Group click permissions live on `groups.companion_click_surfaces`.
  - Blank/NULL/`[]` means the group can click all configured surfaces.
  - A non-empty JSON list restricts clicking to those surface IDs.
  - Admin can always click every surface.
  - These permissions only block pointer/touch/keyboard interaction in TDeck's iframe UI. They do not secure Companion directly if a user opens Companion outside TDeck.
- Viewing a surface is controlled by the containing page's normal page permission. The surface click check should not be used as a view permission.

## DiGiCo Personal Mixes
- `digico.py` owns the one process-wide UDP socket, OSC codec, desk discovery/cache, heartbeat and optional external-device relay.
- DiGiCo can send address-only OSC packets without a type-tag string; the decoder must continue accepting these packets.
- `/personal-mixes` uses the `page:digico_mixer` page permission; `/config/digico` uses the normal Config permission.
- `groups.digico_allowed_auxes` stores JSON string IDs. Blank/NULL/`[]` means all enabled AUXes; otherwise multiple group lists are unioned, with any unrestricted applicable group granting all.
- Never rely on hidden browser controls for AUX authorization. Enforce the scope on all read and write mixer endpoints.
- Config lives in the main `config.json`: connection/listen/timing scalar keys plus `digico_auxes`, `digico_channels`, and `digico_external_devices` arrays. It is already included in normal config transport.
- Native relay input is accepted only from configured external device IPs. Disabled devices must not relay; loopback is off by default.
- Keep browser polling non-blocking and bounded. The threaded Flask server plus one backend cache is intentional so many phones do not multiply desk traffic.
- Log setup, restart, discovery, permission and final mixer changes with `log_event(...)`; do not log every intermediate fader drag.
