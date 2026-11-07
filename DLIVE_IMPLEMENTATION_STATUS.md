# dLive implementation status

## UI parity and active-provider permissions

This follow-up supersedes the historical choice to display inactive/missing
permission-grant checkboxes. It does not change the dLive transport or claim a
new full-repository-suite, deployment or physical-audio result.

- dLive AUXes/Channels now use the existing setup tabs, shared icon picker and
  arrow movement. AUX group/colour/order and input section headings persist
  through save/reload without changing mono/stereo ordinals or namespaced IDs.
  AUX group headings also render in the permitted performer picker.
- The administrator picker contains only the active desk's mappings. Both form
  and JSON saves preserve hidden raw grants; empty restricted selection is not
  All. Explicit desk-only All/None use reserved scope strings in the existing
  allow-list, preserving legacy blank/all and additive group semantics. Browser
  saves carry their catalog provider and reject stale desk context before writes.
  See `docs/DLIVE_PERSONAL_MIXES.md` for the storage/API contract.
- Strict behavioral RED failures were observed before each slice: mixed-provider
  choices, hidden-only grant loss, empty-selection broadening, disabled setup
  tabs, missing explicit All, missing performer AUX heading and stale-provider
  save acceptance. The added legacy-form stale-context case also reproduced a
  redirect-scope error before its one-line correction.
- Actual local evidence under `/home/icefire4/.hermes/cache/scratch/`:
  `dlive-parity-python.log` — **55 passed** (default guarded dLive/DiGiCo,
  capacity/configuration/frontend run before final API-case additions).
  `dlive-parity-final-python.log` — **65 passed** (dLive APIs plus group media,
  API security, impersonation, catalog organization and frontend regressions).
  `dlive-parity-api-verified.log` — final expanded **17 API cases passed**, including
  both stale JSON/form saves, disabled AUX rejection, legacy-All preservation,
  inactive grant retention and multi-group union. These runs overlap; their
  counts must not be added as distinct tests. Existing Node suites: **48 passed**.
  Node syntax and `git diff --check` also passed.
- `dlive-parity-final-evidence.log` — **25 passed**, five cases in each of desktop
  Chromium/Firefox/WebKit and mobile Chromium/WebKit. Native colour inputs,
  icon selection, arrows, saved metadata/order, grouped picker, input tabs,
  hidden-only permission autosave, explicit All, provider switch and stale
  browser-save rejection are exercised. Arrangement uses 1920px desktop/320px
  mobile; screenshots are in `dlive-parity-final-evidence/` and were inspected.
  The final legacy-form-only redirect correction was subsequently exercised by
  the 17-case Python rerun; browser-covered UI/JSON paths are unchanged.
- Initial browser runs exposed missing host dependency search paths and fixture
  session/tab-readiness races. Reused the inspected private browser environment
  at `.local/share/billy-browser/tdeck-env.sh`; no system installs. Kept raw
  failed/intermediate artifacts rather than substituting them for final passes.
- `AGENTS.md` remains unchanged: protected-file enforcement timed out awaiting
  approval. No alternate writer/bypass was used after the explicit denial;
  current implementation/test guidance is recorded in the feature document and
  this status section instead. Independent parent authorization review remains
  required before any commit or publication. No commit/push/merge/deploy,
  actual-service restart or real-hardware operation occurred.

## Pre-release provider-authorization race correction

The independent release review reproduced an authorization race: a desk-specific
All grant could pass a guard, then a concurrent provider change could replace
the client before the handler read/wrote an AUX restricted on the other desk.
The existing configuration RLock now covers all five operational mixer endpoints
from authorization through execution, without changing browser/token/scheduler
policy. Five authenticated Flask/fake-client threaded regressions cover AUX
reads, channel writes, master writes, config filtering and input meters.

Actual guarded logs under `/home/icefire4/.hermes/cache/scratch/`:
- `dlive-provider-race-red.log`: all five desired regressions failed before the
  production fix (29.701s).
