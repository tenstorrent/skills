---
"tt-project": patch
---

Push batches settle a conflict where two changes each raised the same number in a test file (such as a prompt size cap) by adding both raises and keeping both comment reasons. The batch's checks judge the result, and the approval holds without a review rerun. Code conflicts and doc lines both sides changed still send the approval back.
