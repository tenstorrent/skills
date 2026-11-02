---
"tt-project": patch
---

`tt-project`: `ttp schedules <project> run <schedule>` runs a command schedule now. The daemon runs it, so it also works from a worker sandbox that cannot run the schedule's tool itself. It is a normal timed run: same runner, environment, timeout, heal and observation handling, and it records last_run. The command prints the schedule's output, ends with the command's exit code, and writes `exit <n>` to stderr. It refuses llm schedules and names that are not in the config with a one-line message. It joins a run that is already going instead of starting a second one. A command schedule that is new or re-enabled and has never run now runs at the next daemon pass. Before, it waited a whole `every`, or until its `at` time.
