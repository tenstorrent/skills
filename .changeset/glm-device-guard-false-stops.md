---
"tt-project": patch
---

`tt-project`: three GLM device-plan stops on safe steps are fixed. A plan that the source check sends back gets its JSON block checked and fixed once more, the same way as the first plan. The source check lets a copied job submit use the run's own scratch folder where the source run used another `/tmp` folder. Only the folder may differ: the command, broker verb, limits and script, its file name included, must still match. The planner is told which host the run is on, so a step there runs locally rather than over ssh to itself.
