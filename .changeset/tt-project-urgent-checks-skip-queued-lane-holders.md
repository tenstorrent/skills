---
"tt-project": patch
---

`tt-project`: an urgent or priority-1 `ttp checks` run now takes a place in its repository's checks lanes at once and queues for a host-wide checks slot, where it preempts routine runs as before. It still takes the one over-cap lane first when that lane is free and the host has room, so it runs past a cap full of routine runs as before. Before, with the project's cap of running checks (checks.test_project_max) full, it could wait outside the host queue for hours behind lower-priority runs that only held their places while they queued for a host-wide slot. How many checks actually run per project and per host is unchanged: a run still starts only within checks.test_project_max and its repository's lanes, unless it holds a reserved urgent slot. A run waiting for its lane also reads checks.slots again at each poll, so a raised limit takes effect without a restart.
