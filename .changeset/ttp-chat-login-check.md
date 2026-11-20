---
"tt-project": patch
---

The chat provider has a model-free login check: while no key is saved for its endpoint alias, or the
endpoint refuses the key, its logged-out pause starts no probe runs (each failed at launch again) and
work routed to it falls back to the core provider. A key the endpoint takes ends the pause.
