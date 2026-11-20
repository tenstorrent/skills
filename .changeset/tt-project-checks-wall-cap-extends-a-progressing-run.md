---
"tt-project": patch
---

`ttp checks` no longer stops a run at its wall-time cap while the run is still making progress. If its output grew within the silence limit and its pytest progress lines (`[NN%]`) project a finish within a bounded extension (at most half the cap, and never past the hard ceiling), the cap is extended. Each extension, or the reason none was granted, is written to the check log. A silent run is still stopped at the silence limit, and a run with no progress line, a stuck percentage or not enough room left still stops at the cap. A rerun after a script changed mid-run is skipped when the run's measured time cannot fit before the cap; the original result stands and the log says why. The checks-queue incident now calls a holder past the expected time "a likely hang" only when its output stopped growing. A holder still printing results is reported as "slow" with the host load.
