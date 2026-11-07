---
"tt-project": patch
---

`tt-project`: the daemon's lock sweep of the project root's repository also moves aside a `.git/config.lock` left behind by a killed sandboxed run (the empty, read-only placeholder a sandbox mounts for each command), which otherwise blocks every `git config` write in the project root. It counts as stale only when it is empty, has no write bits, is older than `harness_git.root_ref_lock_s`, no process has it open and no live git process working in the repository started before it. It goes to `ttp-stale-locks/`, never deleted; a non-empty, writable, young or open config.lock is left alone.
