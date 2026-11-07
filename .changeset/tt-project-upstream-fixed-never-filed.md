---
"tt-project": patch
---

`tt-project`: `ttp upstream --fixed <fp>` on an id that was never filed (in no inbox, resolved log or outbox on this machine, and in no forward target's inbox) prints "unknown note <fp>: never filed" and exits 4, so a task waiting on it wakes as a broken probe instead of sleeping until its deferral runs out. A filed but open note still exits 1, a fixed one 0, and a forward target that cannot be asked keeps the wait. A new start_when, `ttp task set-when` retry_when or waiting hand-off retry_when on such an id is refused, naming it; a hand-off's own `upstream:` note counts as filed.
