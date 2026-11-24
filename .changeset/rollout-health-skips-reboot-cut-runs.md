---
"tt-project": patch
---

The rollout health watch no longer counts a run a host reboot or crash cut short as a failed run, on
every flag and in the baseline alike. A host restart used to switch a model-route flag off in a project
when its window was small; the rerun of such a run counts on its own outcome.
