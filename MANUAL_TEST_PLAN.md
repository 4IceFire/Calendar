# Manual Test Plan

This plan covers the recent fixes and optimization work:

1. CLI support for advanced calendar trigger types
2. Timer apply path no longer double-fires Companion button presses
3. Group-based permissions and group idle timeout override
4. Removal/cleanup of legacy or dead helpers
5. Stable project-root path handling
6. Shared JSON caching
7. Home/schedule cache improvements
8. Server-side centralized status polling

## 1. What has already been machine-checked

These checks were run successfully:

- Python compile checks for:
  - `cli.py`
  - `webui.py`
  - `companion.py`
  - `videohub.py`
  - `package/json_cache.py`
  - `package/apps/calendar/storage.py`
  - `package/apps/calendar/utils.py`
  - `package/apps/calendar/scheduler.py`
  - `package/apps/videohub/storage.py`
- JavaScript syntax check for `static/app.js`
- Flask test-client smoke checks:
  - `GET /api/status/summary`
  - `GET /api/upcoming_triggers?limit=5`
  - `GET /api/home/overview`
  - `GET /api/ui/events`
  - `GET /api/timers`
  - `GET /api/companion_status`
  - `GET /api/propresenter_status`
  - `GET /api/videohub_status`
- Utility/storage smoke checks:
  - `utils.get_project_path(...)`
  - `utils.load_timer_presets()`
  - `storage.load_events_safe()`

What still needs manual validation:

- Browser behavior
- Auth/group behavior
- Real device integration with Companion/ProPresenter/VideoHub
- Real scheduler execution timing

## 2. Suggested test order

Recommended order:

1. Start the app and confirm pages load
2. Test group permissions and idle-timeout behavior
3. Test status indicators and dashboard refresh
4. Test calendar UI and trigger rendering
5. Test CLI advanced trigger support
6. Test timer apply behavior with real Companion/ProPresenter
7. Test scheduler path stability by launching from a different directory

## 3. Test environment

Use this baseline:

- App started with your normal workflow
- At least one browser logged in as Admin
- Optional but ideal:
  - Companion reachable
  - ProPresenter reachable
  - VideoHub reachable

Useful startup commands:

```powershell
python webui.py
```

Optional scheduler:

```powershell
python cli.py start calendar --background
```

## 4. Test cases

### A. Basic app startup

1. Start `webui.py`
2. Open the app in the browser
3. Visit:
   - `/`
   - `/calendar`
   - `/calendar/triggers`
   - `/timers`
   - `/config`
   - `/admin/permissions`

Expected:

- No server crash on startup
- No obvious template or JS errors
- Pages render successfully

### B. Centralized status polling

Purpose:
Verify that status indicators still work after moving polling logic server-side.

Steps:

1. Open the browser dev tools Network tab
2. Load the home page and another page with status badges
3. Watch requests for 30-45 seconds

Expected:

- The browser should request `/api/status/summary`
- It should not continuously make three separate polling requests for:
  - `/api/companion_status`
  - `/api/propresenter_status`
  - `/api/videohub_status`
- Status dots/labels should still update

Optional live test:

1. Disconnect one device or change its config to an unreachable host
2. Wait for status refresh

Expected:

- The relevant status turns offline/unknown
- Other status indicators still update normally

### C. Home dashboard cache behavior

Purpose:
Make sure Home still shows the right data after endpoint caching.

Steps:

1. Open `/`
2. Confirm upcoming event/timer/VideoHub widgets render
3. Refresh the page multiple times
4. Trigger one state change:
   - apply a timer preset
   - or apply a VideoHub preset
   - or edit event data

Expected:

- The page loads consistently
- Repeated refreshes do not break or show stale empty data
- After a real change, the dashboard updates within the expected refresh window

### D. Calendar page still loads events/templates

Purpose:
Verify shared JSON caching did not break event/template loading.

Steps:

1. Open `/calendar`
2. Confirm events appear
3. Expand an event and inspect trigger display
4. Open `/calendar/new`
5. Confirm templates load

Expected:

- Event list loads
- Trigger display still shows correct labels/details
- Template dropdowns/loaders still work

### E. Calendar triggers page

Purpose:
Verify upcoming-trigger rendering still works after schedule/cache changes.

Steps:

1. Open `/calendar/triggers`
2. Confirm upcoming triggers are listed
3. Press refresh on the page if available
4. Compare with the contents of `calendar_triggers.json` if the scheduler is running

Expected:

- Page loads without error
- Trigger rows show event name, timing, and action details
- Data looks consistent with the running scheduler

### F. CLI show/add/edit for advanced triggers

Purpose:
Verify the CLI now safely handles `api` and `timer` trigger types.

#### F1. Show existing advanced event

