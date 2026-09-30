---
"tt-project": patch
---

`tt-project`: harness tasks change only their own project's harness.

- The harness-task and worker prompts and the `tt-project-harness` skill say a harness task never
  edits, or makes a worktree or branch in, the tt-project plugin's source repository or another
  project's harness, even to port a fix.
- Lessons for tt-project go in the hand-off as upstream notes: `followups` titled `upstream: ...`.
  The coordinator passes them on to the user instead of queueing them as work.