- `dlive-provider-race-green.log`: all five passed after the fix (32.902s).
- `dlive-provider-race-regressions.log`: 62 default guarded tests passed
  (304.390s), including cached-read responsiveness and four-AUX/512-send capacity.
- `dlive-provider-race-api-security.log`: 11 API security regressions passed
  (68.406s).

These are overlapping focused runs, not a full-repository-suite assertion.
Independent release re-review approved the corrected candidate with no blocking
security or logic findings. Fresh post-fix browser acceptance passed all 25 cases
across the five projects (`dlive-release-browsers.log` and
`dlive-release-browser-evidence/` in the same scratch directory).
Production was left on its previous commit while the fix was verified; this
section records pre-release hardening, not a deployment receipt. The user
subsequently authorized pushing the feature branch and updating/restarting
production to that exact branch, without merging main.

## Current R4 generic Pixie revision follow-up (2026-10-26)

This narrow follow-up supersedes only the generic-writer revision gap below.
`r3-independent-review.md/json` remain unchanged FAIL reports for their frozen
source; fresh parent independent review of this candidate is pending.

- Generic `/api/config` now advances the existing Pixie revision under its existing
  shared lock whenever its accepted fields include a `pixie_*` key, including
  no-op and ABA saves. Authorization, field filtering, conflict checks and cloud
  work outside the lock are unchanged. The increment precedes persistence, so
  even a failed/partial generic save conservatively invalidates older cloud work;
  this adds no generic rollback guarantee. Unrelated generic edits do not advance
  this revision.
- Both exact independent R4 regression methods are retained verbatim in
  `tests/test_dlive_configuration.py`, without removing existing tests. Existing
  successful debug/mixer-setup composition coverage proves unrelated edits still
  permit legitimate pending provisioning.
- Actual guarded evidence in `/home/icefire4/apps/dlive-evidence/`:
  `r4-fix-red.log` reproduced both desired failures before the production change
  (**2 failures**, 20.607s; stale setup returned 200).
  `r4-fix-focused.log`: **12 passed**, 80.569s.
  `r4-fix-affected.log`: **50 passed**, 284.615s; both old cloud results returned
  **409 / `configuration_changed`**, retaining disabled/original-gateway state.
  The affected run also passed four-AUX/512-send freshness beyond 10s and cached
  read responsiveness (0.003813s before cloud release). Counts, identities and
  hashes are recorded in `r4-fix-test-summary.json`.
- Only `webui.py`, the existing configuration regression module and this status
  file were changed in this follow-up. The runner, dLive transport, JS/UI and all
  other feature source files are unchanged. No full-suite/browser rerun is claimed.
  No commit/deploy/hardware contact, service restart, branch operation, protected
  AGENTS write retry or pending manifest-target write occurred.

## Current R3 shared-lock fix checkpoint (2026-10-26)

This checkpoint supersedes the older readiness/locking statements below. Earlier
source, review and test artifacts are retained unchanged. Parent review v2
`parent-independent-review-v2.json` remains **FAIL for its frozen source**; this
worker does not grant independent approval to the new candidate.

- **R3 fixed locally:** Pixie config snapshots local dependencies under the shared
  configuration lock, then validates/provisions cloud data **outside** that lock.
  The final short locked transaction rereads current configuration and merges
  only validated Pixie-owned fields. Generic Config and mixer setup edits made
  during cloud provisioning survive; cached mixer reads and explicit controls
  can proceed during that unrelated cloud wait.
- Before writing, compare the storage-root identity, process-local Pixie revision,
  complete Pixie configuration snapshot and retained credential values. A newer
  Pixie save or config import increments the revision even with identical values
  (ABA); conflicting results return JSON **409 / `configuration_changed`** without
  writing config/secrets or replacing the manager. Do not retry stale results
  automatically. External file/CLI changes are not a process-wide transaction.
- **R1 preserved:** final read/merge/persist/reload/manager replacement remains
  serialized against generic/setup/import writers; there is no unlocked stale
  full-config overwrite. Deep-copy `get_config()` results before composing edits
  (the production helper returns only a shallow copy). Local persistence failures
  restore prior config/secret bytes and modes, including partially truncated
  writes, before returning failure; unrecoverable storage/rollback failures are
  not promised atomicity. No audio write is performed by setup/import.
