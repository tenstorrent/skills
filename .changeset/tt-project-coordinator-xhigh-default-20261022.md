---
"tt-project": patch
---

The coordinator runs every turn at xhigh effort by default. An explicit `coordinator.effort` wins,
and either is mapped to the provider's nearest supported level, applied at the next turn. The
low/high effort switch is removed with its knobs: `coordinator.unblock_effort`,
`coordinator.jev_threshold`, `coordinator.jev_wait_h`, `jev.uses.coord_effort` and the
coordinator's `escalate` action.
