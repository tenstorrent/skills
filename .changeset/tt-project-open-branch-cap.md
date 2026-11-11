---
"tt-project": patch
---

tt-project counts each project's open code branches and running workers without a model call and
shows them in `ttp status`, the web app and the coordinator's Course check, with one alarm line over
the soft caps `delivery.max_open_branches` and `delivery.max_work_threads` asking to fold new work
onto open branches (urgent and user-ordered work is never held). Landed tasks' local branches are
deleted once their worktree is gone, each tip saved as `refs/ttp-archive/<branch>` and recorded in
`state/branch-deletions.jsonl`.
