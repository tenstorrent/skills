---
"tt-project": patch
---

`tt-project`: the on-host claim check on coordinator replies and notifies no longer marks a report of a past host event as "Planned, not started on the host yet". A sentence in the past tense only, or one that states a time at or before the send time, is posted as written; a stop not yet confirmed (its jobs still run) is still marked. A sentence backed by a reply claim whose `expect` probe passes at send time is posted as written. The "Status now:" line the check appends is no longer read as the coordinator's promise. A claim that work is happening now, with no worker started on the host, is still marked.
 A claim that work started or finished ("started", "began", "launched", "kicked off", "finished", "completed") is never read as a past report: whatever its tense or stated time, it is marked until a worker has started on the host or its `expect` probe passes.
