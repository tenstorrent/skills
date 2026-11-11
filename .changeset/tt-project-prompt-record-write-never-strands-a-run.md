---
"tt-project": patch
---

A coordinator session wake whose prompt record could not be saved in its run folder (a full disk, say) now still starts, and the failure is logged. Before, the run's row stayed "running" with no process until the reaper ended it as lost, and the wake never ran. With nothing saved, nothing is acknowledged, so the next wake sends the prompt changes again.
