---
"tt-project": patch
---

`tt-project`: queue-wait items stop paging the coordinator for the same wait. A task and queue is one episode from its earliest start, so a start time that flips between the checks queue and the task's hand-off never reopens it, and a gap under 30 minutes keeps it (its unread item is parked, then revived). An unacted wait is raised again only when its cause (queue, resource, host-held, options left) changes or the wait has doubled since the last raise, not at every threshold mark. A checks wait held by full host-wide checks slots, which no coordinator action lifts, is raised at normal and rides along with the next turn. A must-act item that rides along no longer skips the coordinator's burst, batch, event and turn-guard holds.
