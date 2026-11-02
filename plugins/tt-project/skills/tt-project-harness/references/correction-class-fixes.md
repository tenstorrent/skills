# Correction class fixes

A correction (a message pointing out something the project should have caught) gets a class fix: a `harness` task (or an `upstream:` note) for the detector, rule or default that would have caught it.
The Course check keeps two model-free lines on those fixes (runtime/ttp/coursecheck.py, both under `coordinator.course_check.recurring`; 0 turns them off).

## Recurring mistake

`recurring mistake` fires when a later correction has the class of an earlier one that got a fix task: the fix did not hold.
Find why it missed this case and fix that in a task the same turn.
When the fix task was cancelled because another task or commit re-landed the work, record that one: `ttp task <name> superseded <cancelled id> '#<done task>'`, or `ttp task <name> superseded <cancelled id> <sha>` for a commit in the harness, the project root or `--repo`.
The line stops while that task stays done (or the commit exists).
Open, failed or cancelled fix tasks, another project's task and unknown commits are refused.

## Report-only follow-up

`report-only follow-up` fires when every enabled schedule citing the correction (`correction #<msg>`) or its fix task (`#<id>`) in its description or payload is an `llm` schedule.
Such a watcher writes reports; nothing turns a report line into a queued task or an alert, so the class comes back unseen.
The line stays open until a model-free gate checks that the reported action followed, any one of:
- an enabled `command` (or built-in watcher) schedule citing the correction or the fix task, whose script prints an observation when the action did not follow (see [watchers.md](watchers.md));
- a done `harness` or `code` task citing the correction or the fix task that adds a detector with a test (not the fix task itself and not a run of the schedule);
- a recorded later fix: when the report-only fix task was cancelled and another task or commit re-landed the work as a real gate, `ttp task <name> superseded <cancelled id> <sha|'#<done task>'>`.

Keep the llm schedule if its judgement is worth the cost; the gate is what makes the follow-up count.
