---
"tt-project": patch
---

A finished run whose only unread update is the harness's own drift-check note is no longer sent
back to redo its work: the note is a model-free health check, never an instruction from the
coordinator or the user, so a done hand-off is accepted as if nothing was unread and its review is
queued. A real unread spec update still sends the run back as before. The drift `quiet` check no
longer fires while the run's own detached checks are queued or running: a live checks run counts as
activity, so a worker only waiting on its checks is not idle.
