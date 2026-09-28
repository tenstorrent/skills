# Commit Messages

- High level, informative. No diff narration.
- A `git log` reader learns what changed and why.
- **Subject:** `<scope>: <verb> <what>`. ≤72 chars, verb-led, lowercase scope.
- **Body** (when needed): 1-2 lines of **why**. Diff shows what.
- **Skip the body** when the subject says enough.
- **Load-bearing context only:** PCC, perf numbers, compat, invariants.
- NEVER list files or walk through the implementation.
