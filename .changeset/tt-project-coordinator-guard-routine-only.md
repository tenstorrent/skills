---
"tt-project": patch
---

`tt-project`: the runaway guard's coordinator signal (self-started coordinator turns past their hourly limit) no longer stops every worker start. It still defers self-started coordinator turns and holds routine and scheduled work, but urgent tasks and user-ordered tasks (answering a chat or owning an obligation) keep starting under the dollar caps and every other gate. The budget line, `ttp status` slots line and the coordinator's gate summary say which runs it holds.
