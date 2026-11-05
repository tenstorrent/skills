---
"tt-project": patch
---

`tt-project`: the tests gate now judges a remote check's failures on that check's own host, a spill host included. Its failing tests rerun there on the head and then on the base, with the select plugin copied along with each copy of the commit, and the base results are saved under that host's name. A spilled run whose failures are all pre-existing or flaky lands (landing only) without running the whole suite again on the project host; a failure new on the head still reruns here as before. Slurm batch checks are not judged.
