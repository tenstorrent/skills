---
"tt-project": patch
---

`ttp host-run`: a `runner.host_commands` entry may be `{"cmd": "<command>", "timeout_s": N}`, its own
limit up to 3600 s (plain entries keep `runner.host_command_timeout_s`, at most 600 s); an over-cap or
malformed entry is refused and named, never clamped, and `--timeout S` asks for less. `ttp host-run
--detach` starts the command in a supervisor of its own that outlives the worker's tool call, the worker
and a daemon restart, and writes `state/host-run/<id>.out.json` and `<id>.rc`; `--status <id>` (0 ended,
1 running, 4 unknown or lost) and `--result <id>` poll it.
