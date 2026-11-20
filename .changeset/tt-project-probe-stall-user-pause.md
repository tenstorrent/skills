---
"tt-project": patch
---

`tt-project`: the progress check no longer raises a probe stall for a wait that is only `ttp lock --probe` of a resource the user paused, directly or through a user's fleet hold of its machine. Only the user's Resume ends such a wait, so waking the coordinator about it cost turns for nothing. A pause the coordinator set still counts. Once the user's pause lifts, the stall clock starts again from the lift, so the not-yets from before it never raise a stall.
