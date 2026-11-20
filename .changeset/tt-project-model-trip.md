---
"tt-project": patch
---

`tt-project`: a model-free tripwire for the model pick. Per kind, tier and task label, it compares routed and core tasks on failures, failed checks, failed reviews and rework. Once both sides have enough samples and routed goes wrong more than a set margin above core, that stratum goes back to the core model, a decision is recorded and one low notice is sent. Only a newer passing head-to-head result turns routing back on (`ttp model-trip --h2h`). It does nothing while the model pick is off.
