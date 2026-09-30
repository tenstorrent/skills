---
"tt-project": patch
---

`tt-project`: a run lost to a host reboot no longer uses up an attempt, no longer lifts a light
review to standard, and is retried at once instead of after a delay. A task lost to
`budget.max_reboot_losses` reboots (default 3) is blocked as possibly causing them, with a
high-severity event for the coordinator. The resumed worker is told the host rebooted and when,
that detached jobs, /tmp files and device state are gone, and sees the lost run's last 5 notes.
Waiting tasks that handed off before the boot are due on the daemon's first tick, past their timer
and probe, unless their hand-off set `"survives_reboot": true`.
