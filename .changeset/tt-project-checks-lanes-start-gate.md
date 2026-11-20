---
"tt-project": patch
---

`tt-project`: checks lanes: with checks.slots unset, a repository's lanes follow the host (cores / one run's cores, capped by free memory and load). An overflow lane is taken only while the host test pool lets the run start; a lane holder waiting on the host checks slot or test slots says which in status and raises no likely-hang wake (its run clock starts when it is granted). The host-wide checks cap prices a check at one run's cores, falling back to 4 when the test pool is off, and never below the costliest of the host's last 20 checks runs as measured (their commands' user and system CPU per wall second, so process churn counts).