- **R2 preserved:** the dLive transport/scheduling implementation and its capacity
  regressions are unchanged. Final affected verification again exercised all 512
  sends/four AUXes beyond the 10-second observation expiry.
- `tests/test_dlive_configuration.py` retains the independent desired live-read
  regression verbatim and adds nine real-auth tests covering live control/config
  composition, stale save/import/endpoint/credential results, same-value ABA,
  newer provisioned home/mode precedence, lifecycle ordering, validation/cloud
  failures and partial local-write rollback. It is in the default guarded runner.

### Actual R3 worker evidence

All files below are in `/home/icefire4/apps/dlive-evidence/`:

| Log | Actual result |
| --- | --- |
| `r3-fix-red.log` | Desired cached-read invariant failed before production changes: read blocked 1.004627s until cloud release. |
| `r3-fix-green-live.log` | Original desired invariant passed after splitting the lock phases. |
| `r3-fix-red-stale.log` | Four stale-result invariants failed before conflict detection; composition already passed. |
| `r3-fix-green-stale.log` | Six tests passed after revision/config/credential conflict detection. |
| `r3-fix-red-errors.log` | Two partial-write rollback subcases failed before restoring local-store bytes; validation/cloud errors already passed. |
| `r3-fix-focused.log` | Intermediate checkpoint: **12 passed**, 77.999s, including prior R1 invariants. |
| `r3-fix-affected.log` | Intermediate checkpoint: **48 passed**, 207.027s. |
| `r3-fix-expanded.log` | Intermediate checkpoint: **72 passed**, 74.524s. |
| `r3-fix-red-permissions.log` | Unchanged secret bytes still left a changed file mode after failed config persistence; desired rollback subcase failed before the mode comparison fix. |
| `r3-fix-final-focused.log` | Final source: **12 passed**, 85.524s, including both prior R1 concurrency/import invariants and all new R3 tests. |
| `r3-fix-final-affected.log` | Final source: **48 passed**, 212.184s. Cached dLive HTTP read completed in 0.002707s before cloud release; four-AUX / 512-send capacity passed with max foreground 0.004342s, max queue 128. |
| `r3-fix-final-expanded.log` | Final source: **72 passed**, 80.563s: prior concurrency/import plus Hisense/Pixie/Props/Timers/stream-delay shared-write suites. |

Final focused, affected and expanded runs were sequential in this worker; parsed
logs establish **118 unique test identities** across affected/expanded, with two
overlapping tests, not 120 distinct ones. `r3-fix-final-test-summary.json` records
each enumerated identity, reported count and log hash. Intermediate results remain
in `r3-fix-test-summary.json` and are not substituted for final-source testing.
`r3-fix-before.json` records the starting 22-file source hashes; the separate
`r3-fix-*` scoped receipts record only this authorized fix, not approval of the
pending full-source manifest.

### Remaining gates and honored boundaries

- Parent independent source inspection, desired-regression/browser reruns and
  final source hashing remain pending. No independent-review PASS is claimed.
- No fresh full-suite or browser pass is claimed by this worker. The historical
  unrelated baseline Record Audio/private-DB `auth_meta` failure is untouched.
- The protected `AGENTS.md` write denial remains honored without retry/bypass;
  current lock/API/test guidance is recorded here instead. The pending
  `final-source-manifest-v2.json` target was not written or retried.
- No commit, push, branch switch/stash/reset, deploy, shared service restart or
  real-hardware contact. Existing dirty feature files are preserved; only
  `webui.py`, the guarded runner, this status file and the new configuration
  regression module are changed by this worker.

## Current acceptance-defect fix checkpoint (2026-10-25)

This section supersedes readiness claims in the historical authoring record below;
that record and every earlier review/log artifact are retained unchanged.

