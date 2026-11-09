---
"tt-project": patch
---

`ttp push --own` and direct `ttp push` runs now use the same base-vs-head tests gate as the push queue and `ttp checks`. When a pytest check fails with a complete list of failing tests, those tests run once more on the head. Any that fail again then run on the merge base with the push target, in a temporary checkout on the same host. A test that passes on the rerun is quarantined as flaky. A test that fails the same way on the base is named as pre-existing. Neither blocks the push. A test that fails only on the head still blocks, and so does any failure matching `delivery.push_hard_tests` (the leak scan). A check excused this way is never recorded as a pass, and its rerun time is not counted as check time.
