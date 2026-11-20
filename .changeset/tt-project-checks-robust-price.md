---
"tt-project": patch
---

`tt-project`: one unusually heavy checks run no longer prices every check on the host. A check's measured price is the 80th percentile of the last 20 runs on the host within 7 days, not the costliest, and needs at least 5 measured runs (fewer use the default price). A single outlier run never sets it; heavy runs that are a fifth or more of recent runs still raise it. Busy and free cores now count the same cores: the process's CPU affinity where the OS has one, else the CPU count.