- Independent review `/home/icefire4/apps/dlive-evidence/parent-independent-review.json`
  is **FAIL** for its frozen source (R1 config/lifecycle race, R2 polling capacity).
  Both defects were reproduced locally as desired-invariant RED tests before their
  corresponding fixes. Fresh independent review of this updated dirty candidate
  is still required; the earlier review is not represented as current approval.
- **R1 fixed:** validated setup and the other full-config TV/Pixie setup writers
  now share the existing generic/media/import/timer configuration mutation lock
  for their complete read/persist/reload/lifecycle sequence. Provider lookup also
  reads and reconciles under this boundary. Generic field filtering remains intact,
  and unrelated setup retains its existing media-busy behavior. Candidate tests
  preserve two successful concurrent edits in **both** request orders, verify old
  real TCP client closure/command refusal, and exercise actual config import plus
  setup with disk reload. Import overwrite/provider-preservation/disabled semantics
  remain unchanged; this is process-local, not external CLI/file-edit coordination.
- **R2 fixed:** one shared 65-second demand lease/cursor per active AUX; round-robin
  field refresh in bounded 16-message read batches, reserving background discovery
  capacity instead of accumulating full AUX snapshots. The configured interval
  now separates batches. Names, send queries and master queries remain exact
  documented MIDI messages; no bulk command or audio write is invented. Observed
  timestamps and the 10-second expiry are unchanged. Slow/overlarge demand gets a
  conservative `pollingWarning`/budget estimate and visible performer-page warning.
  Only loopback capacity is proved, not physical console throughput or unlimited
  client counts. Details/limitations are in `docs/DLIVE_PERSONAL_MIXES.md`.
- Reviewer invariant regressions are retained in new
  `tests/test_dlive_acceptance.py`, included in the default isolated runner. The
  R1 reproduction's barrier was adapted so serialization can wait without a test
  deadlock, while retaining final endpoint/request-success assertions and adding
  unrelated-setting composition. The R2 invariant retains healthy default TCP /
  four 128-input AUXes / no expiry after a first observation, and strengthens it
  to require all 512 sends repeatedly refreshed. Additional coverage proves live
  readers do not starve hidden demand, all eight master fields refresh, inactive
  leases expire/return, no queued snapshots accumulate and overload feedback never
  fabricates observations.

### Executed fix evidence (all under `/home/icefire4/apps/dlive-evidence/`)

| Evidence | Actual result |
| --- | --- |
| `39-r1-red.log` | Desired invariant failed in both orders: old protected host restored; unrelated generic edit lost. |
| `40-r1-green.log` | Concurrent successful generic/setup composition passed. |
| `41-r2-red.log` | Actual default TCP transport lost a previously observed send after 10s on a healthy desk. |
| `42-r2-green.log` | 27.162s capacity invariant passed after fair bounded read scheduling. |
| `43-capacity-warning-red.log` | Missing overload-feedback contract (`pollingWarning`) failed. |
| `44-capacity-warning-green.log` | Test printed OK but command exited SIGTERM/143; not accepted as a successful run. |
| `45-capacity-warning-green-rerun.log` | Clean rerun exited 0; overload feedback test passed. |
| `47-browser-warning-red.log` | Actual Chromium failed because overload warning was absent from the performer page. |
| `48-browser-warning-green.log` | Actual Chromium warning behavior passed after UI fix (3.2s). |
| `49-fixed-affected.log` | 37 passed, 130.462s, before the final import-composition test was added. |
| `50-config-write-lifecycle-regressions.log` | 72 unique Python tests passed, 74.315s: final concurrency/import tests plus TV/Pixie/Props/Timers/stream-delay write/lifecycle regressions. |
| `52-fixed-five-project-browser.log` | **15 passed**, 1.1m: all three dLive cases in desktop Chromium/Firefox/WebKit and mobile Chromium/WebKit, on a restarted isolated final-source fixture. |
| `53-fixed-node-regressions.log` | **48 passed**, zero skipped/cancelled, 1389.870059ms. |
| `55-fixed-final-affected.log` | **38 unique Python tests passed**, 135.270s. Four AUXes / 512 sends remained fresh and repeatedly observed beyond 10s; maximum combined four-state foreground response 0.004041s, maximum discovery queue 128, one connection, 7900 total read messages in the measured 27s case. |
| `56-fixed-static-checks.log` | PASS: tracked diff whitespace, two Node syntax checks, five Python AST checks and all twelve untracked source files checked for whitespace. |

