# Settle hook

`coordinator.settle_cmd` in `project.json` is a shell command the daemon runs before it wakes the coordinator for a batch of events, so the project's own script can settle routine events without a model run.
It is empty (off) by default, and no settle rule ships with the plugin: each project writes its own.
The coordinator can set it from chat with `config_set` (`ttp config --chat-settable coordinator.settle_cmd "<cmd>"` checks a value): one line, at most 2000 characters, `""` turns it off.
It works the same for every provider and for both coordinator modes (session and stateless turn): the daemon runs it before any turn starts.

## When it runs

Only for a batch with no user message and no rerun, after the debounce, burst, batch and event holds, and before the quiet and repeat gates.
Only when every event of the batch (ride-alongs aside) may be settled: one never-settle event in the batch wakes the whole batch as before, with no hook run.
Each event goes to the hook at most once: an event the hook already saw and left wakes the coordinator.
The command runs from the project root, with no model, in its own process group, and is killed after about 20 seconds.
While it runs, the batch waits; events that arrive meanwhile join the next round.

## Input (stdin)

A JSON list, one entry per event:

```json
[{"event_id": 41, "kind": "task_done", "severity": "normal", "text": "#12 tidy docs → done: ..."}]
```

`kind` is one of `task_done`, `upstream_note` or `observation`.

## Output (stdout)

A JSON list of the events to settle, each with a non-empty reason:

```json
[{"event_id": 41, "settle": true, "reason": "docs-only task, nothing to decide"}]
```

An entry with `"settle": false`, or an event left out, wakes the coordinator as usual.
An `event_id` that was not in the input is ignored.
A reason is cut to 300 characters.

## Never settled

The runtime refuses these whatever the hook prints, both before the run and again on its answer:

- user messages (no hook runs for a batch that has one);
- any kind other than `task_done`, `upstream_note` and `observation`, so task failures, Must-act-now items, obligation and promise events, review results, follow-ups and the rest always wake;
- `high` or `critical` severity;
- events from a user or chat source, or marked must-act;
- text naming a promise, an obligation or a must-act item, and events of a task that owns an open obligation;
- review results: an event of a review task, or text naming a verdict or a review result;
- failures: text naming a failure, an error or a crash;
- text mentioning a Restriction;
- outage and machine-condition events: text naming an outage, an unreachable or unhealthy host, a reboot or a machine going down, and observations from a machine, host, health, broker, device, disk or network source or naming a machine condition;
- the hook's own fault observation;
- an event no longer queued when the hook answers.

A later event about the same task (a follow-up, a failure, a review) is a new event and wakes as usual.

## Faults (fail open)

A non-zero exit, a timeout, output that is not the JSON list above (an entry without a reason, a non-integer `event_id`), or more than 256 KiB of output settles nothing: the batch wakes the coordinator as it would without a hook.
Each fault raises one low observation (`daemon:settle-hook`), deduplicated into one issue that counts repeats, and is kept in the `settle_hook_faults` record.

## Where settled events show

- Each settled event is marked handled and logged in the `settle_hook_log` record with its time, kind, task and reason, and in the daemon log.
- The web app's feed shows each settled event with its reason for 24 hours.
- The daily review's self-efficiency audit counts the events the hook settled, their top reasons and the hook runs that ended in a fault.
