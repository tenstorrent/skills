---
"tt-project": patch
---

A commit a later revert took out no longer reads as landed or live: the landed and live checks (landed waits, `ttp landed`, reply and notify claim lines) also test that the commit's change is still in the tree (`git apply --check -R` of its diff on a scratch index), and a check that cannot run marks the claim unverified, never "not landed". A post-landing test failure no longer reverts a push batch entry's whole range: flaky and pre-existing failures revert nothing, the entry's commits are bisected on the failing tests only (bounded to 15 minutes) and only the guilty commit is reverted, widening to its task's commits and then the range only when that conflicts, still fails or runs out of time, with the reason in the revert message; the fix task re-applies only what was reverted.
