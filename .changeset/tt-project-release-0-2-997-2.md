---
"tt-project": patch
---

tt-project 0.2.997: the daemon's dispatch step starts every ready task with a free place in one pass, even when git is slow on a loaded machine. Each start used to read the project root's head, branch and status again and fetch origin again, so on a machine where one git call takes seconds the step's budget ran out after a start or two and the rest waited a tick each. Within one dispatch pass those reads and the fetch now run once, and a git command that may move the root's checkout or refs drops the kept answers. With 4 ready tasks and git at 0.4 s per call, the pass went from 75 git calls in about 45 s to 50 calls in about 28 s.
