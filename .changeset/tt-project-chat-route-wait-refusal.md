---
"tt-project": patch
---

`tt-project`: a chat-route run is now refused a wait call (a `sleep` of 30 s or more, a loop that sleeps, `tail -f` or `watch`) by the worker hook, with a hint to hand off `waiting` with a `retry_when` such as `ttp checks --result <run dir>`. Such waits would otherwise end the run as stalled and rerun it on the core provider for the same result. `tail -f` and `watch` now also count as waits for the chat-route stall check; `timeout N ttp checks --result` does not, as it returns at once. Other providers' runs are unchanged.
