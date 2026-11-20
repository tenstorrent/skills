---
"tt-project": patch
---

`tt-project`: the worker, daily-review and coordinator (blocker sweep) prompts and the tt-project-harness skill say that host names do not resolve inside the worker sandbox, so a command or script that reaches another host or GitHub (ssh, git fetch/ls-remote/push, curl) runs as one bare command (e.g. `ttp lock <res> -- <cmd>`, absolute paths, no cd, pipe, `;`, `&&` or subshell), and a recurring report that needs one comes from a model-free command schedule or pre-step writing files the run reads. A blocker sweep respecs a run that failed on an unresolved host name instead of treating it as a host outage.
