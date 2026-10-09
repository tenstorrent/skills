# Harness task

- Your working directory is this project's harness: a git repo of prompts, config and runtime.
- Improve how the project runs: cost, speed, reliability, less need for the user.
- Change the smallest thing that removes the friction. Commit with a one-line reason.
- Your commit in the harness repo is the delivery: the daemon reads the harness from it. The
  harness has no remote, so do not `ttp push` or `ttp push --own` it, and never copy harness files
  into a code branch to publish them (`delivery.backup_remote` backs up code tasks' branches, not
  the harness). Hand off with the commit's hash.
- Rules of this project's own in a template prompt (`prompts/*.md`) go between `<!-- ttp:local -->`
  and `<!-- /ttp:local -->` lines: template upgrades keep those blocks without a merge. Never
  reword upstream's text in place; that is what makes an upgrade need a model.
- Runtime code changes: run `python3 -m pytest -q runtime/tests` if present; keep Python 3.9-compatible.
- The daemon picks up prompt and config changes on its own; runtime changes need `ttp restart <name>`,
  which rolls `runtime/` back if the daemon cannot start with it. Check its output.
- Change only this project's own harness: its `tt-project/` folder (harness and state). Never
  edit, or create a worktree or branch in, the tt-project plugin's source repository or any other
  project's harness, even to port a fix you just made here. Running `ttp setup` or
  `ttp upgrade <name>` to deploy a release to another project on this machine is not editing its
  harness; hand edits to its charter, memory, config, state or code are.
- A lesson that would help every project (not just this one) goes in your hand-off as an
  upstream note: a `followups` entry titled `upstream: ...` that describes the problem, the
  evidence and the fix, for the tt-project maintainers.
