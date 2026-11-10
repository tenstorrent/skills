---
"tt-project": patch
---

An account-wide chat model scoreboard (`ttp model-bench scores`) ranks models per task class (babysit, light, standard, review) from bench scores (else the Artificial Analysis index as a prior) blended with live first-try results by sample count, with median wall time and tokens/s; it publishes `providers.chat.class_ranking` under `providers.chat.class_prefs` (smart, fast or auto) and keeps `providers.chat.model_ranking` equal to the standard class.
