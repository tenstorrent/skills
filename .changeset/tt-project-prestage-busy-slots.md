---
"tt-project": patch
---

The `ttp checks --detach` pre-stage no longer skips itself when the host's test slots are busy. It
waits at most 5 s for a slot, then runs on one worker without one, so a change that fails a fast
test ends in the foreground instead of queueing a full detached run behind the busy slots. A pass
or a timeout still starts the full checks unchanged.
