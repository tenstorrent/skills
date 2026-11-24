---
"tt-project": patch
---

GLM device runs: the plan check and the run-time guard refuse a step or command that runs a direct device tool (default `run_cluster_validation`, `test_system_health`, `test_tt_fabric`; set per project in `route.glm_work.device_tools`) outside the box's device broker, with a reason naming the step and saying to run it through the broker. Submitting it as a broker job, and `--help` or `-h` alone, still pass. The advisor's plan rules say device jobs go only through the broker, and the GLM-arm prompt says a spec's publish, push or `published` step is the review's job and is skipped; any `git push`, dry run included, still hands the run back.
