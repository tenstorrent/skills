---
"tt-project": patch
---

Sandbox: `sandbox.fence_freed_commands` drops the env form. The Claude Code Bash sandbox never frees a
line that starts with an assignment (re-measured on CLI 2.1.296: neither `BAZ=1 cmd *` nor `FOO=* cmd *`
freed such a line, while `cmd *` freed `cmd ...`), so `{"command": ..., "env": [...]}` no longer adds
`VAR=* cmd *` patterns. Such an entry still loads: the command is freed and the env part is ignored with
one daemon log line. The loader and shell-startup env-name refusal goes with it.
