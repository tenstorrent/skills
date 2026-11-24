---
"tt-project": patch
---

Reboot accounting. The wait for a replacement of a run a reboot cut counts from this boot's daemon start, not the run's last sign of life, so host downtime is left out. The host line credits a planned reboot to a project only from a host-cycle record or a machine-change ledger entry; a device broker power-cycle notice reads as the broker's, also for boots an older daemon stored with this project's name.
