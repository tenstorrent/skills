---
"tt-project": patch
---

`tt-project`: a credential file changed outside tt-project's runs no longer fails every run that happened to be open, and no longer pages each project once per run and file. A run's hand-off fails only when its own transcript names the changed file or runs a command that writes such a store (`gh auth` login/refresh/switch/logout, `ssh-keygen` without a scratch `-f`, `git credential`). Any other change is told once for the whole host (one claim per file, change minute and kind, shared by every project), high when a store is new, removed, replaced or resized and normal otherwise, and the text says how to record a deliberate change in the machine's change ledger (`ttp machines change add <alias> ... --undo 'none: ...'`) so later runs are not blamed.
