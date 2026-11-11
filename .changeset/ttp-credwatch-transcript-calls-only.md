---
"tt-project": patch
---

`tt-project`: the credential tripwire's transcript check (credwatch.transcript_hit) no longer stalls the daemon's thread on a large transcript. It parsed every line of a run's transcript (Claude Code lines reach over 1 MB) as runs ended, so a burst of run ends could stall dispatch for minutes on a loaded host. Now a transcript and refusal log holding neither the file's name nor its writer's command word are answered from a byte scan, with no line parsed, and answers are kept per run dir, path, and the two files' size and mtime, so a repeat call reads nothing.
