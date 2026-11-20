---
"tt-project": patch
---

`tt-project`: fewer model wakes after a host reboot. A `retry_when` that runs a probe script from a run folder (directly, through `sh`/`bash`, or under `timeout`) is now judged by what the script checks, line by line, as an inline probe is: a script that checks only durable state (a timer, another host over ssh, CI, a reply) keeps its task asleep through a reboot, and one that checks a process, a path a reboot clears, a local detached job or a run-folder file still wakes it. A script that is missing, over 64 KiB, not a regular file, unreadable or not text counts, as before, as a file in a run folder.
