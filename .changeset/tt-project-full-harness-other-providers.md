---
"tt-project": patch
---

A Codex review's `apply_patch` and a Cursor review's file writes are now held to the same rule as on Claude Code: they write only in the review's run folder or temp folders, unless a merge or rebase is in progress. A Cursor Grep from `/` or the home folder is refused. The provider matrix lists the guards that still differ by provider.
