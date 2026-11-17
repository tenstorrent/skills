---
"tt-project": patch
---

`ttp task` refuses to change another project's tasks from inside a run (`list` and `show` still read them). A dry-run worker replaying a real task's spec once recorded that spec's `superseded` step in the real project.
