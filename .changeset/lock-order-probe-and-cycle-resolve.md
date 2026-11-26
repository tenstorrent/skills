---
"tt-project": patch
---

`tt-project`: a resource's own lock now comes before its `<prefix>:<resource>` locks. A `ttp lock box1` inside a hold of `probe:box1` takes box1 only if it is free, else exits busy, so that pair can no longer deadlock. A lock-order cycle among one project's own tasks is broken by that project's daemon: the newest waiter's `ttp lock` exits busy with the reason, its task hands back `waiting` with no attempt spent, no holder or process is touched, and a `lock_cycle_resolved` event records it. A cycle across projects is still only alerted. `ttp setup` now also moves an installed bridge self-heal watch onto the new release's runtime when no bridge config is left.
