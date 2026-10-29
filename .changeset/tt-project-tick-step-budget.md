---
"tt-project": patch
---

`tt-project`: no single step in the daemon tick can stall the project. Starting a `retry_when`, `start_when` or end-condition probe (its temp-file open and fork) now runs on a small pool of daemon threads, so a stalled `/tmp` open no longer freezes the tick. A probe killed at its 60 s timeout counts as "not yet", with a log note, and only counts as broken after 3 timeouts in a row; kills reach the probe's whole process group and wait at most 5 s. Probing waiting tasks, deferred tasks and ended conditions each get a 60 s budget per tick and resume where they stopped on the next tick. A new low `tick_step_slow` event names the step and what held it (at most once per step per hour), and the tick-overrun stack dump names the item too.
