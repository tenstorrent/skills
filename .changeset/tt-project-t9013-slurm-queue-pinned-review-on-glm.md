---
"tt-project": patch
---

`tt-project`: the Slurm guard gains guards.slurm_queue_pinned (off by default), which lets an sbatch without --immediate name busy or drained nodes since it only queues, and now refuses scancel of another user's job and scontrol updates of a node's state; a review run that starts on the chat provider raises one high `review-on-glm` observation, and `ttp stats --glm-share` lists reviews that ran on GLM.
