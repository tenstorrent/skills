---
"tt-project": patch
---

`tt-project`: `ttp checks` spill hosts may be one host of a machines-list entry with several hosts (`<alias>:<host>`), allowed when the machine is; single-host entries work as before. A project may set its own read-only per-host pre-run hook, `checks.spill_pre_cmd` (no default): exit 0 lets a run spill there, 75 skips the host for now, any other exit is reported as a hook error and the host is skipped; on `checks.spill_beside_device` hosts the hook decides. Checks behind `checks.wrap_cmd` kept here because neither `checks.spill_wrap_cmd` nor `checks.wrap_needs_device: false` is set now say so and name the settings, in `checks.log` and the queue-wait item, instead of blaming a pause.
