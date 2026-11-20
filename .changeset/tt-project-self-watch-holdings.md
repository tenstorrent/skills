---
"tt-project": patch
---

`tt-project`: the self-watch line now also shows what a project holds and installed past its own host: a read-only ssh probe about every 2 minutes of each machine a running task names (load against cores, processes created per second, memory and disk; never a machine under a pause or hold, unreachable ones skipped), the crontab entries and systemd user units the project installed, with any new entry that appears while one of its runs runs flagged with that run's id, and the locks, keeper holds and detached jobs it holds. A stopped runaway's locks are released at once, and the stop text says how many.
