---
"tt-project": patch
---

`tt-project`: with spill hosts set and `delivery.checks_spill_first` on, the push batch's checks now go to an idle spill host before the project host, as `ttp checks` does. The project host keeps only checks that cannot spill, such as plugin validate and agent-CLI checks. A spilled check runs with the host's worker grant, and a fixed `-n N` above that grant is lowered to it on the spill host. If a spill host fails mid-run, the check reruns on the next spill host, or on the project host, and the batch does not fail. `ttp checks status` (and its `--json`, under `"here"`) shows each run that stayed on the project host and why. `checkspill.spill_run` gives other suites the same spill-first route. `delivery.push_hard_tests` is now a known setting and no longer gets an unknown-key hint.
