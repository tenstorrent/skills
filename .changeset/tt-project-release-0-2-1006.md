---
"tt-project": patch
---

tt-project 0.2.1006: `ttp push` scans the outgoing history for secret shapes as its own first step, before the rebase and the checks, so a leak fails in seconds with nothing run or sent. The refusal now also says how to rewrite history without leaving filter-branch's `.git-rewrite` folder in the worktree. A task whose title rebases a change, or whose title or spec re-lands one, is no longer chained to wait for that change to land, and no second review of the change is queued while such a task is open (its own review covers it). `ttp detach --stop` also ends a job's helpers that moved to other process groups or left its session, such as pytest-xdist workers. A command forwarded to a project on another machine whose older ttp lacks it now says so. A task citing a user message is held only on the resources the message's words name. A code comment no longer names a test box.
