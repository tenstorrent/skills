---
"tt-project": patch
---

`tt-project`: an `exclusive:<resource>` task that cannot start because the disk is low, or that
is blocked because its workspace could not be set up, drops its resource reservation at once
instead of holding `ttp lock` commands off until the reservation lapses.
