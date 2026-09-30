---
"tt-project": patch
---

`tt-project`: Claude Code workers start with a smaller context, about 2.9k fewer cache-write
tokens a run.

- Claude Code's bundled skills are listed by name only; they can still be called by name. Skills
  from a project's plugin dirs stay fully listed.
- Auto-memory is off for workers; the project keeps its own memory.
- Workers cannot use Workflow, ScheduleWakeup, the Cron tools, RemoteTrigger, PushNotification
  or DesignSync. The no-internet restriction joins the same deny list.
