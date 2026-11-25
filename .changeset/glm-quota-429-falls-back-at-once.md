---
"tt-project": patch
---

`tt-project`: a chat-provider run whose endpoint answers HTTP 429 with code `quota_exceeded` stops at once instead of retrying until its time limit. The bridge records when the token quota resets (the body's `resets_at`, else the next full hour), the runner ends the run, and the daemon requeues the task with no attempt spent and its worktree kept, with the route line `fell back: quota (HTTP 429 quota_exceeded; token quota used up until <time>)`. The provider's existing limit pause then holds the chat provider until that time: routed work starts on the core provider, a preference pin moves at once and a required pin waits for the reset; status and the web app show the pause and its end. A run that handed off before the stop keeps its hand-off.