The two final Python runs cover **108 unique test identities** (72 + 38 with two
shared concurrency tests), verified by parsing the logs, not added blindly.
Desktop Chromium and mobile WebKit overload-warning screenshots plus the normal
mobile Chromium performer screenshot were visually inspected: readable warning,
no overlapping or clipped warning/performer controls. Screenshots are retained in
`browser-fix-final/`. Fixture final health (`54-fixture-final-health.json`) verified
its marker and unchanged checkout auth DB. Both owned fixture process sessions
were stopped through the process manager and read back as exited; no owned fixture
or test remains running. No staging/production service or hardware was contacted.

### Current remaining gates / boundaries

- Fresh independent source review and independent affected/browser acceptance of
  this complete dirty source, including the new regression file, remain pending.
- The full 568-test run below predates these fixes. It is not a fresh full-suite
  pass; its one independently reproduced baseline private-DB `auth_meta` failure
  remains documented and untouched. This fix pass ran the affected and expanded
  shared-write regressions plus five-project browser acceptance instead.
- The prior `AGENTS.md` protected-write denial remains honored: no retry, alternate
  writer or bypass. The prior pending `final-source-manifest-v2.json` approval has
  not been retried or replaced through a different manifest target. Hash calculation
  may be reported from tool output, but no pending receipt write is claimed.
- No commit, push, merge, staging switch/restart, production change or physical
  S5000/DM32 commissioning. HEAD/branch remain the authorized baseline/candidate.

## Historical authoring record (retained)

## Scope / branch boundary

- Authorized worktree: `/home/icefire4/apps/tdeck-dlive-dev`.
- Branch: `feat/dlive-personal-mixes`, base `44060fc727993b6eeba42a08ef9eeb1540f2c3c6` (latest fetched origin/main supplied by parent).
- Git common repository: `/home/icefire4/apps/tdeck-staging/.git` (durable, outside scratch).
- Sole implementation owner; no delegation, commits, push, merge, staging switches/restarts, production access or physical console connections.
- Existing staging `.venv/bin/python` reused only as an interpreter; no global installs. `npm ci --ignore-scripts` installed this worktree's pinned browser dependencies.

## Implemented checklist

- [x] Central provider selection/lifecycle preserving legacy DiGiCo transport and HTTP contracts.
- [x] dLive mono/stereo AUX send levels with explicit mapping and independent logical IDs (1000+mono, 2000+stereo), base MIDI channel selection.
- [x] Read-only names; honest manual bus/input topology, no guessed inventory.
- [x] AUX master level/mute API and reports (no performer layout change).
- [x] Bounded TCP SysEx/running-status parser, fragmentation/coalescing, serialized writes, read scheduling, silent-peer/reconnect recovery without replay.
- [x] Observed-only levels, per-field timestamps/staleness, revision expiry; no optimistic device observations.
- [x] Existing Personal Mixes groups/tabs retained; unsupported mute/pan disabled and labelled rather than faked with whole-input controls.
- [x] Validated setup; protected provider/dLive keys ignored by generic Config saves; validated import preserves target provider and disables dLive.
- [x] Real-auth AUX scope / CSRF / browser-only setup / token and scheduler checks.
- [x] Real TCP desk simulator + API tests and five-project Playwright fixture/spec, screenshots.
- [x] Commissioning/rollback/operator/admin documentation: `docs/DLIVE_PERSONAL_MIXES.md`; README links.
- [x] Final affected Python regression: 33 passed (`34-heartbeat-final-affected.log`, 81.396s).
- [x] Final five-project dLive browser acceptance: 10 passed (`35-browser-heartbeat-final.log`, 1.0m).
- [x] Existing five-project SD9/group-tab browser regression: 50 passed (`30-sd9-browser-regression.log`, 3.1m).
- [x] Node regression suite: 48 passed (`31-node-regressions.log`, 1.495s).
- [x] Corrected full-suite run completed: **568 unique test identities**, 1642.344s, **one independently reproduced pre-existing Record Audio/private-DB failure**, no dLive/SD9 failures (`36-final-full-regression.log`). This is not an all-green suite claim.
- [x] Static checks: tracked diff whitespace, five Node syntax checks, seven Python AST checks, eleven new-file whitespace checks (`37-static-checks.log`).
- [ ] Final source-manifest refresh (tool approval pending; see below).
- [ ] Independent parent review and independent reruns before any commit.
- [ ] Physical S5000/DM32 and actual firmware/show/audio commissioning (not authorized; not claimed).

