---
"tt-project": patch
---

`tt-project`: before the daemon merges a harness task's worktree branch into the harness checkout, it now commits its own uncommitted memory writes (`MEMORY.md`, `memory/`) as a separate commit. Before, git refused the merge when such a write touched a file the branch also changed, so the task's commits waited for someone to merge them by hand. If the branch changes the same memory file in a way that clashes, the merge is aborted and reported as a conflict, and both sides stay in their commits. While another harness task's run works in the checkout, its memory edits are left alone. Other uncommitted files are never committed: git refuses over them as before.
