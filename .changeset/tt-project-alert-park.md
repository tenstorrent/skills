---
"tt-project": patch
---

`tt-project`: a "needs you" alert now asks the user only for something they can and will do. An alert acknowledged with `ttp alerts --ack <id> --why` stays quiet for its condition while that condition is unchanged (a provider's logout, a missing optional key, a task's user-only step), across new episodes; a real change (the login came back and lapsed again, the task ended or its deferral no longer waits on the user) may raise it once more. A login-deferred task is a "needs you" item only while no other provider can run its work: with a logged-in failover target it waits silently, and an open item closes without a post. A provider logout whose work fails over is a feed line only. A missing model-ranking key while a ranking is in use is a low feed line, not a "needs you" item.
