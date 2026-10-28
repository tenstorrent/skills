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
