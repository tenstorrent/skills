---
"tt-project": patch
---

`tt-project`: automatic switch-off rules count only what happened after the latest re-arm. The glm_device harm rule counts harmful steps only since the project's latest re-arm, a re-arm restarts its audits, and a new harmful step after a re-arm stops the route again. The code arm switch-back and the code-route guard never count the tasks or reviews that tripped them again once their setting changes.
