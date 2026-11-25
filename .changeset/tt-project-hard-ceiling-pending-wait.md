---
"tt-project": patch
---

A waiting task whose probe still says "not yet" (exit 1, 75 or 255) no longer starts a paid worker run
only because it reached the wake gate's hard ceiling (`waiting.wake_gate.cap_hard_h`). It keeps
sleeping on its backoff with one model-free skip line naming the task and its wait age, and the
coordinator still gets the long-wait line. A probe that passes, fails, breaks or gave no answer still
wakes the task at the ceiling as before. Any skip past the ceiling, a planned check's included, now
sleeps its backoff instead of being re-decided every minute.
