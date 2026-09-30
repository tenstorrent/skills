---
"tt-project": patch
---

`tt-project`: `ttp push` no longer falls back to `delivery.base_ref`; it needs
`delivery.push_branch` set explicitly and refuses `HEAD`, `main`, `master` and the remote's default
branch (and an unreachable remote). `delivery.push_allowed` strings such as `false`, `0`, `no` and
`off` now read as false, and a non-integer `delivery.push_rounds` is refused; it is clamped to at
least 1.
