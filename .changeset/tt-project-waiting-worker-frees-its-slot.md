---
"tt-project": patch
---

`tt-project`:

- a worker waiting on a remote test run (`ttp lock <box> -- ...`, a test over ssh) or a slow poll runs it detached (`ttp detach`) and hands off `waiting` with the `retry_when` it prints, so its slot frees at once and it resumes when the job ends. The worker prompt says so; the PreToolUse hook adds a reminder (never a refusal) on such a foreground command in a worker run; `ttp lock` in a worker run whose foreground command passes 3 minutes (`TTP_FG_WAIT_S`) writes one steer note and a progress line naming the `ttp detach` command to use; tests
