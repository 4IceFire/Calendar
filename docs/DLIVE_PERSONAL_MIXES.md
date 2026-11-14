# dLive Personal Mixes: setup and commissioning

## What is implemented

TDeck's existing **Personal Mixes** page and group tabs now select either the
existing DiGiCo SD/Quantum provider or an Allen & Heath dLive MIDI-over-TCP
provider. There is one selected provider and one shared transport/cache per
process, not one console connection per browser. The default remains DiGiCo;
existing SD9 setup, IDs, OSC relay and operational HTTP routes are retained.

| Operation | dLive implementation |
| --- | --- |
| Input → mapped mono/stereo AUX send level | Read and write; asynchronous console report; 7-bit level resolution |
| AUX master fader and master mute | Read in AUX state; explicit scoped API writes only (no added performer controls) |
| Desk input/AUX names | Read-only requests for mapped resources; friendly manual labels take precedence |
| Automatic bus inventory / show topology | Not supported by this public MIDI protocol path; explicit manual mapping required |
| Per-send mute, stereo-send pan, input meters | Unavailable, not emulated with input mutes, routing reassignment or guessed packets |
| ME-1 remote mixing | Not implemented; ME-1 remains a separate personal audio mixer, not a dLive AUX |
| TLS/profile authentication | Not implemented; TLS ports are rejected and never silently downgraded |

The performer page preserves its AUX picker and group tabs. Unsupported per-send
mute/pan controls and meter placeholders are omitted, not replaced with whole-input
mutes or fabricated signal values. The routine capability banner is not shown. A
stale or not-yet-observed dLive fader is disabled rather than displayed as a
fresh zero. Device-reported observations are not proof of physical headphone
output; commissioning below remains necessary.

This is a limitation of TDeck's present MIDI/TCP provider, not a claim that the
dLive console lacks meters. True input signal metering would need a separate
supported metering transport. The documented MIDI fader/send values are setpoints,
not signal measurements. Backend `inputMeters: false` and the unsupported, empty
meter response remain explicit. Offline, stale, error and refresh-budget warnings
remain visible when actionable.

## Primary protocol references

Verified against the actual A&H documents, not Companion implementations:

