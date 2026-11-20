---
"tt-project": patch
---

`tt-project`: `ttp note --to` keeps an escalation sent with the same words. The same text to the same project at a higher severity than the note already filed (low or normal, then high) is no longer dropped as a duplicate: it raises the stored note's severity, every inbox reader still sees one note, and the reading project's coordinator gets it once more as an escalation. A note still waiting in the outbox is raised in place; the run-queue path and forwarding to another machine carry the raise too. The same text at the same or a lower severity is still dropped.
