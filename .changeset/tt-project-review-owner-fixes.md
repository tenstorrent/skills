---
"tt-project": patch
---

Review owners: the unreviewed sweep no longer counts a continuer working in the project root as holding a done task's review, and logs once per reason change why it left an urgent or owed task alone; a done task whose continuer is cancelled late is still reviewed (its age runs from the cancel); a failed review whose change already has an open review or PR fix gets its own fix and re-review and moves its dependents.
