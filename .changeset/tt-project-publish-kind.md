---
"tt-project": patch
---

`tt-project`: a follow-up or task that publishes a code task's branch (a `pr_branch:` line, or a `ttp push --own` step onto the branch of the code task it names or continues) runs as kind `code`, with that `pr_branch:` and a `continues:<owner>` link to the code task whose branch it is, and does not copy a non-code parent's tier. An owner that cannot be worked out unambiguously is refused at task_add (a refused continue hop stays pending for the coordinator with the reason), and a queued non-code task whose spec has a `pr_branch:` line is corrected or blocked before dispatch.