Explicit unsupported capabilities: per-send mute, per-send pan, input metering, ME-1 remote control and TLS/profile authentication. Plain MixRack TCP 51325 / V2 Surface 51328 only; TLS ports 51327/51329 are rejected, never downgraded. Master controls are scoped APIs only because the requested performer UI remains the same.

## Protocol provenance

Authoritative PDFs downloaded in full and inspected:

- `https://www.allen-heath.com/content/uploads/2024/06/dLive-MIDI-Over-TCP-Protocol-V2.0.pdf`, pp1–2 and final reference chart, saved `/home/icefire4/apps/dlive-evidence/dlive-v2.0.pdf`.
- `https://www.allen-heath.com/content/uploads/2023/05/dLive-MIDI-Over-TCP-Protocol-V1.9.pdf`, same relevant sections, saved `/home/icefire4/apps/dlive-evidence/dlive-v1.9.pdf`.
- Current official resources search linked V2.0 protocol and V2.12 firmware; no third-party module is a wire authority.
- A&H External Control article ID 37282561723537 and IT-managers article ID 37287399691409 retrieved as publisher Zendesk JSON after HTML 403. Exact source bodies and updated timestamps retained in `/home/icefire4/apps/dlive-evidence/ah-{external-control,it-managers}.{json,txt}`. IT article updated 2026-09-04; External Control updated 2025-07-18.
- Native `web.run` was not available in this execution's callable tool set; permitted search-tool retrieval was used, followed by full primary PDF/JSON retrieval. The ordinary extraction backend could not extract, and no invented response was substituted.

## Evidence directory and executed gates so far

All runtime/log/browser data is outside the source tree in
`/home/icefire4/apps/dlive-evidence/` (durable, not auto-pruned scratch).

