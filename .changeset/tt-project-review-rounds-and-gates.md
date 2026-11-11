---
"tt-project": patch
---

Reviews and landings never wait on a repeat of a test that already passed on the head: the review
and code prompts say a review judges the diff plus the gate results for that head, a wanted extra
test is a finding while the verdict still goes out, and only the required checks and the verdict
hold a landing. A failed re-review now goes to the coordinator as one `review_nofix` event instead
of the daemon queuing a third review round (`review.auto_fix_rounds`, default 1). The first
unreviewed sweep after an upgrade logs how many done code tasks have no landing, push-queue entry
or superseded record.
