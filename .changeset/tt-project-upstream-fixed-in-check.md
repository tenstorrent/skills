---
"tt-project": patch
---

`tt-project`: `ttp upstream --resolve <fp> --fixed-in <version>` now checks the version when it knows the task that fixed the note (the one whose hand-off lists it in `resolves`, or a new `--task <id>`). The task's landed commit, also as a rebased copy, must be in the first commit on the push branch whose `delivery.version_bump` file carries that version; otherwise the resolve is refused with a message naming the task, its commit (or that it has not landed) and the version, and nothing is recorded. With no known fixing task, push branch or version bump, it records as before and prints one line saying the version was not checked. The push queue's own resolve path is unchanged: it records the version it just pushed, which carries the change by construction.
