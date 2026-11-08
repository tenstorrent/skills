---
"tt-project": patch
---

A task's detached `ttp checks` run or a review's precheck that dies on the same boot without writing its exit code (an OOM kill, a crash) is started again by the daemon, model-free, on the same head and at its old place in the host's checks queue, once per run folder and head. Before, only a reboot restarted them, and a crash woke the task for a model run. Checks tt-project stopped itself, and those of a cancelled, finished or deferred task or a closed review, stay stopped.
