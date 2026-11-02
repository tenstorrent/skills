---
"tt-project": patch
---

`tt-project`: a worker's normal `ttp notify` from a run that ends blocked, waiting, failed or needs_review is no longer posted at run end. It goes to the coordinator as a `worker_notify` item that rides on that run's hand-off turn. An ask, reply or notify naming the task, or a `task_update` with text `notify dropped: <why>`, within `notify.hold_s` (default 900 s) means it is not posted; otherwise the daemon posts it unchanged and model-free. The hold is kept in the database, so a daemon restart neither loses nor doubles it. Low notifies, notifies from done runs and notifies from a hand-off that starts no coordinator turn (a waiting one, such as a sign-in link or device code, or a routine or quiet failed or needs_review one) are posted at run end as before. `ttp notify` now says where a normal notify goes.
