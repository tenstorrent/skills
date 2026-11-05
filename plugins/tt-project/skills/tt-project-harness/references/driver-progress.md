# Driver progress reports

A detached or looping driver (a filler, a recheck loop, a guard, a series runner) that a worker or schedule starts keeps its refusals and failures inside it: the daemon only sees its process running. So every such driver reports each counted result, refusal, stall and failure with one bash line:

```bash
ttp progress report refusal --key nightly-filler "lock build-dir busy (exit 75)"
ttp progress report failure --key nightly-filler "step 3 exit 4"
ttp progress report result  --key nightly-filler "batch 12 of 40 done"
```

- Kinds: `result` (a counted result: it ends a streak), `refusal`, `stall` and `failure` (they count toward one).
- `--key` names what the report is about, the same on every loop. Inside a `ttp detach` job it defaults to `<task>/<job name>`.
- A refusal on a busy lock adds `--resource <name>`: its holders now (task and run, or an external label, with pid and live or stale) are recorded from the lock state `ttp lock --probe --json` reads, or `unknown` when it cannot be read. `--holder "<who>"` records what the driver knows instead. The stall observation and `ttp status` name the holder, so nobody needs a run to find it.
- The project comes from `TTP_PROJECT` or the folder it runs in, as for other `ttp` commands.
- It exits 0 with the daemon down, and also when the line cannot be written (it says so on stderr), so a driver under `set -e` keeps going. It exits 2 only with no project or no key.

Each report is one JSON line appended under a lock to `state/progress.jsonl`: `{"ts", "key", "kind", "reason", "source"}`, plus the driver's `pid` (the caller's parent), `boot`, inside a `ttp detach` job its `job` (.rc path), `task` and `run`, and a refusal's `resource` and `holder` when given or found. Each line is synced as it is written, and the file rotates to `progress.1.jsonl` past 256 KiB.

Readers (runtime `progress.py`):

- The daemon reads it every minute. A key whose last `waiting.progress.stall_after` reports (default 5) are refusals, stalls or failures with no result, the last one within `waiting.progress.window_s` (default 24 h), raises one keyed observation to the coordinator with the reasons, once per streak. A result ends the streak, and a new streak raises again.
- `ttp status` and the web app show such a driver as stalled, not running, while its process still runs: the `ttp detach` job is still running, or else the reporting pid is alive on this boot.
- An obligation owner waiting on its own `ttp detach` job: each `result` the job reports keeps the owner's 2 h durable-progress clock from running out while the job still runs, so a producing job raises no `obligation_overdue`. Report each checkpoint or finished cell, not a heartbeat. A job that writes one file per finished unit can instead be started with `ttp detach <name> --results '<glob>' -- <command...>` (the glob relative to the job's working dir, kept across a relaunch): each new matching file, changed after the job started and within 2 h, counts as a result from that job, with no change to the driver. A live job with neither is named in the overdue alert with both fixes, and a job an obligation owner starts without `--results` gets a reminder in its start notice.

## Spend cap for a job that calls a paid API

`ttp detach <name> --spend-cmd "<cmd>" --spend-cap <usd> [--spend-every <s>] -- <command...>` puts a model-free cap on a detached job's spend (runtime `spendcap.py`). The two options go together; one without the other, a cap that is not a positive number or a cadence under 60 s is refused, and they are for local jobs only. A job started without them is never checked.

- The command prints one number: the job's spend so far in USD (a leading `$` is fine). It reads it from the provider's usage API or the job's own cost log, and may run under `ttp secret env` for its key.
- While the job runs, the daemon runs the command every `--spend-every` seconds (default 300) from the project root, within 20 s. Once a read reaches the cap, it stops the job's process group as `ttp detach --stop` does, records a `failure` progress report keyed by the job's name ("spend cap") and raises one normal event to the coordinator naming the job, the read and the cap. The job is not checked again.
- A read that fails (non-zero exit, timeout, or output that is not one number) is counted. Three in a row raise one event and leave the job running, uncapped until a read succeeds. A good read resets the count.
- Logs, events and status carry only the number read and a fixed failure reason, never the command's output or environment.

