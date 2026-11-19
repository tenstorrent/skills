---
"tt-project": patch
---

`tt-project`: a code task the user ordered onto the cheaper code arm carries the coordinator's spec line `code_arm: user msg #<id>`. It skips the policy-class check and the coin, runs on the arm while it is live, and is not bounced for touching policy files; its Opus review still reads the whole diff. The line counts only on a task from the coordinator's own task_add that cites a user message; a worker follow-up drops it and a daemon task ignores it. `ttp task show` reads `code route arm: glm (user msg #<id>)`.
