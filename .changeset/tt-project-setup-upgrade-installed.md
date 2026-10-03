---
"tt-project": patch
---

`ttp setup` and `ttp upgrade` run from a project's harness copy (the `ttp` first on a run's PATH)
now hand over to the installed stable ttp in `$TTP_HOME/lib/current`, so an older harness runtime
no longer rewrites its own files mid-upgrade and crashes. With nothing installed they stop and name
where the installed copy should be.
