---
"tt-project": patch
---

A queued chat message that the project host keeps failing for its own reason (not ssh exit 255)
no longer blocks forwarded commands or the remote listener: the message stays queued, the user is
told once, and the command or listener still runs. Only ssh exit 255 means the host is
unreachable. Remote upstream inbox reads strip the padding BSD `wc -c` adds to the byte count.
