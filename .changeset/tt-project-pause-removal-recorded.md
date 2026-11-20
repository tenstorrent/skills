---
"tt-project": patch
---

`tt-project`: a resource pause never disappears silently. Every path that drops or replaces one (`ttp resume --resource`, the coordinator's `resource_pause`, the web app, a pause's health end or user end, the end migration, a shared pause's resume, a quarantined unreadable pause row, and any other write of the pauses) leaves a `resource_pause_removed` or `resource_pause_replaced` event naming the path, the actor and the reason. An open machine-ledger entry whose undo resumes a project pause that is gone raises one normal `pause-gone:<id>` alert naming who removed it; it clears once the entry closes or the pause is back.
