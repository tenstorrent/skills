---
"tt-project": patch
---

The test select plugin (targeted `ttp checks`, push batch cap probes and failing-test reruns) acts
only in the pytest it was loaded into and its xdist workers: it takes its load, settings and folder
out of the environment, so a pytest a test starts runs its own tests instead of failing on the
missing plugin or selecting nothing.
