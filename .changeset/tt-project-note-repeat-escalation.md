---
"tt-project": patch
---

`tt-project`: `ttp note --to` keeps a task's escalation. A note from the same task naming the same note ids within 24 h is still dropped as a repeat at the same or a lower severity, but one at a higher severity than the earlier note (low or normal, then high) is filed and surfaced like a first note, on the inbox, outbox and run-queue paths.
