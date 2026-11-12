---
"tt-project": patch
---

`tt-project`: `ttp bridge` links come back on their own after the computer reboots while the box still holds the old sessions. Each session's remote command records its sshd's pid in the box account's cache dir (private, removed when it exits), and the stale-session cleanup reads these records, so it finds sessions sshd made non-dumpable (hidden from `ss` and `/proc`). It still ends only the box account's own sshd sessions carrying this computer's tag for the same box and agent, now checking the owner by real uid; a record naming a reused pid is removed and its process left alone. `ttp bridge status` says "could not check" when the box did not answer the probe, and says the ssh config block is missing only when the box said so.
