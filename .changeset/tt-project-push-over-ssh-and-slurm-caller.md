---
"tt-project": patch
---

With no https login on the host, ttp push, its reach check and remotes with an explicit https
push URL go to GitHub over its ssh form, so git never asks a credential helper for github.com and
no token reaches the host. Fetches, pulls and ls-remote (also `ttp landed`) use ssh only while an
ssh agent answers; without one they stay https with github.com's credential helpers reset (other hosts'
helpers still answer), so an anonymous fetch of a public repository keeps working. Other gh commands outside the workstation allowlist
stay refused. The Slurm guard reads `-u $USER` (and `${USER}`, `$LOGNAME`, `$(whoami)`) as the
caller only when every `-u $(...)` in a scancel's text is the caller, and a job id squeue no longer knows as held by
nobody; scancel of another user's live job and node-state updates stay refused.
