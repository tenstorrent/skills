---
"tt-project": patch
---

`tt-project`: a done code task whose only follow-up is a `continue:` hop with progress, and whose change is neither approved nor landed, is now re-queued as the same task (same id, worktree, branch and review chain) instead of a new one. The follow-up's spec is appended as an `## Update`, the per-task budget tops up to a fresh one, attempts stay, and the last summary stays in the prompt. Each re-queue counts toward `chains.max_hops`, and progress counts only runs since the last re-queue. An approved or landed task keeps the old path (a new task). `chains.requeue: false` turns it off. The Stop hook refuses a done hand-off whose `continue:` only waits (checks, a detached job, a landing): hand off waiting with retry_when and on_pass_cmd instead.
