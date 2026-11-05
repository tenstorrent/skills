# Host-reaching commands

Runs are unsandboxed by default: ssh, git, curl and sudo work as in a plain shell. This page applies only
to a project that turned its provider's write fence on (`providers.<name>.write_fence: true`).

- Host names do not resolve in a worker's sandbox. Only a bare excluded command (`ssh`, `git fetch`, `ttp lock ...`) runs outside it; the same command inside cd, a pipe, `;`, `&&`, a subshell or a script fails with an unresolved host name. A host reached by raw IP or loopback still works, which can hide the cause.
- A command or script reaching another host or GitHub (ssh, git fetch/ls-remote/push, curl) runs as one bare command (e.g. `ttp lock <res> -- <cmd>`, absolute paths, no cd, pipe, `;`, `&&` or subshell).
- A recurring report that needs one (a daily review's input) gets a model-free `kind: command` schedule or pre-step writing files the run reads.
