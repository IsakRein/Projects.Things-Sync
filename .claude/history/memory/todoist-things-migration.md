---
name: todoist-things-migration
description: One-time Todoist -> Things migration done 2026-07-20; mapping conventions used
metadata: 
  node_type: memory
  type: project
  originSessionId: 751e4530-5e19-4bc4-8424-35010a767c38
  modified: 2026-07-20T15:25:11.731Z
---

On 2026-07-20 a one-time Todoist -> Things Cloud migration was performed via `td` + `things` CLIs (5 areas, 30 projects, 207 todos). Conventions chosen, likely relevant if Things-Sync grows a continuous mirror:

- Todoist top-level project -> Things area; nested project -> Things project in that area, titled with the sub-path joined by dots (Health/Looks/Teeth -> project "Looks.Teeth" in area Health).
- Todoist Inbox -> Things Inbox; due date -> when; Todoist deadline -> deadline; subtasks -> checklist items.
- Recurring tasks initially became one-shot todos, but later the same day repeat support was added to things-cli (see [[things-repeat-rule-schema]]) and all 18 were recreated as native Things repeats; the one-shot stand-ins were trashed.
- atlas "Todoist expand" pre-expanded instances of recurring routines (Morning 1/2, Night, In-Bed, Downtime, Journal, Update personal finance) were treated as machine-generated duplicates and skipped (39 of them).
- Empty Todoist projects (pm/Notion mirrors with no tasks) were not recreated.
- Full todoist-id -> things-uuid mapping was in the session scratchpad (migration-log.jsonl), which is temporary.
- Todoist was left untouched (nothing archived or deleted there).
