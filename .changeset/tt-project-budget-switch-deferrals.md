---
"tt-project": patch
---

`tt-project`: a provider account or budget kind change no longer releases deferrals that wait on something else. A deferral counts as waiting for an old plan window's reset only when it waits for a time alone; one with a `start_when` probe (a landing, a run) keeps its deferral even when its `start_after` falls near that reset, unless its reason names the old limits. When only the account changed, the event says "account switched: still <budget>" instead of "was X, now X".
