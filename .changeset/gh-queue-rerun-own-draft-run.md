---
"tt-project": patch
---

`tt-project`: the workers' gh queue accepts `gh run rerun <id>` (optionally `--failed`), so a flaky CI job on the project's own draft PR can be retried without the user. Just before the rerun, the workstation reads the run's head and reruns it only when that head sha is the head of an open draft PR its own gh user opened, never on the repo's default branch; an unreadable head is refused. `--job`, `--debug`, `run cancel`, `run delete` and `workflow run` stay refused.

`ttp rollout arm` names the level it leaves in force: the level before the rollback, the level set by hand after it, or off.
