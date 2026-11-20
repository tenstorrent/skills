---
"tt-project": patch
---

`tt-project`: a coordinator batch no longer loses actions after a `task_add`. The user-order note a `task_add` or `task_update` records overwrote the batch's list of actions in `apply`, so every later `task_update` was rejected with "not enough values to unpack", a hold on `ask:new` no longer saw the batch's later `ask_user`, and the started-on-host check on later replies and notifies failed or missed the batch's later actions. The note now has its own name. Two other names `apply` reused with a different value are fixed too: a review `task_add` no longer replaces the batch start time that due moves compare owners with (an `obligation` after it raised a TypeError out of `apply`), and a refused `charter_update` with `replaces` now marks its own action refused, not the action at the replaced section's index.
