---
"tt-project": patch
---

Rollout: `ttp rollout fixed FLAG PROJECT --fix TASK --runs R1,R2` records the stops a guard fix covers, once the fix landed and a daemon start in that project ran it. The GLM device and work/code rollback counts then leave out each listed stop from before the fix went live; later stops and stops no fix lists still count. The verdict and `ttp rollout status` show the stops not counted.
