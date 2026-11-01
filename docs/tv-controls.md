# TV Controls and TV Diagnostics

## Operator workflow

- **TV Diagnostics** is a read-only room-picture overview, limited to rooms/TVs you may see. Positioned TV icons show reported power (green On, red Off, neutral Unknown/Stale with text), a volume bar and numeric value below, and the current input. Missing/unplaced TVs appear below the picture, never disappear. Feedback details retain command failures and observation times; a failed refresh immediately makes displayed feedback unverified.
- **TV Controls** uses distinct room and TV icon tiles, then an individual remote. On mobile, Back returns to the room’s TVs; Rooms returns to room selection. Desktop keeps selection on the left and the remote on the right, grey/disabled until a TV is selected. Power on/off remain explicit, not a blind toggle. Volume uses a 0–100 slider with relative +/- buttons, and every available input has a direct button (no input dropdown, directional pad or channel controls). Selection and navigation send no commands. The compact remote puts connection/power/volume/input feedback above the controls; observation times and media guidance remain under Feedback details. Normal controls fit 390×700 and 320×640 viewports without shrinking touch targets. Expanded details/errors or unusually long source lists may still scroll.
- Unknown, stale, pending and unconfirmed are not zero, off or successful execution. A request being accepted by the transport does not prove the TV obeyed it. Off requested means intentional-off bookkeeping, not a physical off measurement. The legacy API's top-level expected-off power value remains compatible; the new pages use the separate `observation` fields.
- Failure messages identify the operation and time without exposing transport credentials. Active connection/read failures are separate from historical command failures. The latest request can be pending, confirmed, mismatch, unconfirmed or failed. Relative key commands without a verifiable absolute result may remain unconfirmed.
- Do not repeatedly retry an uncertain action. Check the screen and feedback first.

## Set up rooms and access

1. Open **Config → TV Setup**. Existing TV groups are the rooms. Keep their stable IDs: Companion targets `group:<id>` and `tv:<id>` are unchanged. Existing pairing and ordered membership continue to work.
2. In **Group Permissions**, grant **TV Controls**, **TV Diagnostics**, or both. These are independent page permissions. No new permission is automatically added to an existing non-admin group.
3. Open the conditional **TVs** settings tab. Allow the room and either its individually selected TVs or all current/future TVs in that room. No selection means none. Admin has full access.
4. Room and TV grants must be in the same group as the requested page grant. Valid scoped access unions across groups; an unrelated group's room or TV grant cannot supply a missing half. Moving a TV to another room immediately removes access through its old room grant. Saved hidden settings remain stored.

The grants are stored in the existing `group_pages` table as `tvroom:<room_id>` and `tvdevice:<room_id>:<tv_id>` (or `:*`), not in a second room-assignment database. Ungrouped uses `@ungrouped`, a root identity that cannot collide with existing normalized Hisense group IDs. These are resource grants, not extra top-level pages.

Click a diagnostic TV to open `/tv-controls?tv=<stable-id>` directly on its remote, with no command sent. Links appear only for TVs currently allowed by that user’s independent TV Controls grants; Diagnostics-only readers retain a non-interactive monitor. The destination rechecks current scope before rendering.

The absolute volume slider previews a requested value while dragging and sends one command on release/change, including the iOS/WebKit track-tap fallback. Keyboard arrows use the same commit path; duplicate release/change events do not replay it. Background feedback never moves a held slider or becomes a requested value. Pending/ignored commands keep separate reported feedback. When absolute volume is stale, unknown or external/unverified, the slider is disabled with Unavailable rather than inventing zero; relative +/- remain available.

## Configure diagnostic room pictures

Open **TV Diagnostics → Configure room pictures** (or `/config/tv-rooms`) with existing **Config** access. This does not require a new grant or protected Admin membership. TV Setup still owns room names and membership; this editor never uses VideoHub output membership.

1. Choose an existing TV room, then upload a still JPEG/PNG/WebP or select an already uploaded room picture. File choice alone does not upload; Upload stores a local picture, and Save explicitly assigns it to the diagnostic room. No external image URLs or filesystem paths are accepted.
2. Select a TV and tap/click the picture to place it; drag it, use arrow keys (Shift = larger step), or enter Across/Down percentages. Unplace returns it to the fallback list. Changes stay as drafts across room selection until **Save room layout**; leaving with unsaved changes warns. Nothing here powers, routes or controls a TV.
3. Save, then reopen Diagnostics. Coordinates are normalized 0–1 within the canvas’s safe card-centre area, so edge placements remain visible responsively. Keep TVs separated in the editor; intentionally overlapping saved locations are not automatically rearranged. Narrow screens abbreviate long TV names visually; their complete names remain in the accessible heading and hover title. Missing images show an explicit fallback, not a broken/empty page.

