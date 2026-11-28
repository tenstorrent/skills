---
"tt-project": patch
---

GLM code runs: behind the `glm_probes` rollout flag (off by default; `glm.probes` per project), treatment-arm runs (even task id) run each changed function or rule on 2-3 adversarial inputs and hand off the actual outputs as `probes`; the hand-off check sends back a treatment-arm code change without them. The daily GLM report adds first-review fail rate and the share of failures on a code finding per arm.
