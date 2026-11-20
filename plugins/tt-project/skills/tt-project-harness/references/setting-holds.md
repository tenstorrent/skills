# Setting holds

While `delivery.allow_protected_push_branch` is off, refuses the push branch and an open ask names it, the daemon (runtime `settinghold.py`) keeps every landing task on that ask (`waits:ask:<id>`, blocked_reason `setting hold: ...`), attempts untouched: queued ones before they start, and blocked ones a worker handed off naming the setting.
A review with a push step runs as review only and hands off blocked naming the setting.
All are released together once the setting is on or the ask is resolved; the digest shows one line per setting.
A charter line granting pushes there (allowed, or no approval needed; the verbatim Brief counts as the user's word) while the setting is off and unasked makes the daemon raise one ask itself (blocking restriction, naming the setting), model-free, at setup or the first tick after a charter or delivery edit: once per push branch and charter wording, never per turn.
That ask is only for a grant with no user word on record: see [Charter grants](charter-grants.md).
A coordinator config_set the daemon rejects names that open ask, and a memory_add in the same turn naming the rejected setting is marked `Not in effect` in the entry.
A project-local copy of this logic is no longer needed.
