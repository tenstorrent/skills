# Harness task

- Your working directory is this project's harness: a git repo of prompts, config and runtime.
- Improve how the project runs: cost, speed, reliability, less need for the user.
- Change the smallest thing that removes the friction. Commit with a one-line reason.
- Runtime code changes: run `python3 -m pytest -q runtime/tests` if present; keep Python 3.9-compatible.
- The daemon picks up prompt and config changes on its own; runtime changes need `ttp restart <name>`,
  which rolls `runtime/` back if the daemon cannot start with it. Check its output.
- A lesson that would help every project (not just this one): add a `followups` entry titled
  `upstream: ...` describing it for the tt-project maintainers.
