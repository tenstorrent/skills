---
"tt-project": patch
---

`tt-project`: A project no longer only pauses when a shared resource keeps failing; it moves the work.
Each user now has one machine list, `~/.tt-project/machines.json`, managed with `ttp machines add <alias>
--tags device,... [--note ...]`, `ttp machines list` and `ttp machines remove <alias>`, and every project
shows it in the coordinator digest. The charter's Resources section says which machines a project may use,
and project creation offers to record machines and that choice. The digest gains a Resource trouble section
for resources whose tasks failed at least twice in 24 h (runs crashed, stalled, timed out or lost, failed or
blocked hand-offs, host reboots while held), with the machines that share its tags; the daemon starts one
coordinator turn per such episode, and only for a resource with open tasks. Many waits alone show only as a
hint (a busy resource is not a broken one), dependency blocks do not count, and an episode ends only after a
day without failures, so a count at the threshold does not start one again and again. The coordinator moves the affected tasks to an allowed healthy machine
(`task_update` now takes `resources` and `exclusive`, and a waiting task moved this way starts at once),
records the decision and tells the user, and asks only when the charter allows no alternative.