The cap is a backstop, not the budget: still turn on prompt caching, cap each unit's calls and cost in the driver, and keep clear of a shared key's daily limit.

## Who signals a detached job

A `ttp detach` job outlives the run that started it while an open task still waits on it: the daemon stops a job only once its task has ended (cancelled, failed, done, or blocked without naming the job), or on `ttp detach --stop`, a spend cap, a reboot relaunch the task no longer needs or a fix past its timeout. That stop freezes the job's group (SIGSTOP), then kills it (SIGKILL) and writes `<name>.lost` with why: such a job never gets an `.rc`.
- `ttp detach --stop <name|rc> [--why "<reason>"]` is allowed for an ended task's job, for the job's own task from any of its runs (this one or an earlier one: a stalled driver an earlier run started), or for an open priority-1 task whose resources the job's locks hold; anyone else, or a caller with no task, is refused (exit 3) while the job's task is open. An owner's stop records who stopped it, the run that started the job, the caller's run and the reason in `<name>.lost` and the stop event. A stopped job is never relaunched at boot. A run's detached `ttp checks` (`<run>/checks.rc`, or `checks` from that run) are stopped by the same rule: SIGTERM to their group, then SIGKILL, recorded as a stop event. The daemon also stops the detached checks of a task that handed off done, unless its hand-off names them or an open task or another checks run that joined them still waits on their result.
- Every signal tt-project sends a detached job, its detached checks or its detached push adds one line to that job's own log and one JSON row to `state/job-signals.jsonl`: the job, pid or group, signal, code path, why, sender (component, pid, command) and time.
- A `ttp lock` that waits for a slot logs the sender (pid, uid, command line) of a SIGTERM, SIGINT or SIGHUP it receives, to stderr (the job's log), its run's progress.md and `state/job-signals.jsonl`, then exits 128 plus the signal number as before.
- So an `.rc` of 143 with none of these lines means a process outside tt-project sent SIGTERM to the job's command alone (the wrapper lived to write the code); a SIGTERM to the whole group leaves no `.rc`.

## Stopping a driver started without `ttp detach`

A driver started some other way (`setsid` and a pid file, a project's own launcher) is stopped with `ttp detach --stop-pids <file>`, run by the task that started it, so the stop never depends on the driver's own loop reading a STOP file. The file lists one pid per line; a `start=<token>` word on the line (the start ticks from `/proc/<pid>/stat` field 22) pins it to that process.
- The file's resolved path must lie under one of the task's results dirs: a run dir of the task, or a folder `t<id>`, `task<id>` or `task-<id>` inside the project root. A symlink out of them is refused.
- Each live pid must run as you, carry the task's own `TTP_TASK` and the project's `TTP_PROJECT` in its environment, and still be the recorded process (its start token, else started no later than the file was last written). The caller, its ancestors, running workers and a process holding a lock slot for another holder are refused.
- It sends SIGTERM, then SIGKILL after 10 s, only to the listed pids (their children are not looked up). A pid that is gone is skipped. Any refusal refuses the whole file (exit 3) and stops nothing. In a sandbox the daemon checks again and stops them, and writes the outcome to `<file>.stopped`. The command is Linux-only: it reads `/proc` for the pids' identity and start, so a host without `/proc` (macOS) refuses every listed pid as unreadable and stops nothing.

## Ending an ended task's leftovers

`ttp leftover-procs --stop <task> [--pid N ...]` ends what a done, failed or cancelled task left running, such as a test script still holding a checks slot or a lane. It stops only processes it can attribute to that task from what the harness set when the run started: your user, this project's `TTP_PROJECT`, the task's `TTP_TASK` and a `TTP_RUN_DIR` of one of its runs. A running task's, another task's, project's or user's process, a `ttp detach` job's, one using a device, and any without those markers are refused (exit 2). It never stops the daemon. A `ttp checks` command counts as its task's, and a stopped process-group leader takes its group with it unless a member is protected or not the task's (then only the leader goes, exit 2). It prints each pid stopped and the locks freed. The daemon does the same by itself once such processes are the only ones holding a lock.
