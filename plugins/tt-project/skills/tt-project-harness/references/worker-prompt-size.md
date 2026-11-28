# Worker prompt size

A worker's prompt is `system.md` (the same for every run of a project) plus `prompt.md` (the run's own).
Every byte is paid on every run, so both are held to what the task needs.

- **Charter digest.** Each Goals or Resources section shows workers at most `WORKER_WHOLE_CHARS` whole. The
  blocks past it are listed in `worker_context`'s `charter_left` and reach only a task whose title or spec
  names a path, resource or task number they name (`worker_rules`). Restrictions are never cut and stay verbatim
  in the binding block.
- **Memory.** Pinned entries over their room are pointers; past `MEMORY_POINTERS_LISTED` they are one count
  line with a `grep -il` hint on the memory folder.
- **Cap.** `worker.prompt_max_bytes` (default 60000; 0 turns it off) bounds `system.md` + `prompt.md` less the
  binding restrictions and the task section. Past it `prompts._fit` cuts whole lines from the end of, in
  order: the memory block, the context block, the task rules. worker.md's own blocks, the restrictions and
  the task are never cut. A forked run (the coordinator's session copied) is not cut or counted.
- **Watching it.** `promptdrift.size_line` gives the median bytes per role over the last day and the runs
  over the cap; it is part of the daily review.

Raise the cap in `config.json` under `worker` only when a task really needs more text; trimming the memory
and charter text that every run carries is the better fix.
