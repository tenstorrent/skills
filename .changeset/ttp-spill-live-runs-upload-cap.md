---
"tt-project": patch
---

Spill host slots count only remote check folders whose run started and that a live process works in, so stalled tree copies no longer fill a host. Tree copies to one host from a machine are capped (two at once, shared by every project there). A host that cannot build the pinned checks env (for example with no package index) fails fast and is skipped by every project on the machine until a run there passes its env step, with one retry every 30 minutes.
