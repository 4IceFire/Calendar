# VideoHub Presets operator guide and proposed developer guidance

This is separate guidance; AGENTS.md has not been changed.

## Editing without Save/Confirm
Open VideoHub Presets, then Open a preset. Type its name or select an input,
Keep Current Routing, or a Routing Preset image for an output. Explicit choices
save automatically. Saving/Saved/errors appear beside the editor. If saving
fails, keep the page/draft and correct the problem; do not assume Apply used it.
Switching, picker entry, duplication, locking and Apply first flush pending edits.
Opening/searching/cancelling an input/image picker changes nothing.

Duplicate sits beside the name and creates a new unlocked preset with copied
routes. New Preset asks for a name and 1-to-1, Current Device Routing or All Keep
Current Routing. All modes require real device counts; Current Device Routing
requires complete verified routes. Cancel does not create anything. An editor
restricted to individually assigned IDs must ask an administrator to create and
assign new IDs; creation/duplication is denied before saving, without grant changes.

## Apply is separate
An image reference uses the selected VideoHub output, not its catalog destination.
A differing destination is an informative notice, not a blocking save error. It
never changes the catalog or executes that source preset's extra actions. Applying
an image-bearing VideoHub preset supports browser Apply and Companion's existing
Apply Preset API with a `videohub`-scoped token and all applicable preset, port,
path and nested-reference constraints. Poll the 202 job with the exact initiating
token/session; never automatically repeat Apply. Scheduler/delegated image Apply
remains denied. Numeric/Keep presets still support scheduling; all Keep is a no-op.

Standalone Routing Preset automation instead applies the full saved preset,
confirmed image first then ordered approved actions. It requires its own explicit
routing_presets token scope and exclusive player. Fixed destinations are preserved.
A failed action stops later actions; inspect partial completion before any new
explicit trigger. Never replay a POST after an uncertain response; poll that job.
See API_REFERENCE.md for nullable catalog fields and exact execution identity,
revision, epoch, time-window, token ownership and output restrictions.

## Proposed developer instruction corrections (not installed)
Replace old Save Preset/Save From Device guidance with the autosave/New Preset flow.
Keep page URLs, permission keys, saved IDs and existing Companion action option IDs.
Keep automation catalog/trigger/jobs distinct from browser-only confirmation APIs.
Use real MediaRoutingManager factory lifecycle tests, temporary auth/media and
blocked hardware; confirm all five browser projects and reset markers. Never
interpret HTTP202 as confirmed success. README/API_REFERENCE are the implemented
contract until instruction-file updates receive separate authorization.
