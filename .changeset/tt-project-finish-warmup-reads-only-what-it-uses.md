---
"tt-project": patch
---

`tt-project`: daemon: a finish's git warm-up reads file overlaps only for the lone `continue:` hop the finish may queue itself, not for every follow-up (the coordinator's task_add reads those outside any finish). Measured on a 2,000-commit project with 200 task branches: the warm-up went from 1,727 git calls to 23, and the code task's finish from 15.1 s to 0.14 s (50.4 s to 0.59 s with each git call taking 20 ms); it still commits in its first round with no live git under the write lock, and strict rounds take as long as the one-transaction finish.
