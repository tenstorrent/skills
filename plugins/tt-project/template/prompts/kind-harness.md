# Harness task

- Your working directory is this project's harness: a git repo of prompts, config and runtime.
- Improve how the project runs: cost, speed, reliability, less need for the user.
- Change the smallest thing that removes the friction. Commit with a one-line reason.
- Runtime code changes: run `python3 -m pytest -q runtime/tests` if present; keep Python 3.9-compatible.
- The daemon picks up prompt and config changes on its own; runtime changes need `ttp restart <name>`,
  which rolls `runtime/` back if the daemon cannot start with it. Check its output.
- Change only this project's own harness: its `tt-project/` folder (harness and state). Never
  edit, or create a worktree or branch in, the tt-project plugin's source repository or any other
  project's harness, even to port a fix you just made here.
- A lesson that would help every project (not just this one) goes in your hand-off as an
  upstream note: a `followups` entry titled `upstream: ...` that describes the problem, the
  evidence and the fix, for the tt-project maintainers.
