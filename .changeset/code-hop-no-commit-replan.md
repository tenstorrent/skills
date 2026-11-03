---
"tt-project": patch
---

Continue chains: a code hop's progress is its commits only. They are counted in the hop's own worktree
or clone (also where the project root is no git repository and no branch is recorded), its branch and
pr_branch, and the shas its hand-off names, keeping only commits whose committer time falls inside the
hop's runs. New or changed metrics alone no longer count for a code hop, so one that committed nothing
never has its continue queued by the daemon: it raises one `continue_no_commit` event (trigger
"re-plan: no new commit") naming the task and the continue it asked for, and the coordinator decides.
Other kinds keep metrics as evidence; continue caps and max_hops are unchanged.
