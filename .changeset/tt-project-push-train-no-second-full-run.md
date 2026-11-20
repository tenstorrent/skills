---
"tt-project": patch
---

`tt-project`:

- a push train no longer runs a second full check once its guilty search knows the failing tests: the prefix that passed the split probes, and the entries replayed after a blamed entry 1, run only those tests and the checks that never ran on the failing head; a probe that cannot tell still checks in full. A head or prefix that passed pushes at once and nothing joins it, as before.
- each push train records the CPU seconds of the check runs it waited for (`push_batches.check_cpu_s`; a spilled run counts only its ssh here), and `ttp stats --landing` adds them to CPU-h per landed change and shows them beside the trains' wall hours.
- `Batch._rescue` drops its unused carried-prefix branch (no behaviour change).
