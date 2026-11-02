---
"tt-project": patch
---

`tt-project`: a "Needs you" item for a task deferred on a user-only step keeps the whole start_when as its check (up to the 1000 characters a start_when may have, not 500). A long start_when no longer rewrites the item on every daemon tick, and once its check passes the item posts that the step is done instead of clearing silently.
