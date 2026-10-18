# TV Controls and TV Diagnostics

## Operator workflow

- **TV Diagnostics** is a read-only building overview, grouped by the rooms you may see. Each TV shows reported power, volume and input with its own observation time. Command failures remain visible after connectivity recovers.
- **TV Controls** lets you choose a room, then one TV. Power on and Power off are explicit actions, not a toggle. Volume and input controls affect only that TV. Choosing a room/TV sends no commands.
- Unknown, stale, pending and unconfirmed are not zero, off or successful execution. A request being accepted by the transport does not prove the TV obeyed it. Off requested means intentional-off bookkeeping, not a physical off measurement. The legacy API's top-level expected-off power value remains compatible; the new pages use the separate `observation` fields.
- Failure messages identify the operation and time without exposing transport credentials. Active connection/read failures are separate from historical command failures. The latest request can be pending, confirmed, mismatch, unconfirmed or failed. Relative key commands without a verifiable absolute result may remain unconfirmed.
- Do not repeatedly retry an uncertain action. Check the screen and feedback first.

## Set up rooms and access

1. Open **Config → TV Setup**. Existing TV groups are the rooms. Keep their stable IDs: Companion targets `group:<id>` and `tv:<id>` are unchanged. Existing pairing and ordered membership continue to work.
2. In **Group Permissions**, grant **TV Controls**, **TV Diagnostics**, or both. These are independent page permissions. No new permission is automatically added to an existing non-admin group.
3. Open the conditional **TVs** settings tab. Allow the room and either its individually selected TVs or all current/future TVs in that room. No selection means none. Admin has full access.
4. Room and TV grants must be in the same group as the requested page grant. Valid scoped access unions across groups; an unrelated group's room or TV grant cannot supply a missing half. Moving a TV to another room immediately removes access through its old room grant. Saved hidden settings remain stored.

The grants are stored in the existing `group_pages` table as `tvroom:<room_id>` and `tvdevice:<room_id>:<tv_id>` (or `:*`), not in a second room-assignment database. Ungrouped uses `@ungrouped`, a root identity that cannot collide with existing normalized Hisense group IDs. These are resource grants, not extra top-level pages.

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

The harness binds loopback port zero, blocks outbound sockets before app import and uses temporary auth/config/media plus fake TV/ATEM/VideoHub transports. It redirects the app's checkout-relative default auth DB during cold import, not only after initialization. Browser verification requires its exact marker and a successful reset. It exercises real page and resource permissions, explicit individual actions, accepted-but-ignored requests, readback/failures, shared existing media flow, mapping save/readback, delayed CSRF-script startup, desktop/mobile layout and screenshots. Console checks allow only the deliberately induced command HTTP 502 and reject unexpected errors. Do not run against staging/production or interpret simulated latency as hardware performance. Keep runtime/evidence outside the repository.
