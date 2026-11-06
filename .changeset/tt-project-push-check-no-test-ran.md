---
"tt-project": patch
---

`tt-project`: a push check that fails before any test ran (a work or head dir gone before the pool copy, an empty or corrupt archive, no free pool slot, or the push's own pool-wait bound) is an infrastructure fault, not a failed check. `ttp push`, `ttp land`, `ttp checks` and the push queue rerun a setup fault up to 2 times (a starved pool not at once); still so, `ttp push` exits 75 with a `retry_when`, `ttp checks` exits 75 with `infra_fault: no-test-ran`, and the push queue holds the batch (`setup_fault`) and retries it after 10 min. Any test verdict in the output keeps a failure a failure, and such a check never counts as passed.
