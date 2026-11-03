---
"tt-project": patch
---

push: a push queued from a worker's sandbox runs its checks in a short temp dir. When the daemon starts it, it reuses the recorded short scratch only if it is still a writable directory of the daemon's user, else makes and records a new one; the long check-runs path is used only when no short dir can be made. The push log names the scratch used and why.
