---
"tt-project": patch
---

`tt-project`: `ttp machines activity`'s holder view judges a lock holder it cannot see (its pid is from another PID namespace, e.g. a run from inside a sandbox) through the holder's run folder — the same route `ttp lock --probe` uses — instead of showing `alive None`. A stale beat or lease never makes it 'gone'; 'unknown' stays unknown; 'gone' only when the run's record says the hold ended or exit.json exists. Holders whose pid this process sees are judged as before.
