---
"tt-project": patch
---

`tt-project`: small-fix batch part 2. In the throughput report, a checks slot wait now ends at its slot grant: the checks' own run counts as the task's own job, and the rest of the span goes to the lane that held it. With `checks.slots` unset, a checks wait held by the host-sized lanes names the host's load, not a `checks.slots` raise. Waits behind a one-at-a-time wrapper lock (`checks.wrap_cmd`) count as serialized, not at the limit. A first probe verdict that runs past the step budget names its own task in the slow-step event. Credwatch's shared-change notice no longer says no run failed for it: a run whose own transcript writes that store still fails on its own, at high severity, and the guide says so. The `||` guard on a stored `retry_when` no longer reads the `&` of a redirect (`2>&1`, `&>f`) or `|&` as a separator, so a probe with one is still guarded.
