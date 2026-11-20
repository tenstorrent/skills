---
"tt-project": patch
---

Sandbox: `sandbox.fence_freed_commands` leaves out env names that make a command load or run other code
(`PATH`, `LD_*`, `DYLD_*`, `BASH_ENV`, `ENV`, `SHELLOPTS`, `BASHOPTS`, `IFS`, `PYTHONPATH`, `PYTHONSTARTUP`,
`PYTHONHOME`, Perl, Ruby and Node options), with one daemon log line each, so a project's config cannot
free other code through an env prefix on a freed command. The guide notes that Claude Code 2.1.296 frees
no line starting with an assignment (measured), so the env form adds a pattern that frees nothing there yet.
