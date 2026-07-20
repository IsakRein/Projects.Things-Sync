---
name: todoist-things-migration
description: One-time Todoist -> Things migration done 2026-07-20; mapping conventions used
metadata: 
  node_type: memory
  type: project
  originSessionId: 751e4530-5e19-4bc4-8424-35010a767c38
  modified: 2026-07-20T15:42:44.198Z
---

On 2026-07-20 a one-time Todoist -> Things Cloud migration was performed via `td` + `things` CLIs (5 areas, 30 projects, 207 todos). Conventions chosen, likely relevant if Things-Sync grows a continuous mirror:

- Todoist top-level project -> Things area; nested project -> Things project in that area, titled with the sub-path joined by dots (Health/Looks/Teeth -> project "Looks.Teeth" in area Health).
- Todoist Inbox -> Things Inbox; due date -> when; Todoist deadline -> deadline; subtasks -> checklist items.
- Recurring tasks initially became one-shot todos, but later the same day repeat support was added to things-cli (see [[things-repeat-rule-schema]]) and all 18 were recreated as native Things repeats; the one-shot stand-ins were trashed.
- atlas "Todoist expand" pre-expanded instances of recurring routines (Morning 1/2, Night, In-Bed, Downtime, Journal, Update personal finance) were treated as machine-generated duplicates and skipped (39 of them).
- Empty Todoist projects (pm/Notion mirrors with no tasks) were not recreated.
- Full todoist-id -> things-uuid mapping was in the session scratchpad (migration-log.jsonl), which is temporary.
- Todoist was left untouched (nothing archived or deleted there).

Todoist RETIRED later on 2026-07-20: empty projects were also imported (13 areas / 79 projects, full tree), td CLI (npm + brew) and Todoist.app uninstalled on mac + mini (air was offline — its cleanup happens via the updated bootstrap.sh), todoist-cli skill replaced by a new things-cli skill, bootstrap.sh purged of Todoist (formula, cask, td token seeding) with THINGS3_* added to the secrets check. STILL PENDING (Isak does these manually): removing the Todoist jobs (listen/expand/mirror/label-track/autostart) from the atlas worker and the Todoist integration from pm — until then TODOIST_* keys must stay in ~/.envrc on all machines.
