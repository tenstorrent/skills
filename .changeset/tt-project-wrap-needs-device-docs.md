---
"tt-project": patch
---

`tt-project`: the guide and the push-checks reference say when to use `checks.no_device_cmds` or `ttp checks --no-device` (commands that never need a device skip `checks.wrap_cmd` everywhere and may spill) and when `checks.wrap_needs_device: false` (a wrapper that only serializes CPU test runs: kept here, dropped on a spill host), how the two combine, with one example each.