Metadata lives in `config.json` as `hisense_room_layouts: {"<existing-room-id>": {"background": "<server-generated-id>.png", "positions": {"<existing-tv-id>": {"x": 0.25, "y": 0.65}}}}`. Moving/deleting TVs filters obsolete positions from reads and cannot grant access. Dedicated saves and config imports validate room membership, finite normalized coordinates and safe image IDs; generic Config submissions preserve dedicated edits. Existing room/TV IDs and permissions do not change. Automation config imports may not change this browser-owned metadata; unrelated existing config transport remains unchanged.

Uploaded backgrounds live in the ignored app-local **`tv_room_images/`** folder, separate from VideoHub room media. Back up this folder **alongside `config.json`**. Config ZIP includes the metadata in `config.json`, but **does not include this folder**; copy it separately when migrating/restoring. Missing restored assets remain explicitly unavailable until uploaded again. Display pages use a metadata-free JPEG preview (at most 1280 pixels on either side), generated once in the ignored `tv_room_images/previews/` cache; uploaded PNG originals remain unchanged. The derived cache is bounded to 16 MB and is safe to regenerate. Existing uploaded images gain previews on first read, without reuploading. Diagnostics retains decoded image elements across polling rather than downloading/rebuilding them each second. No new database or dependency is introduced (Pillow is already used for Media Library).

Uploads are decoded/normalized to metadata-free PNG (JPEG/PNG/WebP only, one frame, 10 MB input/output, 16 megapixels, maximum normalized dimensions 3840×2160). Streaming multipart limits apply before parsing, partial streams are closed on rejection, and the existing upload admission/rate guard bounds concurrent decoders. Local room-image storage is capped at 100 images/100 MB; older images remain reusable, never automatically deleted. An administrator can archive unused files after backing up metadata. Image reads require a current authorized room/TV (or Config), use private/no-cache revalidation and nosniff (authorization is rechecked before conditional 304 responses), and reject symlinks/traversal. Setup APIs and v1 aliases are browser-only, Config/CSRF/origin protected; tokens and scheduler are denied. Activity Log records picture uploads and layout saves, not file choice or placement drafts.

## Display saved Media Library content

1. Configure the existing ATEM media players and their real VideoHub input feeds in Media Setup. ATEM AUX wiring remains manually managed; TDeck cannot infer or verify the site's entire signal path.
2. In TV Setup's **Media signal paths**, enter the real VideoHub output and the TV input ID carrying that feed. Leave the TV unmapped until wiring is known. Saving mappings sends no device commands and does not display content.
3. On TV Controls, explicitly power/select the appropriate feed input as needed, then choose **Select media**. Fresh device-reported input must match the configured feed before display can start. Media never automatically changes TV power/input.
4. The existing searchable Media Library picker is reused. Selecting an image explicitly starts the existing ATEM/VideoHub display coordinator. Shared-player use requires the existing signed confirmation and fresh allocation checks before any upload. Warnings identify other affected output numbers without exposing restricted room names.

TV Controls grants this narrow saved-image capability for permitted mapped TVs. It does not grant general Routing, uploads, library management or raw ATEM access. Temporary images are excluded. Unmapped TVs provide setup guidance and cannot dispatch media. The browser cannot override the mapped output. Job reads require the original session owner, current TV access and unchanged mapping. An uncertain request is not automatically replayed.

Configuration is `hisense_media_mappings: {"<stable-tv-id>": {"output": <positive 1-based output>, "source": "<TV input ID>"}}`. There is deliberately no live-site default mapping.
Dedicated saves and config imports share validation; imports persist the canonical mapping (an omitted mapping becomes empty). Malformed/unknown-TV mappings are rejected before replacing config. Stale general Config submissions cannot overwrite dedicated mapping edits. Invalid runtime mappings fail closed as unmapped.

## Feedback and soundbars

The installed dependency inspected during implementation was **pyvidaa 2.1.0**. Its original `get_state` and `get_volume` can return cached values after a timeout; its volume handler does not forward volume broadcasts through `on_state_change`. The adapter in `vidaa_observed.py` observes actual MQTT receipts, including unchanged repeated reports, instead. Cached getter fallbacks do not renew field timestamps or confirm commands. Old connection-generation callbacks are ignored.

- Main-speaker type-0 numeric reports in the supported 0–100 scale provide numeric volume.
- Mute-only reports do not renew numeric volume freshness.
- Alternate report types invalidate absolute numeric volume rather than assuming their scale/meaning. The page says **External audio — volume unavailable/unverified** and disables the absolute control.
- Relative +/- sends a single supported VIDAA key per press. There is no repeated-key or alternate-transport fallback and no claim that CEC/soundbar volume obeyed it.

