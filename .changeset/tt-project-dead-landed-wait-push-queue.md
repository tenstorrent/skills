---
"tt-project": patch
---

`tt-project`: `landed_wait_dead` counts a change the push queue landed (a pushed or landed push queue row of the task, a task carrying it on, or its branch) as landed, through the same saved-landing lookup `ttp landed --task` uses, so it no longer reports such changes as never landed.
