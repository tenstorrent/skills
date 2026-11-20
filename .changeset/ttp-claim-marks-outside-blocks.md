---
"tt-project": patch
---

`tt-project`: status-claim marks no longer break text meant for posting. A "Planned, not started on the host yet:" or "(unverified)" mark is never put inside a `---` section, fenced code block or blockquote: it goes on its own line after the block, quoting the sentence it refers to. The not-started-on-the-host check only marks a sentence that names a task id, or a host or resource of a waiting task, never one that names neither. A queued task whose detached driver still runs on its host counts as under way, and the job sweep now asks the host about jobs of open tasks with no worker too, so an ended driver stops counting. A claim's probe runs before the text is checked even when the daemon did not run it first, a sentence that states its claim more briefly still finds it, and a landed-commit probe that cannot tell falls back to the probe as written. A claim whose probe passes is sent unmarked.
