# Task pin verification

`runner.pin_verify` in `project.json`: `lenient` (default) lets an explicitly model/effort-pinned task run if its provider cannot report the resolved session before a model turn; `strict` refuses it.
Set with `ttp config <name> runner.pin_verify strict`.
`task_add` / `task_update` accept `pin_verify: strict` or `lenient` to override the project; `""` clears the override, and it cannot change while the task runs.
Reviews, fixes and continuations inherit a pinned task's override.
A reported model/effort mismatch always refuses.
A strict unverifiable pin blocks the task with the reason, starts no model turn, spends no attempt and does not automatically retry; restore verification or explicitly change the policy, then requeue.
Unpinned tasks are unaffected.
Each check records `session.verification` (`verified`, `mismatched`, `unverifiable`), `pin_verify`, timestamp and resolved fields or error in `run.json` and the run's note.
Providers without a session query are unverifiable.
