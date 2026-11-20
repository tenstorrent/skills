---
"tt-project": patch
---

`tt-project`: device A/B and shadow reviews honour their off switch, and the rerun after a spill failure checks the right commit. With `route.device_ab_shadow.enabled` false, the daemon's device A/B tick still queued "Device shadow review" tasks; `route.device_ab` ignored `enabled: false` altogether. Both now queue nothing while off. After checks failed on a spill host, the rerun on this machine checked the live worktree, so another task's uncommitted edits in a shared worktree stopped it with "commit first". The rerun (and a preempted run's rerun) now checks the commit its first run checked, in a clean checkout of it. A spill host's own fault (exit 75, not a busy refusal) now voids the failures recorded on that host in the last hour, so they no longer count against their commits.
