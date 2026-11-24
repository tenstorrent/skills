---
"tt-project": patch
---

`tt-project`: GLM device plans: the task head's scratch folder is no longer a free word in the plan source check.

- `unsourced()` built its `named` word set from the task text, whose head names the run's own scratch folder, so a plan word that was exactly that bare folder matched any source word: a plan could swap a `/tmp` file for the run's scratch folder. The scratch folder is now left out of `named`, so only the scratch-folder swap rule accepts it, which requires the source word to be a `/tmp` folder the source segment names a path in.
