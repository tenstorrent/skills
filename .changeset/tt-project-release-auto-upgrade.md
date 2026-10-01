---
"tt-project": patch
---

`tt-project`: each project's daemon compares the installed release (`~/.tt-project/lib/current`)
with its harness runtime at start and then hourly. While a newer release is installed, `ttp status`
and the web app show `tt-project <installed> available, harness on <current>`; an older release is
never offered. With `upgrade.auto` on (the default) the daemon starts its own detached
`ttp upgrade <name> --auto` when no push or other upgrade is in flight. That restarts the daemon,
keeps running workers and posts one low notify. Each release is tried once: a merge conflict queues
one harness task and is not retried. `ttp config <name> upgrade.auto false` opts out. `ttp status`
without a name shows the project of the current folder. The skill reruns `ttp setup` when the
installed `ttp` is older than the plugin.
