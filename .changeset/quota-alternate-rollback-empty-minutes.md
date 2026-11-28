---
"tt-project": patch
---

Quota alternates (glm_quota_alt): the wave gate judges each alternate's tasks in a project against that project's own Claude baseline, at a ring level too, and rolls a worse alternate back for that project alone (`<project>:<model>`; `ttp rollout arm glm_quota_alt <project>:<model>` restores it). Each empty quota bucket now keeps its empty windows, and `ttp stats --glm-share` and the daily review show the minutes per UTC hour each model's bucket was empty.
