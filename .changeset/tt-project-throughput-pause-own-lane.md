---
"tt-project": patch
---

Throughput sizing: a paused resource holds only its own resource lane and its tasks' waits; it no longer blocks every lane raise, so worker and checks lanes are judged on the provider gate and the host's CPU alone. Paused resources are still never dispatched.
