---
"tt-project": patch
---

Tests gate: when a `ttp checks` pytest check fails, only its failing tests run again at once on the same host, on the head and then on the base. Failures on both are pre-existing and failures that pass on the head rerun are flaky (quarantined for 7 days, shown in status, one fix note to the coordinator); neither blocks the change's landing. Failures new on the head still block. An excused pass is never recorded, and `ttp checks --release` excuses nothing.
