---
"tt-project": patch
---

The GLM guard reads a loop variable as its literal list only in a straight-line command: nothing in it, quoted text included, runs text as code, sets a variable another way, changes `IFS` or leaves a loop early, and a variable the shell sets itself (`_`, `BASH_REMATCH`) is never read as a value. After `done` it reads the last value only where the loop surely ran in the same shell. A write or delete naming the variable of a loop over a list the guard cannot read (`$(...)`, `$VAR`) is refused; reading it is not. A python command run with `env -u TMPDIR`, `HOME` or `TTP_RUN_DIR` is no longer proven safe.
