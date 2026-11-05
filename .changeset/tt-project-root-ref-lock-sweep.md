---
"tt-project": patch
---

`tt-project`: the daemon's harness sweep also moves aside stale ref locks (`refs/**/*.lock`, `packed-refs.lock`) in the project root's own repository, using the common git dir when the root is a worktree, so a lock left by a killed or rebooted writer no longer makes ref updates and `git gc --auto` fail. A lock moves only when it is older than `harness_git.root_ref_lock_s` (3600 s; 0: off), no process has it open, and no live process that started before the lock has its working directory or GIT_DIR in the repository; without /proc nothing moves. Locks go to `<git dir>/ttp-stale-locks/<stamp>/`, never deleted, and each one's path, age and holder check is logged. Index, HEAD, config, shallow and worktree locks are never touched.
