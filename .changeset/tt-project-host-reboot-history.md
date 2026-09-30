---
"tt-project": patch
---

Record each reboot of the host: when it booted, the earlier boot's last heartbeat, the runs it cut short and the resources held then. The reboot alert counts reboots in the last 24 h and names what was held; from the third reboot with lost runs in a day, one high alert a day says the host looks unstable. `ttp status`, the web header and the coordinator's digest show the last day's reboots while there were any.
