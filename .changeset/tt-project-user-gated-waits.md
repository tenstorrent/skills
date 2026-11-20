---
"tt-project": patch
---

`tt-project`: a wait that only the user's word can end no longer raises a probe stall or `wait_stale` to the coordinator. A probe whose top-level `&&` chain requires a user marker file (`test -e state/user-words/user-x && <check>`, with or without a trailing `|| exit 1`) counts as held by the user while that marker is missing; once the marker exists, the rest of the probe is judged as a live check again. A marker that is only an optional `||` term keeps the old behaviour. `wait_stale` now applies the same user-hold test as the progress check (user pause probe, missing user marker, a resource or machine the user paused, `wait_kind` user) and logs `wait_stale not raised: user hold` once per wait instead.
