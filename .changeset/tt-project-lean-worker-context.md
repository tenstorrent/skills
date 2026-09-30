---
"tt-project": patch
---

`tt-project` 0.2.15: batch release for the changes since 0.2.14 — review tiers follow the size and risk
of the diff, plan windows pace from average worker concurrency, a replacement task can continue a dead
one, allowlisted MCP servers stay in isolated workers, harness tasks edit only their own harness,
every worker shares one cached system prompt (the kind's rules moved into the per-task prompt), and
Claude Code workers start with a smaller context, about 2.9k fewer cache-write tokens a run:

- Claude Code's bundled skills are listed by name only; they can still be called by name. Skills
  from a project's plugin dirs stay fully listed.
- Auto-memory is off for workers; the project keeps its own memory.
- Workers cannot use Workflow, ScheduleWakeup, the Cron tools, RemoteTrigger, PushNotification
  or DesignSync. The no-internet restriction joins the same deny list.
