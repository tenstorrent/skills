---
"tt-project": patch
---

The coordinator session survives daemon restarts and host reboots: both resume the same session id,
and a failed resume reruns its batch in the same session. Only a resume the provider rejects (unknown
or expired session id, or 3 resumes in a row that did no work) or a transcript the provider no longer
keeps starts a new session, seeded with the hand-off. A full context compacts in place (merged from
the in-place compaction change) and never starts a new session.
