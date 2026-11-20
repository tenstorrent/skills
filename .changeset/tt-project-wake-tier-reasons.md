---
"tt-project": patch
---

Every wake of a waiting task records why it woke (a failed check or job, a probe pass, a spec change,
a light run that asked for more, a timer), and the run note keeps it with who chose the tier.
An escalated wake for a spec change that slept with the wait asks Jev (its `wake_tier` use) whether
the run needs judgment, and runs light when it does not, instead of at the task's own tier; a failure
to fix keeps its tier by rule. `ttp stats --wakes` shows task wakes per day by reason with their $.
