---
"tt-project": patch
---

`tt-project`: template upgrades start at most one model task per `upgrade.task_every_h` hours (default 6), whatever the last one did. Before, a task that landed its release lifted the once-a-day limit, so a project whose local edits conflicted on every release ran one model task per release. Releases inside the window only record the wait; the next task merges the newest release, and an open task still takes newer releases on. A clean merge whose runtime compiles and imports is applied and the daemon restarted with no model task, now logged as a feed line instead of a low alert. The docs, the harness skill, the harness task prompt and the upgrade task prompt say where a project's own imports go: in one block at the end of the file, never inside upstream's `from . import ...` lists, so they merge cleanly.
