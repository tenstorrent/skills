---
"tt-project": patch
---

Bench prep rebuilds a landed change whose work branch and push pin are gone, or that landed as a fast-forward: its range starts after the nearest earlier landed commit, push branch tip or version-bump commit (within 60 first-parent commits), and one commit shared by several changes is excluded with its reason. The push queue keeps each landed head under `refs/ttp/landed/<row>` for 90 days once its pin is pruned. Prep also counts old rows that recorded only their batch's pushed commit and code tasks' own `ttp push` landings, and scans candidates newest first. The fairness checks are unchanged.
