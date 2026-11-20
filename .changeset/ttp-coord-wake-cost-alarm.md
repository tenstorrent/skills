---
"tt-project": patch
---

A model-free check reads the coordinator's cost per wake after each session wake. When the median of the last 5 wakes is above `coordinator.wake_cost_alarm_usd` (default $1.00; 0 or `off` turns it off), it raises one alarm per episode naming the cause (context tokens, digest size or tool calls). The same session then compacts in place, and the digest stops resending sections whose only change is an age. It never starts a new or stateless session.