- [dLive MIDI Over TCP/IP Protocol V2.0](https://www.allen-heath.com/content/uploads/2024/06/dLive-MIDI-Over-TCP-Protocol-V2.0.pdf),
  pp1–2 (transport, running status, channel selection, SysEx, levels, mutes,
  names) and the final reference chart (level conversion).
- [dLive MIDI Over TCP/IP Protocol V1.9](https://www.allen-heath.com/content/uploads/2023/05/dLive-MIDI-Over-TCP-Protocol-V1.9.pdf),
  pp1–2 and final reference chart. The implemented AUX/input messages have the
  same format in these two versions. V2 adds Surface-specific endpoints and
  cue/UFX functionality; TDeck does not implement cue/UFX operations.
- [External Control: integrating A&H systems in TCP/IP environments](https://support.allen-heath.com/hc/en-gb/articles/37282561723537-External-Control-integrating-A-H-systems-in-TCP-IP-environments).
- [Allen & Heath for IT managers](https://support.allen-heath.com/hc/en-gb/articles/37287399691409-Allen-Heath-for-IT-managers).
- [Current dLive resources](https://www.allen-heath.com/hardware/dlive-series/s7000/resources)
  still links the V2.0 MIDI specification; the dLive V2 page lists firmware
  V2.12. No assumption that a newer firmware uses the Surface port for MixRack
  connections is made.

The V1.9 and V2.0 documents both describe **plain MixRack TCP 51325** and
**TLS MixRack TCP 51327**. V2.0 separately describes **Surface TCP 51328** and
**Surface TLS 51329**. The TLS authentication message is documented as
`UserProfile, UserPassword`, profile `00`–`1F`, with `AuthOK` acceptance; this
implementation does not store credentials or attempt that handshake. AH-Net
ports 51321/51322 and ME/gigaACE/Dante connections are not substitutes for this
MIDI endpoint. **If local policy requires TLS, this provider is not suitable**:
do not disable security or forward plain console control onto the Internet.

### Implemented wire details

Human base MIDI channel is 1–12, matching Utility / Control / MIDI. Wire `N`
is that value minus one. Inputs use `N`, input number minus one. Mono AUX
ordinals 1–62 use MIDI channel `N+2`, notes `00`–`3D`; stereo AUX ordinals 1–31
use `N+2`, notes `40`–`5E`. AUX ordinals are separate within mono/stereo types,
not the Surface's strip position or an analogue socket number.

Common SysEx header: `F0 00 00 1A 50 10 01 00` (the final `01 00` is the
protocol header version, **not** the desk firmware version). Input send:

- Write: `header N 0D inputNote destinationMidi destinationNote LV F7`.
- Query: `header N 05 0F 0D inputNote destinationMidi destinationNote F7`.
- Observation is the corresponding send-level report, not a sent-value echo.
- Name query: `header midi 01 note F7`; response command `02`.
- Master level query: `header destinationMidi 05 0B 17 destinationNote F7`;
  report/write NRPN CC `63 note`, `62 17`, `06 LV` on destination MIDI channel.
- Master mute query: `header destinationMidi 05 09 destinationNote F7`;
  Note On velocity `7F` means mute, `3F` unmute, followed by velocity zero.
  Incoming velocity zero/Note Off is ignored.

Levels use the reference chart's `[(Gain + 54) / 64] * 7F` conversion with
integer truncation/clamping; `00` means minus infinity. The inverse reports
quantized actual values, e.g. requested −12 dB → `53` (83) → about −12.17 dB.
Do not expect decimal precision finer than this MIDI representation. The chart
contains inconsistent hex/decimal notation at +5; the stated formula is used
consistently rather than silently introducing an arbitrary second taper.

TCP reads support fragmentation/coalescing, bounded SysEx, MIDI running status
and interleaved real-time bytes. Complete NRPN and Note On sequences are
serialized against read requests. Disconnects and silent peers invalidate
freshness and retry the connection; controls are never buffered or replayed.
Socket send success means **submitted**, not confirmed. Only subsequent reports
update observed fields. A periodic read-only Get Name heartbeat keeps an idle
healthy desk connected without refreshing unrelated level observations. Fields
have timestamps and expire separately from connection health. A reconnect primes names and waits for fresh state queries;
startup, save, import and discovery never send audio changes.

### Shared read demand and capacity

`dlive_request_interval` is the minimum interval **between read batches**, not
between individual fields. Each TCP batch contains at most 16 complete documented
read messages (not an invented bulk MIDI command). Up to 15 slots refresh state;
at least one remains available for name discovery. AUX fields are scheduled
round-robin across active AUXes, including both master fields, with a short rest
after each full AUX sweep. Startup names cannot sit ahead of whole state snapshots.
The pending discovery queue is deduplicated and bounded by the configured resource
set; state demand uses one cursor per AUX, not repeated queued full snapshots.
Writes remain separate, explicit and serialized; batching introduces no audio
write on connect, discovery, save, import or reconnect.

Every authorized state read renews a shared **65-second AUX demand lease**.
Multiple phones on one AUX do not add refresh jobs or reset the refresh cursor.
A phone going hidden does not take its AUX's remaining turns away from live
phones, and its AUX continues refreshing during the lease even without HTTP
polls. After 65 seconds without any client demand the AUX stops consuming reads;
returning clients renew it. This is bounded demand, not indefinite subscriptions
to every configured bus. Foreground HTTP responses return cached observations
immediately; only real desk reports update timestamps. Ten-second field expiry
is unchanged, including when a connected desk reports only names or partial state.

Loopback acceptance exercises **four active AUXes × 128 sends**, both master
fields on every AUX, production defaults (25ms batch spacing / 10s freshness),
many live readers sharing an AUX, hidden readers and lease expiry/return.
This is not a physical-console or unlimited-user throughput guarantee. Slow
settings, larger active mappings or a slow/partial desk can still exceed capacity.
Status includes `polling` (active AUXes, field count, batch limit, interval,
estimated sweep and lease) and `pollingWarning`. The estimate is
`active fields × interval / 15 + sweep rest`; crossing half the freshness window
shows a **refresh budget** warning on the performer page, reserving the other
half for response/network/OS delay. It is a conservative estimate, not invented
observations or proof a desk can sustain that rate. Reduce active mappings or the
batch interval and commission the actual console; no stale threshold is silently
extended and stale controls remain disabled.

## Administrator setup

1. Back up the instance's `config.json` and `auth.db`. Do not change production
   or connect a desk merely to verify this branch. First run the isolated
   simulator/tests below.
2. Obtain the exact MixRack model, firmware, network IP, configured MIDI base
   channel, MIDI send/receive enable state and show bus topology from the audio
   engineer. S5000/DM32 is an expected installation, **not verified hardware**.
3. Open **Config → Personal Mixes** (`/config/digico` remains the compatible
   route). Select **Allen & Heath dLive**. Selection alone does not save or
   connect. Use the MixRack IP and port **51325** unless the engineer explicitly
   identifies a V2 Surface endpoint (51328). Never use audio expansion ports.
4. In **AUXes**, add mappings using the separate mono/stereo AUX ordinal. Set
   friendly labels, optional AUX headings, colours and icons; drag the ⠿ handles
   to arrange the flat picker. In **Channels**, add only intended input numbers
   (1–128), label/icon them and use **New group** to create performer groups.
   Drag channels between/within groups, or use a focused handle's Space → arrows
   → Enter sequence (Escape cancels). Group up/down buttons arrange performer tabs;
   Rename keeps a group's identity, and Delete moves its channels to Ungrouped.
   **Save & Restart**, then reload to verify your arrangement. Namespaced IDs,
   mono/stereo ordinals and enabled switches are retained independently of order.
   Friendly labels remain usable if desk names are unavailable.
   Group and drag edits show **Unsaved changes**, never an autosave claim. Failed
   saves retain the draft for correction/retry. Reloading without saving discards
   local edits. This setup behaviour is shared with DiGiCo and uses TDeck's
   VideoHub/Permissions catalog movement component.
5. Enable dLive and explicitly **Save & Restart**. This opens the selected
   read-only discovery connection; it does not set any level or mute. If
   transport or MIDI transmit is disabled, status must remain offline/waiting.
6. Configure **Permissions → Groups → Personal Mix AUX access**. dLive logical
   IDs are **1000 + mono ordinal** and **2000 + stereo ordinal**. Thus mono AUX1
   is `1001`, stereo AUX2 is `2002`. SD9 IDs remain `1`–`128`; specific existing
   SD9 grants must never become dLive grants automatically. Existing blank
   allow-lists still mean all enabled AUXes: explicitly review these broad
   grants before selecting another provider. Changes union across groups.
   The permission picker shows **only the active desk's AUXes**, including its
   disabled mappings for configuration, never old desk or missing saved IDs.
   Inactive grants remain stored and return when that desk is selected again.
   Select **All enabled AUXes on this desk** explicitly for broad access; when
   All is off, no selected AUXes means none, not all. An unrelated autosave or
   clearing the last active checkbox cannot broaden a legacy-only restriction.
   A provider change in another window requires reloading Permissions before
   saving; stale browser saves are rejected without applying other group edits.
7. Sign in as a least-privilege performer and check the AUX picker and direct
   API rejection for an unassigned AUX. Ask the engineer to approve the first
   bounded audible test before changing a real send.

The dLive provider does not implement an OSC relay. The existing relay controls
and discovered SD9 layout remain under the DiGiCo selection. Provider selection
does not delete the inactive provider's saved connection/mapping configuration.

### Stored configuration

`personal_mix_provider` is `digico` (default) or `dlive`. dLive keys:

```json
{
  "personal_mix_provider": "dlive",
  "dlive_enabled": false,
  "dlive_ip": "",
  "dlive_port": 51325,
  "dlive_midi_channel": 1,
  "dlive_request_interval": 0.025,
  "dlive_retry_interval": 1.0,
  "dlive_stale_after": 10.0,
  "dlive_auxes": [
    {"channel": 2002, "bus": 2, "stereo": true, "label": "Vocal IEM", "enabled": true, "order": 1}
  ],
  "dlive_channels": [
    {"channel": 1, "label": "Lead vocal", "group": "Vocals", "enabled": true, "order": 1}
  ]
}
```

Protected dLive/provider keys are saved only through validated Personal Mixes
setup; stale generic Config POSTs cannot change them. Complete setup and generic
Config read/persist/reload operations share the existing process-local media/config
mutation lock, as do imports, media/Props/timer configuration and the other
full-config TV/Pixie setup writers. Provider lookup/replacement is ordered under
that same boundary so an old captured config cannot reinstall a replaced target.
The config race acceptance preserves **both successful requests' edits**, including
an unrelated generic setting, in either order; it also verifies closure of the old
real provider and import/setup composition. This does not coordinate separate CLI
processes or external file editors; do not edit the live config out of process.
Config ZIP imports validate
and canonicalize mappings, preserve the **target instance's provider**, and
force dLive **disabled**. Explicitly review mappings/grants and re-enable in setup
after import. Imports do not preserve an already-running control connection.
Malformed mappings are rejected, not converted to another bus or unrestricted
access. TLS credentials are not exported because none are stored.

### Channel group metadata (both providers)

`dlive_channel_organization` and `digico_channel_organization` are setup-owned
display metadata, using the shared catalog model:

```json
{
  "folders": [{"id": "group-vocals", "name": "Vocals", "order": 0}],
  "items": [{"id": "1", "folderId": "group-vocals", "order": 0}]
}
```

Each physical input ID must occur exactly once, including disabled inputs.
Folder IDs are stable and distinct even when names match. Folder array order and
member order drive performer tabs/channels; Ungrouped appears first. Empty groups
persist in setup without empty performer tabs; disabled inputs do not appear to
performers. New discovered inputs append at Ungrouped in the read view without
writing configuration. DiGiCo mappings remain indexed by physical input number,
not display position. dLive input numbers and namespaced mono/stereo AUX IDs are
unchanged by movement. AUX order stays in the existing mapping `order` fields.

Absent/null metadata retains the existing heading interpretation on the performer
page (including repeated headings and **Inputs** fallback). Setup derives draft
groups from those legacy sections; only explicit Save & Restart persists them.
Group data never grants access or sends a mixer command. Both keys are hidden from
generic Config and protected against generic writes; Config ZIP import validates
both organizations before replacing config. Unknown/duplicate IDs, missing members,
unknown folders and malformed groups fail without a partial save. Saving the
active desk preserves the inactive desk's untouched configuration.

## HTTP compatibility and safety

Existing `/api/digico/...` and `/api/v1/digico/...` paths and `digico` service-token
scope remain the compatibility contract regardless of selected provider.
Existing page permission is `page:digico_mixer`, existing group storage is
`digico_allowed_auxes`; no automatic grant or schema migration is performed.
Legacy blank/NULL/`[]` grants keep their original global-all meaning. Restricted
empty selections and explicit desk-only All selections use reserved string scope
markers (`__none_dlive__`, `__none_digico__`, `__all_dlive__`, `__all_digico__`)
in that existing JSON list, not new console IDs. Only an All marker for the
current provider expands to unrestricted; inactive IDs/markers remain inert.
Both form and JSON editors preserve hidden entries, and disabled/unmapped AUXes
are still rejected on operational reads/writes even for Admin users.

The permission page and both group writers share the configuration lock to keep
catalog scope stable through saves. Browser payloads include
`digico_aux_catalog_provider`; a stale provider gets JSON 409 or the existing
form error redirect before any group fields change. Legacy clients omitting the
marker remain supported, with the existing server-side resource guards intact.
Operational mixer reads and controls hold that same configuration lock from
provider-scoped authorization through client selection, route validation and
command submission. A concurrent provider change therefore cannot turn an All
grant for one desk into access to a restricted AUX on the other. Cached read
responses and bounded command submission remain unchanged; background network
provisioning does not hold this operational boundary.

- GET `mixer/config`: current provider, capabilities, mapped channels/AUXes,
  unchanged presentation fields and status. API filters AUXes by group grants.
- GET `aux/<id>/state`: input levels plus `observedAt` and `stale`; `master`
  includes level/mute and individual observation timestamps. Unknown/stale
  fields are null. Revision-only polling remains available.
- POST `aux/<id>/channel/<input>/level`: existing `{value, final}` shape;
  response value is quantized but is **not** an observation.
- POST `aux/<id>/channel/<input>/pan` or `/on`: unsupported on dLive, rejected
  without a wire command.
- POST `aux/<id>/master/level`: `{ "value": -12 }`.
- POST `aux/<id>/master/mute`: `{ "value": true }` (true means AUX master
  muted, not the input channel and not a single personal send).
- Master writes require exactly the same server-side AUX grants and operational
  API authority. They are not available on legacy providers without that method.
  Read state afterwards; `confirmed: false` on submission is deliberate.
- Setup/restart/discovery are Config-browser-only, protected by CSRF/origin;
  service tokens and scheduler principals are denied. Operational token path
  restrictions and browser resource checks are unchanged. No anonymous bypass.

## Physical commissioning (outstanding; engineer approval required)

1. Use a spare or explicitly approved AUX/input path; lower listening gain and
   protect performers' ears. Record the starting console settings manually.
2. Confirm that only the mapped resources appear. Do not use a real main mix,
   preamp, whole-channel mute, scene recall or ME-1 as a protocol probe.
3. Compare a known send at the desk with TDeck's first received value. Change it
   at the console and verify all browsers update without touching their faders.
4. Approve one small send-level change from one performer browser. Confirm the
   correct mono/stereo AUX, actual console value and audible headphone path.
   Separately verify a second AUX remains unchanged. Restore manually if needed.
5. If master API control is wanted, separately approve a bounded master fader
   test and a mute/unmute pair on that AUX. Confirm no whole-input mute changes.
6. Disconnect the control network, try a command (must fail, not queue), restore
   it and verify recovery with fresh reports and no replay. Repeat after a desk
   reboot and a MIDI send/receive disable/enable. Expect stale values to clear.
7. Repeat for the actual firmware/base-channel setting and stereo topology.
   Verify firewall/network isolation, token paths and ordinary group grants.
8. Do not call the implementation site-compatible until those checks pass. A
   simulated desk, an HTTP 200 and a connected TCP socket are not audio evidence.

Rollback: select DiGiCo in setup and explicitly save. Its saved OSC setup remains;
verify old grants and listeners. Restore the original config/auth backup if site
mapping/permission changes were made. Do not switch/restart a shared installation
while another operator/session is using it.

## Reproducible isolated verification

Use the repository's Python environment with requirements installed; on the
authoring host `/home/icefire4/apps/tdeck-staging/.venv/bin/python` was reused,
without modifying or restarting that checkout.

```bash
python tests/run_dlive_isolated.py
# Complete suite, with in-memory HTTP adapters allowed but hardware blocked:
python tests/run_dlive_isolated.py --all
# Reviewer desired invariants + concurrency/import/demand capacity regressions:
python tests/run_dlive_isolated.py test_dlive_acceptance test_personal_mix_organization test_catalog_organization
node --test tests/personal_mix_organization.test.cjs
npm ci --ignore-scripts
python tests/dlive_ui_harness.py --ready-file /your/private/evidence/ready.json
# Read the marker-protected loopback URL from ready.json, then:
TDECK_BASE_URL=http://127.0.0.1:PORT npx playwright test tests/browser/dlive.spec.js tests/browser/personal_mix_organization.spec.js --workers=1
```

The fixture cold-imports with a private auth DB, blocks outbound hardware, uses
real dLive TCP/Flask/auth, resets per case and verifies a fixture marker before
writes. All five projects cover actual send writes/readback, group tabs,
unsupported controls, scope rejection, setup save/reload and selection without
writes, plus an actual overcapacity warning without fabricated levels. Arrangement
coverage uses native colour inputs, the shared icon modal and shared drag controls,
then verifies saved metadata/order, a grouped/coloured performer picker and input
tabs after reload. Permissions coverage exercises hidden-only restrictions,
explicit All, unrelated autosave, provider switching and stale-editor rejection.
Desktop arrangement runs at 1920px and mobile emulations at 320px; other cases
retain normal project sizes. Screenshots
are generated in Playwright output. Existing SD9 browser
regressions still use `personal_mix_tabs_ui_harness.py` and
`personal_mix_tabs.spec.js`, separately from the dLive fixture. See
`DLIVE_IMPLEMENTATION_STATUS.md` for exact authoring evidence and remaining gates.

The group spec also exercises both providers with native mouse cancel/drop,
keyboard movement, real mobile Chromium touch AUX movement, stable edited row
nodes, duplicate group names, empty/deleted groups, reduced motion/320px overflow,
failed-save draft recovery and actual setup/performer reload/readback. The DiGiCo
fixture feeds synthetic discovery into the real indexed client normalizer without
opening UDP; dLive uses the real loopback TCP desk. All fixtures require readiness,
the isolated marker and a successful reset before mutation, with one worker. No
test result here establishes physical-console compatibility or authorizes a deploy.
