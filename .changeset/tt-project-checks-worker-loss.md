---
"tt-project": patch
---

`tt-project`: ttp checks: a test worker lost mid-run (the forked runner's 'ended (wait status ...) before this test finished' / 'no test worker was left') is an infrastructure fault, not a test verdict: the check runs once more, and a second clean-exit loss is reported as infra (exit 75, `infra_fault` in the checks record), never as a failure of the change.
