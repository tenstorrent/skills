---
"tt-project": patch
---

`tt-project`: an `exclusive:<resource>` task whose run fails to start drops its resource
reservation at once instead of holding `ttp lock` commands off while it waits to retry.
