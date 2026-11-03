---
"tt-project": patch
---

`ttp drain` counts a live `ttp host-run --detach` run as a detached job: it lists it, waits for it with
`--jobs` and records it as interrupted with `--reboot`, so a planned host cycle does not reboot in the
middle of it. A claim whose supervisor died, from an earlier boot or past its limit never holds a drain.
A `runner.host_commands` entry with `timeout_s` below 1 is now refused like any out-of-range limit,
not rounded up to 1 s.
