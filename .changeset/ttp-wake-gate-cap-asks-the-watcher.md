---
"tt-project": patch
---

`tt-project`: the wait-wake gate's cap (`max_skips` or `max_skip_h`) no longer wakes a waiting task before the watcher is asked: at the cap the watcher (or Jev or its Haiku fallback behind it) decides, and may skip with a next look of at least `backoff_s`; no answer, or a probe that answered, still wakes. A new hard ceiling, `waiting.wake_gate.cap_hard_h` (24 h since the last turn), wakes with no question. A wake the daemon cannot start yet (slots full, a hold) is the same wake when re-gated before its turn: no new wake_gate row and no new watcher call each tick. A Jev-path decision names the model that answered (Jev, or the Haiku model when Haiku stood in).