This is a library/provenance limitation, **not proof that every soundbar cannot report volume**. Ground Foyer's soundbar model is unknown and HDMI ARC is user-reported, not verified. No production TV, soundbar, wiring, volume, power or input was tested or changed by this feature's fixture verification. A separate approved live-device investigation is still required for site-specific conclusions.
Primary-source cross-check: `warrenrees/pyvidaa`'s `pyvidaa/client.py` agrees with the installed cache/type-0 handling. Related `tombabolewski/vidaa-control` PR #2, **Fix LEGACY TV auth, eARC volume, and macOS MAC parsing**, was open/unmerged when checked on 2026-10-18 UTC. Its author reports testing a Hisense U7N with Bose Smart Soundbar 600 over eARC, accepting type 1 on some models and a repeated-key fallback after uncertain direct volume writes. That is a proposal in a different project, not verified site telemetry or a safe fallback adopted here.

One serialized worker per TV owns push feedback and hardware reads. Commands receive immediate bounded readback; visible pages keep a five-second shared fast-poll lease with a two-second scheduling target. Idle polling uses the existing configured cadence. Browsers normally refresh the shared snapshot once a second and stop while hidden; opening more browsers does not create more hardware pollers. Read timeouts and reconnect attempts can lengthen device latency. Field observations become stale after 15 seconds; requests without matching fresh feedback become mismatch/unconfirmed after 15 seconds. No universal near-live latency is promised.

Intentional-off TVs stay quiet on startup and while idle. While an observation page holds the shared fast-poll lease they are checked read-only, without waking them, so a fresh physical-remote on report can clear the intentional-off marker. Requests/observation/failure metadata are process/manager-local. Bounded per-TV failures (ten entries, repeated read failures deduplicated) remain separate from healthy polls. Activity Log records attributed terminal commands and read failure/recovery transitions; it survives manager replacement/restarts.

## API and development boundaries

`tv_pages.py` registers the pages and scoped APIs. Legacy `/api/tvs` and `/api/tv-targets` plus v1 aliases remain available to existing constrained automation tokens. Browser reads/writes additionally enforce current room scope, including aggregate reads. Non-Config operator browsers cannot issue group commands, power toggles or reconnects. Existing Config/setup authority and gated legacy-anonymous Companion compatibility are preserved.

New `/api/tv-pages/*`, `/api/tv-media/*` and `/api/config/tv-media-mappings` routes are browser-only. CSRF/origin and the central API policy remain mandatory. TV-specific image URLs recheck page/resource permissions rather than granting access to generic media APIs. Pairing PINs and raw transport diagnostics are excluded from command Activity Log details. Async worker logs capture the initiating identity, including View as context and Companion token attribution.

### Isolated checks

- `python -m unittest discover -s tests -p 'test_tv_*.py' -v`
- `python -m unittest discover -s tests -p test_vidaa_observed.py -v`
- `node --test tests/tv_lifecycle.test.cjs` (cancel pending reads on page hide; resume polling once after cached-page restoration)
- Also run existing Hisense, media/coordinator, API security/token, group, impersonation, Activity Log, frontend and related regression suites.
- `python tests/tv_ui_harness.py --ready-file <private-scratch>/tv-ready.json`
- With Playwright tooling: `python tests/tv_browser_check.py --ready-file <private-scratch>/tv-ready.json --evidence-dir <private-scratch>/tv-evidence`
- Compact layout, pointer/touch/keyboard slider and diagnostic link checks: `python tests/tv_compact_browser_check.py --ready-file <private-scratch>/tv-ready.json --evidence-dir <private-scratch>/tv-compact-evidence` (five projects).
- Room picture/editor checks: `python tests/tv_rooms_browser_check.py --ready-file <private-scratch>/tv-ready.json --evidence-dir <private-scratch>/tv-room-evidence` (same five projects; run serially against the shared fixture).

The harness binds loopback port zero, blocks outbound sockets before app import and uses temporary auth/config/media plus fake TV/ATEM/VideoHub transports. It redirects the app's checkout-relative default auth DB during cold import, not only after initialization. Browser verification requires its exact marker and a successful reset. It exercises real page and resource permissions, explicit individual actions, accepted-but-ignored requests, readback/failures, shared existing media flow, mapping save/readback, delayed CSRF-script startup, desktop/mobile layout and screenshots. Console checks allow only the deliberately induced command HTTP 502 and reject unexpected errors. Do not run against staging/production or interpret simulated latency as hardware performance. Keep runtime/evidence outside the repository.
