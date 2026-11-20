---
"tt-project": patch
---

`tt-project`: ttp checks: full runs of one repository run side by side by default (`checks.slots` default at least 2, sized to the test-slot pool, chat-settable as a whole number); each run gets its own port block (TTP_PORT_BASE, TTP_PORT_COUNT), checks in the authoring worktree keep one lane per worktree, and the wait line and `ttp status` name the limit and `checks.slots`.
