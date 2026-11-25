---
"tt-project": patch
---

`tt-project`: the glm_code rollout flag gets the per-project quality rollback glm_work already has. The wave gate judges a project's code-arm records against its own Claude baseline (glmshare.judge_code) on first-review pass rate, bounce rate and tasks done per day, and rolls the flag back for that project alone past the rollback margin; `ttp rollout status` shows its re-arm line and `ttp rollout arm glm_code <project>` re-arms it. The wave gate now also judges glm_code at a ring level (it runs at one; the only flag marked `ring_judge`): it judges the projects on this host that the rings cover and only rolls back; waves still open only at auto. glm_work and glm_device are judged at auto alone, as before, and glm_work's judge is unchanged.
