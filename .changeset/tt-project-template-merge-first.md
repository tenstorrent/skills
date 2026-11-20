---
"tt-project": patch
---

`tt-project`: the harness task that finishes a template merge after an upgrade now joins the urgent lane and leads it: it starts at the next free worker slot, ahead of queued priority-1 work, and may take the urgent extra or reserved slot, so a project picks up a fixed release within minutes instead of hours. Pauses, resource locks, spend caps and the budget gate still hold it, and only one such task exists at a time.