| Log | Actual result |
| --- | --- |
| `01-send-red.log` | New wire test failed: missing provider module |
| `02-send-green.log` | 1 wire test passed |
| `03-provider-red.log` | Provider tests failed before factory existed |
| `04-provider-green.log` | 3 tests passed |
| `05-api-red.log` | 2 integration tests failed: no provider dispatch / validation |
| `06-api-green.log` | 2 integration tests passed |
| `07-recovery-red.log` | Desired freshness-revision invariant failed |
| `08-master-red.log` | Desired master roundtrip failed before implementation |
| `09-master-green.log` | 6 wire/provider tests passed including recovery and master |
| `10-setup-master-api-red.log` | Missing setup/master API acceptance failed |
| `11-affected-green.log` | 21 dLive/SD9/API/frontend tests passed (40.476s) |
| `12-browser-red.log` | Actual Chromium behavior failed: unsupported pan remained enabled; setup case passed |
| `13-browser-green.log` | 4 passed; Firefox/WebKit launch dependencies missing, not counted as functional passes |
| `14-browser-final.log` | 8 passed; 2 WebKit timing failures retained |
| `15-webkit-recheck.log` | Both WebKit cases passed on rerun (9.8s) |
| `16-import-auth-red.log` | Import enable and setup-token admission invariants failed |
| `17-import-auth-green.log` | 6 real-auth/API/import tests passed (38.441s) |
| `18-full-regression.log` | Interrupted by tool's 420s timeout; NOT a completed full-suite pass |
| `19-full-regression-complete.log` | Completed 558 tests in 1598.454s, 9 failures: 8 caused by an over-broad old harness blocking in-memory Requests adapters; 1 independently reproduced baseline QOL/auth-meta failure. Not counted as a passing suite. |
| `20-hardening-red.log` | Six desired invariants failed before hardening: inactive/missing grant retention, hidden generic fields, unknown TLS setting refusal, numeric validation and failed-command audit. |
| `21-hardening-and-harness-green.log` | 47 tests, all dLive/SD9/timer/frontend cases passed; only baseline QOL failure remained. The corrected runner permits custom in-memory BaseAdapters while denying real HTTPAdapters and non-loopback sockets. |
| `22-qol-diagnostic.log` | Propagated Record Audio exception: missing `auth_meta` table in that test's private DB. |
| `23-baseline-qol.log` | Same exact `sqlite3.OperationalError: no such table: auth_meta` independently reproduced against `git archive` of base 44060fc, outside the live checkouts. No unrelated production fix made. |
| `24-quantization-red.log` | Exhaustive 128-step observed-level roundtrip failed at floating-point truncation boundary. |
| `25-submission-red-and-wire-green.log` | All 11 wire/codec/lifecycle tests passed; API acceptance failed until explicit `confirmed: false` submission metadata was added. |
| `26-final-affected.log` | 32 passed, 80.327s. Includes actual TCP reconnect/silent-peer/ignored-command/master, all 128 level steps, mono/stereo/base-channel edges, A→B→A real factory lifecycle, authenticated API/config/import/grants, SD9 and frontend. |
| `27-browser-verified.log` | 9 passed, one Firefox test raced external feedback against an unfinished final slider write; raw failure retained. |
| `28-browser-accepted.log` | 8 passed, two Chromium timing failures with the unit test's accelerated 2s freshness in the browser fixture. Raw failures retained. |
| `29-browser-final-pass.log` | 10 passed across all five projects after awaiting the actual final slider POST and using production's 10s freshness in the browser fixture. Unit tests still exercise accelerated expiry/silent peers explicitly. |
| `30-sd9-browser-regression.log` | Existing group-tab suite: 50 passed across all five projects, 3.1m. |
| `31-node-regressions.log` | 48 passed, none skipped/cancelled. |
| `32-final-full-regression.log` | Intentionally stopped before completion to fix a concrete idle-liveness defect; NOT counted as a full-suite result. |
| `33-idle-heartbeat-red.log` | Idle healthy desk reconnected solely because no values changed: expected one connection, observed two. |
| `34-heartbeat-final-affected.log` | 33 passed, 81.396s, including the desired idle invariant plus silent-peer/EOF recovery, no replay and prior feature/SD9 cases. |
| `35-browser-heartbeat-final.log` | All 10 dLive cases passed again across five projects on the final heartbeat implementation (1.0m). |
| `36-final-full-regression.log` | Completed: 568 tests / 568 unique enumerated identities in 1642.344s, one failure: `test_qol_changes.QualityOfLifeChangesTests.test_record_audio_name_is_used_while_route_stays_compatible`. Same missing-`auth_meta` private-DB exception reproduced at base 44060fc in `23-baseline-qol.log`. No other failure/error markers; no feature tests skipped. |
| `37-static-checks.log` | PASS: tracked diff whitespace, five Node syntax checks, seven Python AST checks and eleven new-file whitespace checks. |

All owned browser fixture processes (initial and replacement dLive fixtures and
the SD9 fixture) were stopped through the process manager after their final
marker-protected health reads confirmed the checkout auth DB was unchanged.
The final full regression process has exited. Process-manager readback confirmed
that **no owned fixture or test process remains running**. No staging or production
service was switched or restarted.
Existing private browser libs at
`/home/icefire4/.local/share/billy-browser/root/usr/lib/x86_64-linux-gnu`
were inspected and reused; no system packages installed. WebKit actual execution
requires private Mesa paths and `PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1`;
the bypass alone was not counted as verification.

