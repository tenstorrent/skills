---
"tt-project": patch
---

GLM code runs review their own final diff for correctness before the hand-off: with the Opus advisor on, its first question after the last commit is a correctness review of the spec and diff, and the size review is done by the run itself instead of spending advisor calls on it.
