---
"tt-project": patch
---

`tt-project`: device A/B counts only real device incidents tied to the run's own device and lifts a false stop on the next tick.
- device A/B shadow arms (GLM, Sonnet, Opus) plan each device task read only in a sealed cell, no lock or resource, blind reviewed by Opus and a second non-Claude reviewer, side by side in ttp stats --device-ab
- device A/B counts a watcher observation only from a device or machine watcher naming the run's own device and citing no other task (a deferral notice about another task no longer stops the arm); help on the hidden device-shadow args
- help text on the hidden device-shadow arguments
