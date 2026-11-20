---
"tt-project": patch
---

A change to the coordinator's system prompt, charter or action schema no longer starts a new coordinator session. The saved session resumes with the new prompt and schema, and the wake leads with a model-free "Changed since your last wake" block: a prompt version line and the charter's unified diff against the copy saved by the last wake that ended ok (plus the system prompt's diff on Codex, whose resume may keep the old instructions). A new session starts only when the provider changed, or on a provider that cannot resume with a changed prompt (`Provider.resume_new_prompt`), each recorded as its own reason in `ttp stats --coord-session`. Claude Code, Codex and Cursor all resume; the provider matrix lists each.
