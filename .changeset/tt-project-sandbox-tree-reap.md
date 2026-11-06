---
"tt-project": patch
---

`tt-project`: a run's sandbox processes end with the run. When the agent ends, the runner ends what it
left in its process group (a pid the hand-off lists in `leftover_procs` stays) and the run's own
orphaned `bwrap` trees with their proxy listeners, which run in their own session and were adopted by
the init process before. The daemon's sweep, first at start, ends the project's orphaned sandbox trees
of ended runs and writes a `sandbox_trees_stopped` receipt; `ttp leftover-procs --sandboxes
[--dry-run]` does it on request. A `ttp detach` job's tree, a live run's, another project's or user's
and one with a protected or device member are never touched. A test pins that no shipped default
starts a sandbox.
