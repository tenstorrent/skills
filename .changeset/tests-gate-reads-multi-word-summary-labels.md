---
"tt-project": patch
---

`tt-project`: the push checks' tests gate now judges pre-existing failures when pytest's summary line has a count label of several words, such as pytest-subtests' `158 subtests passed`. Before, such a line was not read, the failing ids counted as incomplete, and the gate failed the change with no base rerun even when the same tests failed on the push target. One shared reader of pytest's summary line (`sharedchecks.summary`) now serves every caller: the shared failure ids, the clean-run check, the gate's head rerun count, lost-worker accounting, output trimming and the `suite_green` goal check's test count. Subtest failures that no `FAILED` line names still leave the ids incomplete, so the gate stays on the safe side.
