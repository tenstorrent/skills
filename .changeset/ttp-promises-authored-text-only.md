---
"tt-project": patch
---

`tt-project`: promises and claims come only from the text the coordinator wrote. A reply's or notify's promise extraction (and the cause-fix, charter-promise and plain-reply checks) now read the authored text, before the harness appends its "Status now:" queue forecast or wake-mode line, and `obligations.commitments` drops those lines itself, so a footer's "ETA 21:00 to start" no longer becomes a promise nobody made. The on-host check's status line lists only the tasks the marked sentences were about, not every unstarted task the message covers. The new `coordinator.status_footer` setting (default true) set to false marks the claim in place and leaves the status to the coordinator's note, for chats that take no status footers.