Steps:

1. Run:

```powershell
python cli.py show 1
python cli.py show 3
```

Expected:

- Output is JSON-like and includes full trigger objects
- `api` triggers retain `api` payloads
- `timer` triggers retain `timer` payloads
- They are not collapsed to only `buttonURL`

#### F2. Add an advanced event with JSON trigger objects

Steps:

Use one or more JSON trigger specs:

```powershell
python cli.py add --name "CLI Advanced Test" --day Tuesday --date 2026-12-01 --time 10:00:00 --trigger "{\"minutes\":0,\"typeOfTrigger\":\"AT\",\"actionType\":\"api\",\"api\":{\"method\":\"POST\",\"path\":\"/api/timers/preset\",\"body\":{\"preset\":2,\"time\":\"08:15\"}}}"
```

Expected:

- Command succeeds
- New event appears in `events.json`
- Trigger contains `actionType: "api"` and full nested payload

#### F3. Edit an advanced event without destroying payload

Steps:

1. Edit only a non-trigger field on the advanced event
2. Re-run `python cli.py show <id>`

Expected:

- Existing `api` or `timer` trigger payload remains intact
- No accidental downgrade to companion-only trigger format

### G. CLI manual trigger dispatch by action type

Purpose:
Verify `cli.py trigger` now routes by trigger `actionType`.

#### G1. Companion trigger

Steps:

```powershell
python cli.py trigger <event_id> --which 1
```

Expected:

- Companion trigger behaves as before

#### G2. API trigger

Steps:

1. Pick an event whose selected trigger is `actionType: "api"`
2. Run:

```powershell
python cli.py trigger <event_id> --which <n>
```

Expected:

- CLI does not fail assuming `buttonURL`
- It calls the local API path instead

#### G3. Timer trigger

Steps:

1. Pick an event whose selected trigger is `actionType: "timer"`
2. Run:

```powershell
python cli.py trigger 3 --which 1
```

Expected:

- CLI dispatches through `/api/timers/preset`
- It does not try to treat the timer trigger as a Companion URL

### H. Timer apply should not double-fire Companion presses

Purpose:
Verify the timer apply fix with real button-press side effects.

Recommended setup:

- Pick a timer preset with `button_presses`
- Use a Companion button that is easy to detect if it fires twice

Steps:

1. Open `/timers`
2. Apply a preset with configured `button_presses`
3. Repeat from:
   - `/timers`
   - Home quick-apply
   - any scheduler-driven timer trigger if available

Expected:

- The Companion press sequence fires once, not twice
- ProPresenter timer still sets/resets/starts normally

### I. Group-based users and permissions

Purpose:
Verify users can belong to multiple groups and inherit permissions from all assigned groups.

#### I1. Migration check

Steps:

1. Start the app after updating
2. Log in as `admin`
3. Open `/admin/permissions`
4. Check both the Users and Groups tabs

Expected:

- The old Admin, TD, and SP access levels appear as groups
- Existing users keep equivalent group membership
- The Admin group is marked as full access and cannot be deleted

#### I2. Create groups

Steps:

1. Open `/admin/permissions`
2. Select the Groups tab
3. Create a group named `Schedule Test`
4. Give it Home and Schedule access
5. Create a group named `Timers Test`
6. Give it Home and Timers access

Expected:

- New groups appear in the group list
- Changes auto-save without errors

#### I3. Create users and assign groups

Steps:

1. Open `/admin/permissions`
2. Select the Users tab
3. Create a user with no groups
4. Create a user assigned to `Schedule Test`
5. Create a user assigned to both `Schedule Test` and `Timers Test`

Expected:

- Users are created successfully
- Assigned groups appear in the user list and selected user panel
- A user with no groups is allowed to exist

#### I4. Confirm inherited permissions

Steps:

1. Log in as the user with no groups
2. Confirm protected pages are not available
3. Log in as the `Schedule Test` user
4. Confirm Schedule is available and Timers is not
5. Log in as the user in both test groups
6. Confirm both Schedule and Timers are available

Expected:

- Users inherit the union of page permissions from all assigned groups

#### I5. Remove users from groups

Steps:

1. Open `/admin/permissions`
2. Select the Users tab
3. Search for the multi-group user
4. Select the user
5. Remove `Timers Test`
6. Wait for the autosave confirmation
7. Log in as that user

Expected:

- Timers access is removed
- Schedule access remains

#### I6. Reset passwords

Steps:

1. Open `/admin/permissions`
2. Select the Users tab
3. Select a test user
4. Enter a new password for that user
5. Click Reset password
6. Log out and log in as that user with the new password

Expected:

- Old password no longer works
- New password works

#### I7. Edit account status

Steps:

