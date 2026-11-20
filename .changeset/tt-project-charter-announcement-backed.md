---
"tt-project": patch
---

`tt-project`: a coordinator reply or notify that tells a chat it is adding, changing or removing a charter item ("I'm adding the Restrictions item", "I'll record that in the charter") with no `charter_update` in its batch now queues a `charter_unsent` event for the next turn, quoting the reply. Within a batch, `charter_update` now applies right after `config_set` and before `task_add` and `task_update`, so tasks added in that turn read the new charter. A refused `charter_update`'s `charter_retry` event names the batch's replies that announced it.
