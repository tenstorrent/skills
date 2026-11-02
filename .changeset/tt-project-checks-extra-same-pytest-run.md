---
"tt-project": patch
---

ttp checks: an extra command after `--` that is a project pytest check in another spelling (other test-file order, `-q`, a leading `PYTHONPATH=$PWD`, or `--rootdir=.`/`--confcutdir=.` where pytest would use the worktree top anyway) is not run a second time; the line that says so names the project check that covers it. Anything that selects or changes tests (`-k`, `-m`, node ids, other files, env or options) still runs.
