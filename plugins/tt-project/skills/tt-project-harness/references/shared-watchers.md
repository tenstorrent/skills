# Shared watchers

A watcher script that workers also run (at start or hand-off) takes a non-blocking lock (`flock -n` on a file under the harness state, or `ttp lock` with a short wait) and exits quietly if busy, re-checks the condition before it acts, and dedupes by condition key.
It also finishes in seconds in a big checkout, well inside a tool call's default timeout.
Use `git status --porcelain --untracked-files=no --ignore-submodules=all` when untracked files do not matter, `--untracked-files=normal` when they do (one line per untracked folder, so a virtualenv, data or build folder is never walked file by file), never `--untracked-files=all`.
Each git or network step gets its own timeout (`timeout 20 git ...`, `GIT_TERMINAL_PROMPT=0` for fetch and ls-remote), and a step that times out prints `{"text": "<watcher>: skipped: <step> timed out after <N>s", "error": true}` and the rest still runs.
