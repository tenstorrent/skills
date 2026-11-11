---
"tt-project": patch
---

`tt-project`: Codex worker, review and fix runs that start from a saved session (a fork of the coordinator's, a follow-up's resume, their own continued one) run under their own system prompt: `codex exec resume` keeps the session's developer instructions and takes no new ones, so a worker forked from a coordinator without tools ran under "This session has no tools". The copy's harness instructions are now replaced by the run's; Codex's own blocks stay. Coordinator and Claude runs are unchanged.
