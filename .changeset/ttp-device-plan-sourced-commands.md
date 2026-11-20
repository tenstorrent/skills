---
"tt-project": patch
---

Device plans copy their commands instead of writing them from memory. The planner prompt lists the commands to copy from: steps that passed in the project's recent done runs, and the project's own scripts those steps ran. Each step cites its source. A model-free check rejects a push or publish step. It sends an unsourced check or submit step back to the planner once with the reason, and hands back a plan that is still unsourced.
