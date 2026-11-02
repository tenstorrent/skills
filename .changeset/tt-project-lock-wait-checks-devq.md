---
"tt-project": patch
---

`tt-project`: the `lock-wait:<lock>` alert (past `locks.wait_alert_min`, one per lock per wait, cleared when the wait ends) now also covers `ttp checks` slot waiters and tasks waiting on a devq job. A check-slot wait alerts only while a holder is not judged alive (dead, reused pid or not judgeable): a 7-day replay of the check-slot wait log gave 9 episodes of 30 min or more (up to 4 a day), all queueing behind live checks. `ttp devq probe` now prints how long a queued job waited, how many jobs are ahead and which job, task and pid run (alive or gone); the daemon records it from waiting tasks' probes and raises `lock-wait:devq:<runner>`. The alert text and docs state that the daemon's tick releases a dead holder's lock within about 2 min.
