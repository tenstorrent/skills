---
"tt-project": patch
---

`tt-project`: a chat-route run that only sleep-polls now ends at its tier's stall limit (`budget.stall_s`) and falls back to the core provider, instead of running to the route timeout. Wait calls (a `sleep` of 30 s or more, or a loop that sleeps) no longer keep such a run alive: when they fill 90% of the last `stall_s` of awake time, the run supervisor stops it as stalled, which falls back once like a timeout. A silent run was already stopped at `stall_s`; the route timeout stays the hard cap. Other providers' runs are unchanged.
