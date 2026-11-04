---
"tt-project": patch
---

`tt-project`: task_add no longer chains a task after another for a path prefix or glob. The automatic "needs another task's new file" chain counts only a complete path that the base branch lacks and that the other task's spec says it creates (or its branch adds). A prefix such as `tests/test_x_` of `tests/test_x_*.py` never chains; the overlap note for shared files is unchanged.
