---
"tt-project": patch
---

`tt-project`: the credential tripwire's transcript check (credwatch.transcript_hit) is fast and counts only what a run did. It ran two regexes over every line of a run's transcript, on the daemon's thread as runs ended; Claude Code lines reach over 1 MB, so on a loaded host a burst of run ends stalled dispatch for minutes. It also matched the run's own prompt, compact summary and prose, so a run whose spec only named a changed credential file failed its hand-off. Now a line holding neither the file's name nor its writer's command word is skipped as bytes, unparsed; Claude Code and Cursor lines count only tool calls and tool results, and writer commands (ssh-keygen, gh auth, git credential) count only in tool calls; lines of other shapes still count whole. Answers are kept per run dir, path and transcript size and mtime, so a repeat call reads nothing.
