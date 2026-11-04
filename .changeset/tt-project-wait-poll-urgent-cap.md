---
"tt-project": patch
---

`tt-project`: waits on state the daemon can read are checked sooner. Every `retry_when` and `start_when` probe now runs model-free every `waiting.poll_s` (default 90 s, kept between 20 and 180 s; it was a fixed 180 s), and a passing probe wakes its task at once, whatever later retry the worker chose. A `ttp lock --probe` start gate no longer backs off. An urgent task (priority 1 or an `urgent:` spec line) waiting on a probe that is not a bare clock gate sleeps at most `waiting.urgent_retry_cap_s` (default 300 s; 0 turns it off) per timer instead of the worker's `retry_after_s`; a timer that ends while the probe still says not yet puts the task back to sleep without a model call. Routine tasks keep their backoff.
