---
"tt-project": patch
---

`tt-project`: fewer model calls while a project waits.

- Periodic coordinator wakes back off while nothing changes: each wake that meets the same tasks,
  questions, schedules, budget levels, charter and memory as the last one doubles the wait, up to
  a day. Any message, event or change to the work restores the normal pace.
- A rejected coordinator action no longer starts a turn by itself. It is shown in the next turn's
  digest; the same rejection on consecutive turns is recorded once as a harness event.
- The digest lists the last few replies, questions and alerts sent to the user, and a question
  identical to one still open is not asked again.
- A worker that hands off `waiting` can give `retry_when`, a shell check. The daemon runs it every
  few minutes without a model and brings the task back as soon as it passes; `retry_after_s`
  stays the fallback.
- Worker prompts state the charter's restrictions twice (first and last) instead of three times.
- Claude workers pass `--exclude-dynamic-system-prompt-sections` when the installed CLI has it, so
  the system prompt stays cached across worktrees (measured on a trivial run in a fresh worktree:
  cache-write tokens 13,285 → 9,382).
