---
"tt-project": patch
---

`tt-project`: routed-run escalation. A start that a routed tier would send to the chat provider stays on the core provider, recorded as fallback `escalated` with its kind and cause, when the task is urgent or user-ordered, when it fixes a review or failed checks of a change its lineage wrote on the routed provider, or when its lineage already has two routed misses (routed runs that fell back on their own or handed off `failed`, and routed changes a review or checks sent back). `ttp status` counts escalations apart from fallbacks and `ttp stats --runs` ends with their count by kind.
