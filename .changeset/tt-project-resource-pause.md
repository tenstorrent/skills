---
"tt-project": patch
---

`tt-project`: a shared resource can be paused so the harness enforces it, not the prompt.
`ttp pause <name> --resource <r> [--reason ...]` and `ttp resume <name> --resource <r>` (also the
coordinator's new `resource_pause` action and the web app) keep the pause in the project database,
so it outlives daemon restarts and reboots. While a resource is paused, no task that lists it is
dispatched: it stays queued with a note and spends no attempt, and starts by itself once resumed.
`ttp lock <r>` refuses it with exit 75, also for a task that holds the resource for its whole run
and for a command already waiting for it. Running workers whose task uses the resource get a
mid-run update. Paused resources show in `ttp status`, the web app and the coordinator's STATE. The
coordinator may lift only a pause it set itself, unless the turn carries the user's message.
