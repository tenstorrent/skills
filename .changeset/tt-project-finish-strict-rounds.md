---
"tt-project": patch
---

`tt-project`: daemon: a finish runs as strict rounds: no git read runs under the write lock past HOLD_SLICE_S; a read that misses the warm-up rolls the round back, runs outside the lock and the next round replays it, and after MAX_ROUNDS the final round fails such a read fast as a timed-out git; gitmemo.run is the one git choke point (a guard test requires it); a git write in a round keeps its own cap and makes the round's point of no return, never cut by the slice. Measured on a 2,000-commit project with 200 task branches and each git call taking 0.4 s: the one-transaction finish held the write lock 5.28 s and 4.88 s, strict rounds at most 1.42 s.