1. Open `/admin/permissions`
2. Select the Users tab
3. Select a test user
4. Turn off Account active
5. Wait for the autosave confirmation
6. Try to log in as that user
7. Turn Account active back on

Expected:

- Inactive users cannot log in
- Reactivated users can log in again

#### I8. VideoHub group merging

Steps:

1. Create one group with VideoHub access and allowed preset IDs `1,2`
2. Create another group with VideoHub access and allowed preset IDs `3`
3. Assign both groups to one test user
4. Log in as that user and open `/videohub`

Expected:

- Presets 1, 2, and 3 are visible
- If either group is blank/all, all presets are visible
- A single value such as `3` stays saved as `[3]` and does not turn blank/all

#### I9. Group idle timeout override

Purpose:
Verify the group-level idle timeout field is editable and enforced.

##### I9a. Override inherits global timeout when blank

Steps:

1. Leave group override blank
2. Log in as a user in that group
3. Stay idle for slightly longer than the global timeout

Expected:

- Session expires according to global timeout

##### I9b. Override disables idle logout when `0`

Steps:

1. Set the group override to `0`
2. Log in as a user in that group
3. Stay idle longer than the global timeout

Expected:

- User should remain logged in

##### I9c. Override shortens timeout when positive

Steps:

1. Set group override to a small value, for example `1`
2. Log in as that user
3. Stay idle for just over one minute

Expected:

- User is logged out sooner than the global timeout

### J. Stable project-root path handling

Purpose:
Verify scheduler/CLI no longer depend on current working directory for local project files.

Steps:

1. Open a PowerShell session in a different directory, for example `C:\`
2. Run the app with an absolute path, for example:

```powershell
python "c:\Users\dedwa\OneDrive\Daniel's Stuff\Church\Companion\Calendar\webui.py"
```

3. In another shell, also outside the repo, run:

```powershell
python "c:\Users\dedwa\OneDrive\Daniel's Stuff\Church\Companion\Calendar\cli.py" triggers
```

Expected:

- The app still finds repo-local files correctly
- Scheduler and CLI agree on `calendar_triggers.json`
- Button template labels still resolve properly

### K. Shared JSON cache invalidation

Purpose:
Verify the new JSON cache refreshes when files change.

#### K1. Events

Steps:

1. Open `/calendar`
2. Edit or add an event
3. Refresh `/calendar` and `/calendar/triggers`

Expected:

- New event data appears
- No server restart required

#### K2. Timer presets

Steps:

1. Open `/timers`
2. Change a preset name/time
3. Refresh Home and Timers

Expected:

- Updated preset is visible
- No stale old preset data remains

#### K3. VideoHub presets/rooms

Steps:

1. Change a VideoHub preset or room config
2. Refresh VideoHub pages

Expected:

- Updated data appears after save

### L. Dead helper cleanup sanity check

Purpose:
Verify cleanup work did not break behavior.

Checks:

1. Use a normal Companion path from UI and/or CLI
2. Use normal VideoHub routing/apply flow
3. Use any normal storage/event load path

Expected:

- No missing-function errors
- No import-time crashes

## 5. Pass / fail recording template

Use this quick checklist while testing:

- `[ ]` A. Basic app startup
- `[ ]` B. Centralized status polling
- `[ ]` C. Home dashboard cache behavior
- `[ ]` D. Calendar page still loads events/templates
- `[ ]` E. Calendar triggers page
- `[ ]` F1. CLI show existing advanced event
- `[ ]` F2. CLI add advanced event
- `[ ]` F3. CLI edit advanced event safely
- `[ ]` G1. CLI manual companion trigger
- `[ ]` G2. CLI manual API trigger
- `[ ]` G3. CLI manual timer trigger
- `[ ]` H. Timer apply does not double-fire
- `[ ]` I1. Migration and unified Permissions page
- `[ ]` I2. Create groups
- `[ ]` I3. Create users and assign groups
- `[ ]` I4. Inherited permissions
- `[ ]` I5. Remove users from groups
- `[ ]` I6. Reset passwords
- `[ ]` I7. Edit account status
- `[ ]` I8. VideoHub/routing allow-list merging
- `[ ]` I9. Group idle timeout overrides
- `[ ]` J. Stable project-root path handling
- `[ ]` K1. Event cache invalidation
- `[ ]` K2. Timer preset cache invalidation
- `[ ]` K3. VideoHub cache invalidation
- `[ ]` L. Dead helper cleanup sanity

## 6. Known limits of this plan

This plan is strongest for:

- browser/UI validation
- scheduler correctness checks
- auth behavior checks
- integration checks with real devices

This plan does not replace:

- true automated integration tests
- hardware-in-the-loop validation for Companion/ProPresenter/VideoHub
