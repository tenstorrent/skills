---
"tt-project": patch
---

`tt-project`: a user order counts as started when its owner task's own run started. An order the coordinator records in a batch gets that batch's new tasks as owners when their spec names it (its key, or the user message it carries), whether the task_add came before or after it. The start stamp is the first owner run's start, not the check that saw it, also when a receipt closes the order before any check ran. The daily audit's late-order count leaves out orders whose owners all wait by design past the start limit (start_after, not_before, a start_when probe) or were all cancelled unrun. `## Must act now` lists each open order still without an owner task 10 minutes after its message, until an owner is attached or a reply goes to its chat.
