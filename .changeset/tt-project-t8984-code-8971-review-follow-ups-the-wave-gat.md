---
"tt-project": patch
---

`tt-project`:

- tests: class-route tests follow the GLM work route's floor rule (#8980): a question stays on core until glm_work covers the project; a status-report work task still routes by class, urgent too
- tests: rename the class-route test to what it asserts (a status report routes to chat by class); drop asserts that repeat the question-floor test
- inflight: the backoff level is written with project.durable_write (atomic rename, no sync), so the durable-write test passes again
- inflight: a mid priority class between low and normal, and a host-wide record of chat-call outcomes (ttp chat-load)
- glmguard: a run on the chat model hands outward posts to an Opus step as drafts, and a device run stops at its first failed gate command
- glmshare: the wave gate checks every quality signal with an advance and a rollback margin and a minimum sample; the class-route test exercises the route; reviews always stay on core
