---
"tt-project": patch
---

`tt-project`: the on-host claim check on coordinator replies and notifies no longer marks a report of a past host event as "Planned, not started on the host yet". A sentence in the past tense only, or one that states a time at or before the send time, is posted as written; a stop not yet confirmed (its jobs still run) is still marked. A sentence backed by a reply claim whose `expect` probe passes at send time is posted as written. A host task waiting on its own detached driver or job on its resource reads "detached job running on <resource>" and counts as under way, whatever its status says. The "Status now:" line the check appends is no longer read as the coordinator's promise. A claim that work is happening now, with no worker started on the host, is still marked.