The earlier source receipt `/home/icefire4/apps/dlive-evidence/final-source-manifest.json`
contains SHA-256 hashes of 226 tracked/non-ignored source files, including all new
files, but is superseded by the heartbeat fix in `dlive.py`, its regression in
`tests/test_dlive.py`, and its explanation in `docs/DLIVE_PERSONAL_MIXES.md`.
`DLIVE_IMPLEMENTATION_STATUS.md` was deliberately excluded as the evolving test log.
A request to create `final-source-manifest-v2.json` is pending a one-shot tool
approval; it has NOT executed and has not been retried through another tool.
Source has not been committed; HEAD remains the base. Final receipt refresh/readback
must not be claimed completed until the approval permits actual execution.

Final screenshots are in `browser-heartbeat-final/`; final desktop/mobile Chromium
performer screenshots were inspected, as were the earlier unchanged setup/mobile
WebKit layouts in `browser-final-pass/`. Existing SD9 browser
checks cover wide/short/mobile geometry and sticky/group overflow. Mobile WebKit's
full-page keyboard-focus capture includes the existing sticky header at its
focused scroll position; it is not a physical-device test. No performer layout
redesign was made.

## Issues / remaining review gates

- Updating `AGENTS.md` was blocked by the protected-file enforcement tool despite the task's standing guidance-update authorization. The denial was honored; no alternate write path attempted. Current implementation architecture/security/test guidance is in the commissioning document and this status file. Parent may request separate approval if an AGENTS update is required.
- Exact +5 dB row has inconsistent hex/decimal chart notation; implementation follows the chart's stated `[(Gain+54)/64]*127` formula and explicitly returns quantized actual dB. Physical commissioning must verify the site's taper/firmware.
- No physical-device compatibility or audio-output confirmation is claimed.
- Independent parent source review/reruns remain mandatory before commit. No publication authorization is implied.

## Parent handoff / review focus

- Implementation is complete and exercised on loopback; it is not deployed or
  physically commissioned. Branch and HEAD were read back after the final run:
  `feat/dlive-personal-mixes` at base `44060fc727993b6eeba42a08ef9eeb1540f2c3c6`,
  with the implementation intentionally uncommitted for independent review.
- Read new/untracked `dlive.py`, `personal_mixes.py`, `static/dlive_setup.js`,
  `templates/_dlive_setup.html`, all dLive tests/harnesses and this documentation;
  ordinary tracked `git diff` omits most of the new implementation.
- Review the literal protocol fixtures and reference curve; per-send mute/pan
  refusal versus scoped AUX master operations; freshness expiry and read-only
  idle heartbeat; old-client rejection and no replay across A→B→A replacement.
- Review all write paths: dedicated setup normalization, ignored generic fields,
  import disabling/provider preservation, unsupported TLS-key refusal, retained
  inactive/missing grant checkboxes and fail-closed AUX permission lookups.
- Run `python tests/run_dlive_isolated.py` for the 33 affected cases and `--all`
  for full coverage. The sole baseline failure is documented rather than hidden
  or fixed outside scope. The branch does not claim a passing entire suite.
- Browser fixture/spec commands are in `docs/DLIVE_PERSONAL_MIXES.md`. On this host,
  the successful private browser environment was:

  ```bash
  export LD_LIBRARY_PATH=/home/icefire4/.local/share/billy-browser/root/usr/lib/x86_64-linux-gnu
  export PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=1
  export LIBGL_ALWAYS_SOFTWARE=1
  export LIBGL_DRIVERS_PATH=/home/icefire4/.local/share/billy-browser/root/usr/lib/x86_64-linux-gnu/dri
  export __EGL_VENDOR_LIBRARY_FILENAMES=/home/icefire4/.local/share/billy-browser/root/usr/share/glvnd/egl_vendor.d/50_mesa.json
  ```

- The one-shot approval for refreshing the final checksum receipt remains pending;
  the target `final-source-manifest-v2.json` was checked and is absent. Do not treat
  the older receipt as the current heartbeat source. I made no non-status source
  edits after the final full regression started.
