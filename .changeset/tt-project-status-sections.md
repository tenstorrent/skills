---
"tt-project": patch
---

`tt-project`: `ttp status` lists each task once in clear sections: 'stuck, the project is on it' holds only blocked tasks the project must move and stalled drivers; one 'landing:' line counts the pushing and in-review tasks with the oldest age and names only a task past its stage's bound (`coordinator.review_stall_s`, `delivery.push_landing_alert_h`); one 'waiting:' section holds the urgent, waiting, deferred, logged-out and network-held tasks with the urgent mark and wait inline. Routine diagnostics (checks lanes, test slots, spill and join lines, free shared locks, self-review, health and coverage when fine, host reboots with no lost runs) print only with `--verbose`; a long lane wait, a held or mismatched shared lock, an overdue self-review, a failing health check, a coverage gap and runs lost to a reboot still print by default.
