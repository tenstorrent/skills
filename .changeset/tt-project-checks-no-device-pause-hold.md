---
"tt-project": patch
---

checks: `ttp checks --no-device` and `checks.no_device_cmds` run CPU-only commands without checks.wrap_cmd and its lane; a wrapped command refused by a paused lock ends "held by pause on <resource>" (exit 75, held_by_pause), not a failure. `ttp lock 'A|B'` takes an unpaused alternative and never waits on a paused one.
