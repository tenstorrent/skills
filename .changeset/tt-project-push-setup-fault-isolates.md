---
"tt-project": patch
---

`tt-project`: once a push check's setup_fault holds raised the `push_setup_fault` alert, the push queue's next setup fault of that check on a batch head is a failed check of that head instead of another hold: the batch's entry search sends back the entry that brings it and pushes the rest, so one entry whose check names a missing dir or setup outside the repository no longer holds every other approval. A setup fault on the tip alone (every head) or no free pool slot still holds the batch and blames no entry; holds before the alert are unchanged.
