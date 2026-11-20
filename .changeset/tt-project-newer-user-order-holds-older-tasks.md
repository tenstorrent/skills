---
"tt-project": patch
---

A newer user order on a machine or resource now outranks older tasks there, without relying on the model. When the coordinator adds a task for a user's order (its new `user_msg` field, or a spec citing that turn's user message) and the task names resources, it becomes each resource's latest user order. Until the coordinator re-specifies them, or the order's task ends, every other task created or last re-specified before that message is held off those resources. `ttp lock` and `ttp lock --probe` refuse it with exit 75 and name the order, including a lock that was already waiting. Dispatch also holds it back, so the order's task gets the resource first. Running runs get one steer.md update telling them to stop conflicting steps. The coordinator's digest lists these older tasks so it can cancel or re-spec them. Unrelated resources and tasks re-specified after the order are not affected.
