---
"tt-project": patch
---

`tt-project`: a coordinator batch whose task_add or task_update records a user order no longer breaks the actions after it. The order's note was stored in the variable that held the batch's action list, so a later task_update failed with "not enough values to unpack (expected 2, got 1)" and a later reply or notify skipped the started-on-host check. A charter_update with `replaces` no longer overwrites its own action index either. An action that fails on an internal error now says so (the error, where it was raised and the action's fields) and queues a harness `action_fault` event, instead of returning a bare Python error. A fault in the started-on-host check still sends the message, and now also queues a harness `onhost_fault` event saying it went unchecked.
