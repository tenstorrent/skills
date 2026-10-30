# Harness task

- Your working directory is this project's harness: a git repo of prompts, config and runtime.
- When another harness task works in the harness checkout, yours gets a git worktree of the harness
  of its own: commit there. When your run ends the daemon merges your commits into the harness (and
  restarts the daemon if they change `runtime/`), so do not `ttp restart` from a worktree. Wherever
  you work, stage only the files you changed, by name: never `git add -A`, `git add .` or
  `git commit -a`, which sweep in another task's edits.
- Improve how the project runs: cost, speed, reliability, less need for the user.
- Change the smallest thing that removes the friction. Commit with a one-line reason.
- Your commit in the harness repo is the delivery: the daemon reads the harness from it. The
  harness has no remote, so do not `ttp push` or `ttp push --own` it, and never copy harness files
  into a code branch to publish them (`delivery.backup_remote` backs up code tasks' branches, not
  the harness). Hand off with the commit's hash.
- Rules of this project's own in a template prompt (`prompts/*.md`) go between `<!-- ttp:local -->`
  and `<!-- /ttp:local -->` lines: template upgrades keep those blocks without a merge. Never
  reword upstream's text in place; that is what makes an upgrade need a model.
- This project's own imports in a runtime file go in one block at the end of the file, never inside
  or next to upstream's `from . import ...` lists; leave those lists as upstream ships them.
- Runtime code changes: run `python3 -m pytest -q runtime/tests` if present; keep Python 3.9-compatible.
  Run pytest from the harness root (or your worktree of it): its `pytest.ini` keeps an enclosing
  repository's pytest config and `conftest.py` out and puts `runtime/` on the import path. Add
  other local module paths (such as an `audits/` folder) to its `pythonpath`.
- The daemon picks up prompt and config changes on its own. A runtime change made in the harness
  checkout itself needs `ttp restart <name>`, which rolls `runtime/` back if the daemon cannot start
  with it: check its output. From a worktree, the merge restarts the daemon for you.
- Change only this project's own harness: its `tt-project/` folder (harness and state). Never
  edit, or create a worktree or branch in, the tt-project plugin's source repository or any other
  project's harness, even to port a fix you just made here. Upgrading another
  project on this machine to the installed release (`ttp upgrade <name>`) is not editing its harness; hand edits to its charter, memory, config, state or code are.
- A lesson that would help every project (not just this one) goes in your hand-off as an
  upstream note: a `followups` entry titled `upstream: ...` that describes the problem, the
  evidence and the fix, for the tt-project maintainers.
