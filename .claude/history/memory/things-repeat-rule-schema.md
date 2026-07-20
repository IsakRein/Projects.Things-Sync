---
name: things-repeat-rule-schema
description: "CONFIRMED Things Cloud repeat-rule wire format (rrv=4) — everything needed to implement `things add --repeat`"
metadata: 
  node_type: memory
  type: project
  originSessionId: 751e4530-5e19-4bc4-8424-35010a767c38
  modified: 2026-07-20T15:25:08.396Z
---

Confirmed 2026-07-20 by live experiments (committed crafted templates over the wire; the real Things.app on this Mac accepted them, stored the rule, and materialized instances). No public SDK documents any of this.

To create a repeating todo, commit ONE hidden template row: normal todo props plus `rr` = rule dict (plain JSON on the wire; the app converts it to its local plist blob), `icsd` = start date as unix midnight, `icc: 0`, `icp: false`. Do NOT set `tir`/`ti` on the template (the app clears them — templates must not sit in Today). Any real client then materializes instances itself: it creates instance rows (`rt: [template_uuid]`, `lt: true`, sr/tir set, no rr) and updates the template's `icc`/`icsd` bookkeeping. Headless-only accounts would need the CLI to materialize instances the same way.

Rule dict, version rrv=4: `fu` unit (16=day, 256=week, 8=month, 4=year), `fa` interval ("every fa units"), `of` offset list ({dy: N} day-of-month 0-based / -1=last, {dy,mo} yearly with mo 0-based, {wd: N} weekly weekday — Jul 20 2026 Monday = wd:1), `tp` 0=schedule-based / 1=after-completion (app manages `acrd`), `sr`/`ia` start/anchor unix seconds, `ed` end (64092211200 = never), `rc: 0`.

Deadline + reminder on repeats (decoded 2026-07-20 from a user-created sample, wire payload captured): rule `ts` = start offset in DAYS relative to the occurrence anchor — with a repeat deadline, `ia`/`of` describe the DEADLINE date and `ts: -5` means the todo starts showing 5 days before (`sr` = `ia` + ts days; old tax rules used -40/-180); template `dd` is then the sentinel 64092211200 ("deadline comes from the rule"), per-instance dates derived by the client. No deadline -> ts: 0 and the anchor IS the start date. Reminder = template `ato` (alarm time offset), SECONDS since midnight (43200 = 12:00); null = no reminder. The app's own commits send `rr` as a plain JSON dict, same as our writes. Things's convert-to-repeating flow: the existing todo becomes the first instance (update adding `rt`), and a fresh hidden template row is created.

Wire->local field map: rr=recurrenceRule, rt=repeatingTemplate, icsd/icp/icc=instanceCreation{StartDate,Paused,Count}, acrd=afterCompletionReferenceDate, rp/rmd=repeater/repeaterMigrationDate (newer engine, unused by user's app version — legacy rr is what it writes and reads).

BUG NOTE: things-cli build_create_todo sets icc = checklist count (ported from the reference SDKs) — collides with instanceCreationCount; the app's own instance-create commit uses icc: 0 with a 0-item checklist, so checklist count is likely derived, not stored there. Verify before shipping repeat support.

Local app DB (ground truth for experiments): ~/Library/Group Containers/JLMPQHK86H.com.culturedcode.ThingsMac/ThingsData-2IYPG/Things Database.thingsdatabase/main.sqlite, table TMTask, columns rt1_*. Nudge a sync with `open -g -a Things3`; instance materialization took ~70s.

SHIPPED 2026-07-20: `things add --repeat "every|after [N] day|week|month|year [on ...]"` plus `--deadline-early N` and `--reminder HH:MM` (sync.py parse_repeat/build_repeat_rule, tests in tests/test_repeats.py); the icc-as-checklist-count bug was fixed (icc stays 0; verified checklists render fine without it). The 18 Todoist recurring tasks were recreated as native repeats the same day and their one-shot stand-ins trashed. Converting an EXISTING todo to repeating (app's convert flow: update adding rt + new template) is still unimplemented in the CLI.

Related: [[todoist-things-migration]].
