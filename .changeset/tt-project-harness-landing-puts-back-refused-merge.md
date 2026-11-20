---
"tt-project": patch
---

The daemon's landing of a harness task branch now puts back the files git wrote when the template guard on main refused the merge. Before, a task branch that held a template merge left its runtime files staged in the live harness after the refusal, so the merge task's `ttp upgrade --apply` failed its fast-forward with "Your local changes would be overwritten" although only CHARTER and MEMORY had been edited.
